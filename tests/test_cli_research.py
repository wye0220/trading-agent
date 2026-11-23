"""阶段 5 的命令行：replay 与 baseline。"""
import json
from datetime import date

import helpers as H
import test_engine_runner as ER
from trading_agent.cli import main

D1 = ER.D1


def run(env, capsys, *argv):
    code = main(list(argv), settings=env.settings)
    return code, capsys.readouterr().out


def test_replay_needs_range(env, capsys):
    for argv in (["replay"], ["replay", "--from", "2025-11-25"], ["replay", "--from", "2025-11-26", "--to", "2025-11-25"],
                 ["replay", "--from", "x", "--to", "y"]):
        code, out = run(env, capsys, *argv, "--json")
        assert code == 2 and json.loads(out)["error"] == "CONFIG"
    assert env.con.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0] == 0        # 没有写任何数据


def test_replay_fixture_day(env, capsys):
    ER.load(env, ER.scripted_day())
    code, out = run(env, capsys, "replay", "--from", D1.isoformat(), "--to", D1.isoformat(), "--run-id", "r1", "--json")
    r = json.loads(out)
    assert code == 0 and r["ok"] and (r["run_mode"], r["steps"], r["trades"], r["run_id"]) == ("fixture", 78, 2, "r1")
    code, text = run(env, capsys, "replay", "--from", D1.isoformat(), "--to", D1.isoformat())
    assert code == 0 and "[FIXTURE]" in text and "不是投资建议" in text
    code, out = run(env, capsys, "replay", "--from", D1.isoformat(), "--to", D1.isoformat(), "--run-id", "r1", "--json")
    assert code == 1 and json.loads(out)["error"] == "RUN"                               # 运行标识已被占用


def test_replay_refuses_real_data(env, capsys):
    H.write_csv(env.root / "real.csv", H.rth_bars(D1, fixture=False))
    env.ingest()
    for extra in ([], ["--allow-real-data"]):                 # 没有设置开发可见截止日：显式允许也不行
        code, out = run(env, capsys, "replay", "--from", D1.isoformat(), "--to", D1.isoformat(), "--json", *extra)
        r = json.loads(out)
        assert code == 1 and r["status"] == "REFUSED" and r["error"] == "REAL_DATA_BOUNDARY"
    assert env.con.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0


def test_baseline_cli_on_fixtures(env, capsys):
    ER.load(env, ER.scripted_day() + ER.scripted_day(ER.D2))
    code, out = run(env, capsys, "baseline", "--json")
    r = json.loads(out)
    assert code == 0 and r["ok"] and r["status"] == "FROZEN" and "FIXTURE_ONLY" in r["flags"]
    assert r["strategy_validity"] == "EVIDENCE_INSUFFICIENT" and r["power"] is None and "没有开发段" in r["power_note"]
    assert r["total"]["trades"] == 0 and r["total"]["days"] == 0          # 两个交易日都在预热期内，不计入度量
    md = (env.settings.project_root / "reports" / "baseline_pa_brooks_ES_RTH_5m.md").read_text(encoding="utf-8")
    assert "FIXTURE_ONLY" in md and r["baseline_id"] in md and "另有 4 笔交易" in md
    code, text = run(env, capsys, "baseline", "--skip-power")
    assert code == 0 and "复用已冻结的结果" in text and "[FIXTURE_ONLY]" in text and "不是投资建议" in text
    code, out = run(env, capsys, "baseline", "--jobs", "0", "--json")
    assert code == 2 and json.loads(out)["error"] == "CONFIG"


def test_baseline_cli_refuses_real_data_without_boundary(env, capsys):
    H.write_csv(env.root / "real.csv", H.rth_bars(date(2025, 9, 15), fixture=False, source="databento"))
    env.ingest()
    code, out = run(env, capsys, "baseline", "--allow-real-data", "--json")
    r = json.loads(out)
    assert code == 1 and r["status"] == "REFUSED" and "开发可见截止日" in r["message"]
    assert env.con.execute("SELECT COUNT(*) FROM baselines").fetchone()[0] == 0
