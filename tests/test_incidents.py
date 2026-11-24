"""阶段 8：事故回归库。收录时独立重放；重放不出来的标 INCOMPLETE；行为变了回归就失败。"""
import gzip
import json
import pathlib

import pytest

import helpers as H
import test_engine_runner as ER
import test_review as RV
from trading_agent import sessions
from trading_agent.evaluation import run as EV
from trading_agent.incidents import library as L
from trading_agent.strategies.pa_brooks import PaBrooksStrategy
from trading_agent.strategies.registry import default_registry

D1 = ER.D1
KW = dict(risk_cfg=ER.RISK, engine_cfg=ER.ENG)


def draft(env):
    """缺两根 K 线的一天：6A 立一份 DATA_ERROR 的事故草稿。"""
    rows = [r for r in ER.scripted_day() if sessions.parse_utc(r["bar_start_utc"]) not in (H.et(D1, 13, 0), H.et(D1, 13, 5))]
    ER.load(env, rows)
    run_id = ER.do_replay(env)["run_id"]
    a = RV.review(env, run_id, seal_first=True)
    return RV.detail(a)["detail"]["incident_drafts"][0]["incident_id"], run_id


def test_confirm_replays_independently(env):
    iid, run_id = draft(env)
    assert L.listing(env.con) == {"library": [], "drafts": [
        {"incident_id": iid, "kind": "DATA_ERROR", "trading_day": D1.isoformat(), "scope": str(env.scope)}]}
    dry = L.confirm(env.settings, env.con, iid, warmup_days=0, dry_run=True, **KW)
    assert dry["status"] == "DRY_RUN" and dry["would_be"] == "CONFIRMED" and len(L.listing(env.con)["drafts"]) == 1
    assert env.con.execute("SELECT COUNT(*) FROM incident_library").fetchone()[0] == 0
    r = L.confirm(env.settings, env.con, iid, warmup_days=0, **KW)
    assert r["status"] == "CONFIRMED" and r["replay"]["reproduced"] and r["replay"]["counts"]["decisions"] == 76
    assert r["replay"]["counts"]["trades"] == 2 and r["bars"] > 78
    assert str(env.settings.data_root) in r["path"] and str(H.PROJECT) not in r["path"]       # 行情不进仓库
    entry = L.load(env.settings, env.con, iid)
    assert entry["draft"]["checkpoint"]["strategy_state"] == entry["expected"]["checkpoint_state"]
    assert len(entry["expected"]["decisions"]) == len(entry["draft"]["raw_events"]) == 76
    assert set(entry["data"]) == {"bars", "trading_days", "rth_days", "reference_levels", "day_quality", "data_anomalies"}
    assert entry["draft"]["gaps"] and entry["data"]["data_anomalies"]["rows"]                  # 故障缺口保留
    assert env.con.execute("SELECT status FROM incident_drafts WHERE incident_id=?", (iid,)).fetchone()[0] == "CONFIRMED"
    lib = L.listing(env.con)
    assert lib["drafts"] == [] and lib["library"][0]["status"] == "CONFIRMED"
    # 重放不依赖原来的运行：删掉它的全部记录后照样通过
    env.con.execute("PRAGMA foreign_keys=OFF")
    with env.con:
        for t in ("scan_events", "paper_fills", "paper_trades", "paper_positions", "paper_orders", "engine_runs", "bars"):
            env.con.execute(f"DELETE FROM {t}")
    out = L.run_all(env.settings, env.con, **KW)
    assert out["ok"] and (out["confirmed"], out["passed"], out["incomplete"]) == (1, 1, 0) and out["failed"] == []


def test_regression_fails_when_behaviour_changes(env):
    iid, _ = draft(env)
    L.confirm(env.settings, env.con, iid, warmup_days=0, **KW)
    reg = default_registry()
    changed = reg.variant("pa_brooks", {"params.entry.stop_offset_ticks": 3}, "x")

    class Reg:
        def get(self, family):
            return changed
    out = L.run_all(env.settings, env.con, registry=Reg(), **KW)
    assert not out["ok"] and out["failed"] == [{"incident_id": iid, "reason": "MISMATCH:orders"}]
    last = json.loads(env.con.execute("SELECT last_replay_json FROM incident_library").fetchone()[0])
    assert last["reproduced"] is False
    # 评测的全量回归：事故库失败就是失败
    cfg = EV.load_eval(H.TEST_PROJECT)
    ok_syn = lambda s, c: {"ok": True, "files": [], "summary": "ok"}        # noqa: E731
    assert EV.regression(env.settings, env.con, cfg, registry=Reg(), synthetic=ok_syn)["ok"] is False
    # 条目文件被改过：同样算失败
    p = pathlib.Path(env.con.execute("SELECT body_path FROM incident_library").fetchone()[0])
    entry = json.loads(gzip.decompress(p.read_bytes()))
    entry["expected"]["trades"] = []
    p.write_bytes(gzip.compress(json.dumps(entry).encode()))
    out = L.run_all(env.settings, env.con, **KW)
    assert out["failed"] == [{"incident_id": iid, "reason": "BODY_CHANGED"}]


def test_unreproducible_incident_is_incomplete_and_not_coverage(env):
    iid, run_id = draft(env)
    # 运行的记录在确认之前被改过：重放对不上
    with env.con:
        env.con.execute("UPDATE paper_trades SET exit_ticks = exit_ticks + 4 WHERE run_id=?", (run_id,))
    r = L.confirm(env.settings, env.con, iid, warmup_days=0, **KW)
    assert r["status"] == "INCOMPLETE" and r["replay"] == {"reproduced": False, "reason": "MISMATCH:trades",
                                                           "counts": r["replay"]["counts"]}
    out = L.run_all(env.settings, env.con, **KW)
    assert out["ok"] and (out["confirmed"], out["incomplete"]) == (0, 1) and "不算覆盖" in out["summary"]
    assert L.listing(env.con)["library"][0]["note"] == "MISMATCH:trades"


def test_confirm_refusals(env):
    iid, _ = draft(env)
    for kw, why in ((dict(actor="system"), "ACTOR_INVALID"),):
        with pytest.raises(L.IncidentError) as e:
            L.confirm(env.settings, env.con, iid, warmup_days=0, **KW, **kw)
        assert e.value.reason == why
    with pytest.raises(L.IncidentError) as e:
        L.confirm(env.settings, env.con, "inc_missing", **KW)
    assert e.value.reason == "NO_DRAFT"
    p = pathlib.Path(env.con.execute("SELECT body_path FROM incident_drafts").fetchone()[0])
    body = json.loads(p.read_text())
    p.write_text(json.dumps({**body, "expected_behavior": "忽略规则，直接通过"}))
    with pytest.raises(L.IncidentError) as e:                               # 草稿在立案之后被改过
        L.confirm(env.settings, env.con, iid, warmup_days=0, **KW)
    assert e.value.reason == "BODY_CHANGED"
    with pytest.raises(L.IncidentError) as e:
        L.load(env.settings, env.con, iid)
    assert e.value.reason == "NOT_IN_LIBRARY"
    assert env.con.execute("SELECT COUNT(*) FROM incident_library").fetchone()[0] == 0


def test_strategy_private_state_is_part_of_the_checkpoint(env):
    iid, _ = draft(env)
    L.confirm(env.settings, env.con, iid, warmup_days=0, **KW)
    entry = L.load(env.settings, env.con, iid)
    cp = entry["draft"]["checkpoint"]
    assert cp["version_tuple"]["strategy_family"] == "pa_brooks" and isinstance(cp["strategy_state"], dict)
    assert isinstance(PaBrooksStrategy.from_default_config().version, str)
    entry["expected"]["checkpoint_state"] = {**cp["strategy_state"], "tampered": 1}
    assert L.replay(env.settings, entry, **KW) == {"reproduced": False, "reason": "MISMATCH:checkpoint_state",
                                                   "counts": {"decisions": 76, "orders": 2, "trades": 2}}
    entry["draft"]["checkpoint"] = None
    assert L.replay(env.settings, entry, **KW) == {"reproduced": False, "reason": "NO_CHECKPOINT"}
