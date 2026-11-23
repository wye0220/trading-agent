"""行情只读导入：扫描数据根目录，增量入库，保留 revision 和可用时点。

不修改、不移动、不删除任何来源文件，不发起网络请求。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections import Counter
from datetime import date
from pathlib import Path

from . import contenthash, sessions
from .bars import TIMEFRAME_SECONDS, RawBar
from .config import Settings
from .sources import csv_source, dbn
from .sources.base import Anomaly, RejectedFile, UnsupportedSchema

BATCH = 50_000
SKIP_DIRS = {"runtime"}
HOLDOUT_NOT_SET = "HOLDOUT_BOUNDARY_NOT_SET"

_STAGE = """
CREATE TEMP TABLE IF NOT EXISTS stage (
    seq INTEGER PRIMARY KEY, source TEXT, contract_id TEXT, timeframe TEXT, bar_start_utc INTEGER,
    instrument_root TEXT, trading_day TEXT, session_type TEXT,
    o INTEGER, h INTEGER, l INTEGER, c INTEGER, v INTEGER, fetched INTEGER, fixture INTEGER,
    newkey INTEGER NOT NULL DEFAULT 0)
"""
_KEY = "source, contract_id, timeframe, bar_start_utc"
_TF_SEC = "CASE s.timeframe WHEN '1m' THEN 60 WHEN '5m' THEN 300 ELSE 3600 END"


def new_run_id(prefix: str) -> str:
    return f"{prefix}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{uuid.uuid4().hex[:8]}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def discover(data_root: Path) -> list[Path]:
    """数据根目录下的行情文件（跳过 runtime/ 和隐藏目录），按路径排序。"""
    out = []
    if not data_root.is_dir():
        return out
    for p in sorted(data_root.rglob("*")):
        rel = p.relative_to(data_root)
        if not p.is_file() or rel.parts[0] in SKIP_DIRS or any(part.startswith(".") for part in rel.parts):
            continue
        n = p.name.lower()
        if n.endswith((".dbn", ".dbn.zst", ".csv")) or n == "condition.json":
            out.append(p)
    return out


class Ingestor:
    def __init__(self, settings: Settings, con: sqlite3.Connection, run_id: str, cutoff: date | None):
        self.settings, self.con, self.run_id = settings, con, run_id
        self.cutoff = cutoff.isoformat() if cutoff else None
        self.delay = settings.available_at_delay_seconds
        self.counts: Counter = Counter()
        self.touched: set[tuple[str, str, str]] = set()
        self.withheld: dict[tuple, list] = {}
        con.execute(_STAGE)

    # ------------------------------------------------------------ 异常
    def anomaly(self, a: Anomaly, file_id: int | None = None):
        self.counts["anomaly:" + a.category] += 1
        self.con.execute(
            "INSERT OR IGNORE INTO data_anomalies (category, source, instrument_root, contract_id, timeframe,"
            " start_utc, end_utc, trading_day, detail, file_id, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (a.category, a.source or "", a.instrument_root or "", a.contract_id or "", a.timeframe or "",
             a.start_utc if a.start_utc is not None else -1, a.end_utc, a.trading_day, a.detail, file_id, self.run_id))

    # ------------------------------------------------------------ 暂存
    def stage(self, items, file_id: int):
        con = self.con
        con.execute("DELETE FROM stage")
        batch = []
        classify = sessions.classify
        cutoff = self.cutoff
        for it in items:
            if isinstance(it, Anomaly):
                self.anomaly(it, file_id)
                continue
            b: RawBar = it
            self.counts["records_read"] += 1
            if not (b.low <= b.open <= b.high and b.low <= b.close <= b.high) or b.volume < 0:
                self.anomaly(Anomaly("OHLC_INCONSISTENT", b.source, b.instrument_root, b.contract_id, b.timeframe,
                                     b.bar_start_utc, b.bar_end_utc,
                                     detail=f"o={b.open} h={b.high} l={b.low} c={b.close} v={b.volume}"), file_id)
                continue
            day, sess = classify(b.bar_start_utc)
            if day is None:
                self.anomaly(Anomaly("OUTSIDE_SESSION", b.source, b.instrument_root, b.contract_id, b.timeframe,
                                     b.bar_start_utc, b.bar_end_utc), file_id)
                continue
            ds = day.isoformat()
            if cutoff and ds > cutoff and not b.fixture:
                self.counts["withheld_after_cutoff"] += 1
                self.withhold(b.source, b.contract_id, b.instrument_root, b.timeframe, ds, sess, b.bar_start_utc,
                              b.open, b.high, b.low, b.close, b.volume)
                continue
            batch.append((b.source, b.contract_id, b.timeframe, b.bar_start_utc, b.instrument_root, ds, sess,
                          b.open, b.high, b.low, b.close, b.volume, b.fetched_at_utc, int(b.fixture)))
            if len(batch) >= BATCH:
                self._flush(batch)
        self._flush(batch)

    def _flush(self, batch):
        if batch:
            self.con.executemany(
                "INSERT INTO stage (source, contract_id, timeframe, bar_start_utc, instrument_root, trading_day,"
                " session_type, o, h, l, c, v, fetched, fixture) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()

    # ------------------------------------------------------------ 合并入库
    def merge(self, file_id: int):
        con = self.con
        # 1. 完全相同的重复行只留一条
        cur = con.execute(
            f"DELETE FROM stage WHERE seq NOT IN (SELECT MIN(seq) FROM stage GROUP BY {_KEY}, o, h, l, c, v, fetched)")
        self.counts["duplicates_skipped"] += cur.rowcount
        # 2. 没有取数时刻的来源：同一文件内同键不同内容 = 冲突，两条都不入库
        con.execute(f"CREATE TEMP TABLE IF NOT EXISTS conflict AS SELECT {_KEY} FROM stage WHERE 0")
        con.execute("DELETE FROM conflict")
        con.execute(f"INSERT INTO conflict SELECT {_KEY} FROM stage WHERE fetched IS NULL GROUP BY {_KEY} HAVING COUNT(*) > 1")
        for r in con.execute(
                "SELECT s.source, s.contract_id, s.timeframe, s.bar_start_utc, s.instrument_root, s.trading_day, COUNT(*) n"
                f" FROM stage s JOIN conflict USING ({_KEY}) GROUP BY s.source, s.contract_id, s.timeframe, s.bar_start_utc").fetchall():
            self.anomaly(Anomaly("CONFLICTING_DUPLICATE", r[0], r[4], r[1], r[2], r[3],
                                 r[3] + TIMEFRAME_SECONDS[r[2]], r[5], f"同一文件内 {r[6]} 条内容不同的记录"), file_id)
        con.execute(f"DELETE FROM stage WHERE ({_KEY}) IN (SELECT {_KEY} FROM conflict)")

        not_exists = (" NOT EXISTS (SELECT 1 FROM bars b WHERE b.source=s.source AND b.contract_id=s.contract_id"
                      " AND b.timeframe=s.timeframe AND b.bar_start_utc=s.bar_start_utc)")
        # 3. 快速通道：全新的键、没有取数时刻 → revision 1，可用时点为估计值
        con.execute(f"UPDATE stage AS s SET newkey = 1 WHERE s.fetched IS NULL AND {not_exists}")
        for r in con.execute("SELECT DISTINCT source, contract_id, trading_day FROM stage WHERE newkey = 1"):
            self.touched.add((r[0], r[1], r[2]))
        cur = con.execute(
            "INSERT INTO bars (source, contract_id, timeframe, bar_start_utc, revision, instrument_root, bar_end_utc,"
            " trading_day, session_type, open, high, low, close, volume, available_at_utc, available_at_estimated,"
            " derived, fixture, file_id)"
            f" SELECT s.source, s.contract_id, s.timeframe, s.bar_start_utc, 1, s.instrument_root,"
            f" s.bar_start_utc + {_TF_SEC}, s.trading_day, s.session_type, s.o, s.h, s.l, s.c, s.v,"
            f" s.bar_start_utc + {_TF_SEC} + ?, 1, 0, s.fixture, ?"
            " FROM stage s WHERE s.newkey = 1", (self.delay, file_id))
        self.counts["bars_inserted"] += cur.rowcount
        con.execute("DELETE FROM stage WHERE newkey = 1")

        # 4. 其余：键已存在，或带取数时刻（同键可有多行，按取数时刻先后成为依次的 revision）
        rows = con.execute(
            "SELECT s.source, s.contract_id, s.timeframe, s.bar_start_utc, s.instrument_root, s.trading_day,"
            " s.session_type, s.o, s.h, s.l, s.c, s.v, s.fetched, s.fixture,"
            " b.revision, b.open, b.high, b.low, b.close, b.volume"
            " FROM stage s LEFT JOIN bars b ON b.source=s.source AND b.contract_id=s.contract_id"
            " AND b.timeframe=s.timeframe AND b.bar_start_utc=s.bar_start_utc"
            " AND b.revision=(SELECT MAX(revision) FROM bars x WHERE x.source=s.source AND x.contract_id=s.contract_id"
            "   AND x.timeframe=s.timeframe AND x.bar_start_utc=s.bar_start_utc)"
            " ORDER BY s.source, s.contract_id, s.timeframe, s.bar_start_utc, s.fetched, s.seq")
        inserts = []
        cur_key, cur_rev, cur_content = None, 0, None
        for r in rows:
            key = (r[0], r[1], r[2], r[3])
            if key != cur_key:
                cur_key = key
                cur_rev = r[14] or 0
                cur_content = tuple(r[15:20]) if r[14] else None
            content = tuple(r[7:12])
            if content == cur_content:
                self.counts["duplicates_skipped"] += 1
                continue
            cur_rev += 1
            cur_content = content
            end = r[3] + TIMEFRAME_SECONDS[r[2]]
            if r[12] is None:
                avail, est = end + self.delay, 1
            else:
                avail, est = max(r[12], end), 0
            inserts.append((r[0], r[1], r[2], r[3], cur_rev, r[4], end, r[5], r[6], *content, avail, est, 0, r[13], file_id))
            self.touched.add((r[0], r[1], r[5]))
            self.counts["revisions_added" if cur_rev > 1 else "bars_inserted"] += 1
        if inserts:
            con.executemany(
                "INSERT INTO bars (source, contract_id, timeframe, bar_start_utc, revision, instrument_root, bar_end_utc,"
                " trading_day, session_type, open, high, low, close, volume, available_at_utc, available_at_estimated,"
                " derived, fixture, file_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", inserts)
        con.execute("DELETE FROM stage")

    def withhold(self, source, cid, root, tf, day, sess, start, o, h, l, c, v):  # noqa: E741
        """一根留在库外的 K 线：只累计条数、RTH 覆盖和内容摘要，不保存价格。"""
        w = self.withheld.setdefault((source, cid, root, tf, day), [0, set(), 0])
        w[0] += 1
        if sess == "RTH":
            w[1].add(start // 300)
        w[2] = contenthash.add(w[2], contenthash.bar_digest(source, cid, tf, start, o, h, l, c, v))

    def flush_withheld(self):
        for (src, cid, root, tf, day), (n, slots, acc) in self.withheld.items():
            expected = len(sessions.expected_rth_slots(date.fromisoformat(day))) if sessions.is_trading_day(
                date.fromisoformat(day)) else 0
            prev = self.con.execute(
                "SELECT content_sha256 FROM withheld_coverage WHERE source=? AND contract_id=? AND timeframe=?"
                " AND trading_day=?", (src, cid, tf, day)).fetchone()
            digest = contenthash.merge_hex(prev[0] if prev else None, contenthash.to_hex(acc))
            self.con.execute(
                "INSERT INTO withheld_coverage (source, contract_id, instrument_root, timeframe, trading_day,"
                " bar_count, rth_slots_present, content_sha256) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET"
                " bar_count = bar_count + excluded.bar_count,"
                " rth_slots_present = MIN(rth_slots_present + excluded.rth_slots_present, ?),"
                " content_sha256 = excluded.content_sha256",
                (src, cid, root, tf, day, n, len(slots), digest, expected))
        self.withheld.clear()


def enforce_cutoff(settings: Settings, con: sqlite3.Connection, run_id: str, cutoff: date) -> dict:
    """把运行库中晚于开发可见截止日的真实 K 线移出：先记下覆盖和内容摘要，再删除它们和由它们算出的结果。

    用于先全量导入、后来才设置（或提前）截止日的情形。原始文件不动。合成数据不受影响。
    """
    cut = cutoff.isoformat()
    ing = Ingestor(settings, con, run_id, cutoff)
    rows = con.execute(
        "SELECT source, contract_id, instrument_root, timeframe, trading_day, session_type, bar_start_utc,"
        " open, high, low, close, volume FROM bars b WHERE trading_day > ? AND derived = 0 AND fixture = 0"
        " AND revision = (SELECT MAX(revision) FROM bars x WHERE x.source=b.source AND x.contract_id=b.contract_id"
        "   AND x.timeframe=b.timeframe AND x.bar_start_utc=b.bar_start_utc)", (cut,))
    moved = 0
    for r in rows:
        ing.withhold(r[0], r[1], r[2], r[3], r[4], r[5], r[6], *r[7:12])
        moved += 1
    if not moved and con.execute("SELECT 1 FROM bars WHERE trading_day > ? AND fixture = 0 LIMIT 1",
                                 (cut,)).fetchone() is None:
        return {"moved_after_cutoff": 0, "rows_deleted": 0}
    sources = sorted({k[0] for k in ing.withheld})
    with con:
        ing.flush_withheld()
        deleted = con.execute("DELETE FROM bars WHERE trading_day > ? AND fixture = 0", (cut,)).rowcount
        con.execute("DELETE FROM trading_days WHERE trading_day > ? AND fixture = 0", (cut,))
        con.execute("DELETE FROM rth_days WHERE trading_day > ? AND NOT EXISTS (SELECT 1 FROM bars b WHERE"
                    " b.source = rth_days.source AND b.contract_id = rth_days.contract_id"
                    " AND b.trading_day = rth_days.trading_day)", (cut,))
        con.execute("DELETE FROM reference_levels WHERE trading_day > ? AND NOT EXISTS (SELECT 1 FROM trading_days t"
                    " WHERE t.instrument_root = reference_levels.instrument_root"
                    " AND t.trading_day = reference_levels.trading_day)", (cut,))
        con.execute("DELETE FROM market_snapshots WHERE trading_day > ? AND fixture = 0", (cut,))
        if sources:
            ph = ",".join("?" * len(sources))
            con.execute(f"DELETE FROM data_anomalies WHERE trading_day > ? AND source IN ({ph})"
                        " AND category IN ('UNEXPECTED_GAP','THIN_BAR','INCOMPLETE_HOUR')", (cut, *sources))
        con.execute("DELETE FROM data_anomalies WHERE trading_day > ? AND category = 'WRONG_CONTRACT'"
                    " AND NOT EXISTS (SELECT 1 FROM trading_days t WHERE t.instrument_root ="
                    " data_anomalies.instrument_root AND t.trading_day = data_anomalies.trading_day)", (cut,))
    return {"moved_after_cutoff": moved, "rows_deleted": deleted}


def _file_row(con, rel: str):
    return con.execute("SELECT * FROM ingest_files WHERE path = ?", (rel,)).fetchone()


def _upsert_file(con, rel, size, sha, kind, schema, rows, prefix, records, status, detail, run_id) -> int:
    con.execute(
        "INSERT INTO ingest_files (path, size, sha256, kind, data_schema, rows_processed, prefix_sha256, records,"
        " status, detail, run_id, updated_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET size=excluded.size, sha256=excluded.sha256, kind=excluded.kind,"
        " data_schema=excluded.data_schema, rows_processed=excluded.rows_processed,"
        " prefix_sha256=excluded.prefix_sha256, records=excluded.records, status=excluded.status,"
        " detail=excluded.detail, run_id=excluded.run_id, updated_at_utc=excluded.updated_at_utc",
        (rel, size, sha, kind, schema, rows, prefix, records, status, detail, run_id, int(time.time())))
    return con.execute("SELECT file_id FROM ingest_files WHERE path = ?", (rel,)).fetchone()[0]


def ingest_files(settings: Settings, con: sqlite3.Connection, run_id: str, cutoff: date | None) -> tuple[dict, set]:
    """导入数据根目录里新增或变化的内容。返回 (摘要, 受影响的 (source, contract_id, trading_day))。"""
    ing = Ingestor(settings, con, run_id, cutoff)
    files = {"imported": [], "skipped": [], "unsupported": [], "rejected": []}
    root = settings.data_root
    for path in discover(root):
        rel = str(path.relative_to(root))
        size = path.stat().st_size
        prev = _file_row(con, rel)
        name = path.name.lower()

        if name == "condition.json":
            sha = sha256_file(path)
            if prev and prev["sha256"] == sha:
                files["skipped"].append(rel)
                continue
            with con:
                try:
                    days = json.loads(path.read_text(encoding="utf-8"))
                    rows = [("databento", d["date"], d["condition"]) for d in days]
                except (ValueError, KeyError, TypeError) as e:
                    _upsert_file(con, rel, size, sha, "condition", None, 0, None, 0, "REJECTED", str(e), run_id)
                    files["rejected"].append({"path": rel, "reason": str(e)})
                    continue
                con.executemany("INSERT OR REPLACE INTO day_quality VALUES (?,?,?)", rows)
                _upsert_file(con, rel, size, sha, "condition", None, 0, None, len(rows), "IMPORTED", None, run_id)
                ing.counts["quality_days_loaded"] += len(rows)
            files["imported"].append(rel)
            continue

        if name.endswith(".csv"):
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if prev and prev["sha256"] == sha:
                files["skipped"].append(rel)
                continue
            lines = csv_source.split_lines(data)
            start_row = 0
            if prev and prev["status"] == "IMPORTED" and prev["rows_processed"] and len(lines) - 1 >= prev["rows_processed"]:
                head = hashlib.sha256(b"".join(lines[:1 + prev["rows_processed"]])).hexdigest()
                if head == prev["prefix_sha256"]:
                    start_row = prev["rows_processed"]       # 只是在末尾追加：从上次之后续读
            with con:
                try:
                    before = ing.counts["records_read"]
                    file_id = _upsert_file(con, rel, size, sha, "csv", "csv", len(lines) - 1,
                                           hashlib.sha256(b"".join(lines)).hexdigest(), 0, "IMPORTED", None, run_id)
                    ing.stage(csv_source.read(lines, path.name, settings.instruments, start_row), file_id)
                    ing.merge(file_id)
                    con.execute("UPDATE ingest_files SET records = ? WHERE file_id = ?",
                                (ing.counts["records_read"] - before + (prev["records"] if prev and start_row else 0), file_id))
                except RejectedFile as e:
                    con.execute("DELETE FROM stage")
                    _upsert_file(con, rel, size, sha, "csv", "csv", 0, None, 0, "REJECTED", str(e), run_id)
                    files["rejected"].append({"path": rel, "reason": str(e)})
                    continue
            files["imported"].append(rel)
            continue

        # DBN（写一次就不变）：摘要相同直接跳过
        sha = sha256_file(path)
        if prev and prev["sha256"] == sha:
            files["skipped"].append(rel)
            continue
        with con:
            try:
                stats: Counter = Counter()
                meta = dbn.read_metadata(path)
                schema = f"{meta.dataset}/{meta.schema}"
                file_id = _upsert_file(con, rel, size, sha, "dbn", schema, 0, None, 0, "IMPORTED", None, run_id)
                ing.stage(dbn.read(path, settings.instruments, stats), file_id)
                ing.merge(file_id)
                con.execute("UPDATE ingest_files SET records = ? WHERE file_id = ?", (stats["records"], file_id))
                for k in ("spread_records", "spread_symbols", "unregistered_records"):
                    ing.counts[k] += stats[k]
            except UnsupportedSchema as e:
                con.execute("DELETE FROM stage")
                _upsert_file(con, rel, size, sha, "dbn", str(e), 0, None, 0, "UNSUPPORTED_SCHEMA", str(e), run_id)
                files["unsupported"].append({"path": rel, "schema": str(e)})
                continue
        files["imported"].append(rel)

    with con:
        ing.flush_withheld()
    counts = dict(sorted(ing.counts.items()))
    summary = {
        "files": {k: v for k, v in files.items()},
        "files_imported": len(files["imported"]), "files_skipped": len(files["skipped"]),
        "files_unsupported": len(files["unsupported"]), "files_rejected": len(files["rejected"]),
        "bars_inserted": counts.get("bars_inserted", 0), "revisions_added": counts.get("revisions_added", 0),
        "duplicates_skipped": counts.get("duplicates_skipped", 0),
        "spread_symbols_dropped": counts.get("spread_symbols", 0),
        "spread_records_dropped": counts.get("spread_records", 0),
        "withheld_after_cutoff": counts.get("withheld_after_cutoff", 0),
        "anomalies": {k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith("anomaly:")},
    }
    return summary, ing.touched
