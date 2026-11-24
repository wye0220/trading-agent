"""发布后监控：每个交易日收盘后，对每个有 active 指针的 scope 计算该版本激活以来的滚动指标，
与清单里冻结的回滚触发条件比较。触发时自动冻结该 scope 的纸面入场（原因 MONITOR）并告警；回滚与否由你决定。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time

from .. import sessions
from ..config import Scope, Settings
from ..engine.config import RiskConfig
from ..manual.common import paper_run_id
from ..research import metrics
from ..scan import _session_for
from . import freeze as rfreeze
from . import pointer
from .config import ReleaseConfig


def _sha(obj) -> str:
    return hashlib.sha256(pointer.canon(obj).encode()).hexdigest()


def activation(release_con: sqlite3.Connection, scope: Scope, release_id: str) -> int | None:
    r = release_con.execute("SELECT MAX(created_at_utc) FROM release_events WHERE scope=? AND release_id=?"
                            " AND event_type IN ('ACTIVATED','ROLLED_BACK')", (str(scope), release_id)).fetchone()
    return r[0] if r else None


def first_day_after(t: int) -> str:
    """激活之后第一个完全由新版本处理的交易日：激活早于当日开盘则是当日，否则是下一个交易日。"""
    d = sessions.trading_day_at(t)
    o, _ = sessions.rth_bounds(d)
    return (d if t < o else sessions.next_trading_day(d)).isoformat()


def _days(con, scope: Scope, run_id: str, start: str, end: str) -> list[str]:
    seen = {r[0] for r in con.execute("SELECT DISTINCT json_extract(decision_json,'$.trading_day') FROM scan_events"
                                      " WHERE run_id=?", (run_id,))}
    return [r[0] for r in con.execute("SELECT trading_day FROM trading_days WHERE instrument_root=? AND trading_day"
                                      " BETWEEN ? AND ? AND complete=1 AND degraded=0 ORDER BY trading_day",
                                      (scope.instrument_root, start, end)) if r[0] in seen]


def _of_release(vt_json: str, release_id: str) -> bool:
    try:
        return json.loads(vt_json or "{}").get("release_id") == release_id
    except ValueError:
        return False


def metrics_for(settings: Settings, con: sqlite3.Connection, scope: Scope, release_id: str, start: str, end: str,
                risk_cfg: RiskConfig, window: int) -> dict:
    run_id = paper_run_id(scope)
    days = _days(con, scope, run_id, start, end)
    mine = {r[0] for r in con.execute("SELECT trade_id, version_tuple_json FROM paper_trades WHERE run_id=?", (run_id,))
            if _of_release(r[1], release_id)}
    trades = [t for t in metrics.load_trades(settings, con, scope, run_id, risk_cfg) if t["trade_id"] in mine
              and t["trading_day"] in set(days)]
    ser = metrics.daily_series(trades, days, risk_cfg.research_risk_unit_usd)
    net = ser["net"]
    roll = net[-window:]
    setups = sum(1 for d, vt in con.execute(
        "SELECT json_extract(decision_json,'$.trading_day'), version_tuple_json FROM scan_events WHERE run_id=?"
        " AND signal_status='SETUP_VALID'", (run_id,)) if d in set(days) and _of_release(vt, release_id))
    amb = sum(1 for t in trades if "AMBIGUOUS_BAR" in t["flags"])
    return {"run_id": run_id, "first_day": start, "valid_days": len(days), "unit": ser["unit"], "trades": len(trades),
            "rolling_days": len(roll), "rolling_net_per_day": sum(roll) / len(roll) if roll else None,
            "drawdown": metrics.max_drawdown(net) if net else 0.0,
            "trades_per_day": len(trades) / len(days) if days else None,
            "setups_per_day": setups / len(days) if days else None,
            "ambiguous_bar_share": amb / len(trades) if trades else None}


def breaches(m: dict, trig: dict) -> list[dict]:
    out = []
    full = m["rolling_days"] >= trig["rolling_valid_days"]
    if full and trig.get("rolling_net_per_day_floor") is not None and m["rolling_net_per_day"] is not None \
            and m["rolling_net_per_day"] < trig["rolling_net_per_day_floor"]:
        out.append({"trigger": "ROLLING_NET", "value": m["rolling_net_per_day"], "limit": trig["rolling_net_per_day_floor"]})
    if trig.get("drawdown_limit") is not None and m["drawdown"] > trig["drawdown_limit"]:
        out.append({"trigger": "DRAWDOWN", "value": m["drawdown"], "limit": trig["drawdown_limit"]})
    for key, name in (("trades_per_day", "TRADE_FREQUENCY"), ("setups_per_day", "SIGNAL_FREQUENCY")):
        band = trig.get(f"{key}_band")
        if full and band and m[key] is not None and not (band[0] <= m[key] <= band[1]):
            out.append({"trigger": name, "value": m[key], "limit": band})
    if m["trades"] >= trig["ambiguous_min_trades"] and (m["ambiguous_bar_share"] or 0) > trig["ambiguous_bar_share_max"]:
        out.append({"trigger": "AMBIGUOUS_BAR", "value": m["ambiguous_bar_share"], "limit": trig["ambiguous_bar_share_max"]})
    return out


def notifier(settings: Settings, cfg: ReleaseConfig):
    cmd = cfg.monitor["notify_command"]

    def send(text: str) -> str:
        if not cmd:
            return "NOT_CONFIGURED"
        p = settings.project_root / cmd
        if not p.is_file():
            return "MISSING_SCRIPT"
        try:
            r = subprocess.run([str(p), text], capture_output=True, text=True, timeout=60)
            return "SENT" if r.returncode == 0 else f"FAILED:{r.returncode}"
        except (OSError, subprocess.SubprocessError) as e:
            return f"FAILED:{type(e).__name__}"
    return send


def run(settings: Settings, con: sqlite3.Connection, release_con, scope: Scope, cfg: ReleaseConfig, *,
        risk_cfg: RiskConfig, as_of_day: str | None = None, now: int | None = None, notify=None) -> dict:
    now = now or int(time.time())
    act = pointer.resolve(release_con, scope)
    if act is None:
        return {"scope": str(scope), "status": "NO_ACTIVE"}
    if act["manifest"] is None:
        return {"scope": str(scope), "status": "NO_MANIFEST", "release_id": act["release_id"]}
    rid = act["release_id"]
    day = as_of_day or _session_for(now)[0].isoformat()
    mid = "mon_" + _sha([str(scope), rid, act["manifest_hash"], day])[:16]
    old = con.execute("SELECT * FROM monitor_runs WHERE monitor_id=?", (mid,)).fetchone()
    if old is not None:
        return {"scope": str(scope), "status": "ALREADY_RUN", "monitor_id": mid, "release_id": rid,
                "metrics": json.loads(old["metrics_json"]), "breaches": json.loads(old["breaches_json"]),
                "action": old["action"]}
    t0 = activation(release_con, scope, rid) or act["updated_at_utc"]
    start = first_day_after(t0)
    trig = act["manifest"]["rollback_trigger"]
    m = metrics_for(settings, con, scope, rid, start, day, risk_cfg, trig["rolling_valid_days"])
    found = breaches(m, trig) if start <= day else []
    action, sent = "NONE", None
    if found:
        why = "；".join(f"{b['trigger']} {b['value']} 越过 {b['limit']}" for b in found)
        f = rfreeze.freeze(con, scope, "MONITOR", why, "monitor")
        action = f["status"]
        text = (f"trading-agent 发布监控：{scope} 版本 {rid} 触发回滚条件，已冻结新入场（MONITOR）。{why}。"
                f" 回滚或解冻由你决定：python -m trading_agent rollback --scope {scope} 或 freeze --unfreeze --cause MONITOR")
        sent = (notify or notifier(settings, cfg))(text) if action == "FROZEN" else "SKIPPED_ALREADY_FROZEN"
    with con:
        con.execute("INSERT INTO monitor_runs VALUES (?,?,?,?,?,?,?,?)",
                    (mid, str(scope), rid, day, pointer.canon(m), pointer.canon(found), action, now))
    return {"scope": str(scope), "status": "BREACHED" if found else "OK", "monitor_id": mid, "release_id": rid,
            "as_of_day": day, "metrics": m, "breaches": found, "action": action, "notify": sent,
            "unvalidated": "UNVALIDATED_BASELINE" in (act["manifest"].get("labels") or []),
            "entries_enabled": act["entries_enabled"]}

