import hashlib
import json
from datetime import date

import pytest

import helpers as H
from trading_agent import cli, sessions
from trading_agent.cli import COMMANDS, IMPLEMENTED, main

D1 = date(2025, 11, 25)
NOT_IMPL = sorted(set(COMMANDS) - IMPLEMENTED)


def run(env, capsys, *argv):
    code = main(list(argv), settings=env.settings)
    return code, capsys.readouterr().out


def test_help_lists_all_subcommands(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert len(COMMANDS) == 15 and len(NOT_IMPL) == 8
    for name in ("ingest", "collect", "scan", "plan", "watch", "replay", "review", "evolve", "evaluate", "shadow",
                 "release", "rollback", "freeze", "monitor"):
        assert f"\n    {name} " in out


@pytest.mark.parametrize("name", NOT_IMPL)
def test_not_implemented_touches_nothing(env, capsys, name):
    env.con.commit()
    db_file = env.settings.runtime_db
    before = hashlib.sha256(db_file.read_bytes()).hexdigest()
    listing = sorted(p.name for p in db_file.parent.iterdir())
    code, out = run(env, capsys, name)
    obj = json.loads(out)
    assert code != 0 and obj["status"] == "NOT_IMPLEMENTED" and obj["command"] == name
    assert obj["phase"] == str(COMMANDS[name])
    assert hashlib.sha256(db_file.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in db_file.parent.iterdir()) == listing
    assert not env.settings.release_db.exists()


def test_default_scope_echoed(env, capsys):
    H.write_csv(env.root / "fixture.csv", H.rth_bars(D1))
    assert run(env, capsys, "ingest", "--json", "--no-report")[0] == 0
    code, out = run(env, capsys, "scan", "--as-of", sessions.iso(H.et(D1, 12, 0)), "--json")
    assert code == 0 and json.loads(out)["scope"] == "pa_brooks:ES:RTH:5m"


@pytest.mark.parametrize("scope,needle", [("ES:5m", "scope 格式错误"), ("pa_brooks:CL:RTH:5m", "品种未登记")])
def test_bad_scope_rejected(env, capsys, scope, needle):
    code, out = run(env, capsys, "scan", "--scope", scope)
    assert code != 0 and needle in out
    assert env.con.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0] == 0
    code, out = run(env, capsys, "scan", "--scope", scope, "--json")
    assert code != 0 and json.loads(out)["error"] == "CONFIG"


def test_ingest_json_and_rerun(env, capsys, monkeypatch):
    H.write_csv(env.root / "fixture.csv", H.eth_bars(D1) + H.rth_bars(D1))
    monkeypatch.setattr("trading_agent.config.PROJECT_ROOT", env.tmp)      # 报告写到临时目录
    s = env.settings
    object.__setattr__(s, "project_root", env.tmp)
    code, out = run(env, capsys, "ingest", "--json")
    a = json.loads(out)
    assert code == 0 and a["ok"] and a["run_id"].startswith("ing_") and a["files_imported"] == 1
    assert a["bars_inserted"] == 156 and (env.tmp / "reports" / "data_availability.md").is_file()
    n = env.count()
    code, out = run(env, capsys, "ingest", "--json")
    b = json.loads(out)
    assert code == 0 and b["files_skipped"] == 1 and b["bars_inserted"] == 0 and env.count() == n
    code, out = run(env, capsys, "ingest", "--through", "not-a-date")
    assert code != 0 and "--through" in out


def test_scan_wait_and_data_error(env, capsys):
    H.write_csv(env.root / "fixture.csv", H.rth_bars(D1, skip=[(11, 0)]))
    run(env, capsys, "ingest", "--no-report")
    rel_before = env.settings.release_db.read_bytes()

    code, out = run(env, capsys, "scan", "--as-of", sessions.iso(H.et(D1, 10, 30, 5)), "--json")
    ok = json.loads(out)
    assert code == 0 and ok["signal_status"] == "WAIT" and ok["paper_action"] == "NONE"
    assert ok["snapshot_id"].startswith("snap_") and ok["contract_id"] == "ESZ2025" and ok["strategy_ran"] is True
    assert ok["latest_bar"]["bar_end_utc"] == sessions.iso(H.et(D1, 10, 30))
    assert ok["latest_bar"]["available_at_utc"] == sessions.iso(H.et(D1, 10, 30) + 5)
    assert ok["version_tuple"]["strategy_family"] == "pa_brooks" and ok["version_tuple"]["engine_version"]
    assert ok["decision_metadata"]["skip_reason"] == "FEATURE_UNAVAILABLE:atr14"      # 当日只有 12 根，ATR 还没预热

    code, out = run(env, capsys, "scan", "--as-of", sessions.iso(H.et(D1, 12, 0)), "--json")
    bad = json.loads(out)
    assert code == 0 and bad["signal_status"] == "DATA_ERROR"
    assert bad["problems"] == [{"category": "UNEXPECTED_GAP", "start_utc": sessions.iso(H.et(D1, 11, 0)),
                                "end_utc": sessions.iso(H.et(D1, 11, 5)), "detail": "缺 1 根 K 线"}]

    code, text = run(env, capsys, "scan", "--as-of", sessions.iso(H.et(D1, 12, 0)))
    assert "[DATA_ERROR]" in text and "事实：" in text and "决策：DATA_ERROR" in text and "数据：快照 snap_" in text
    assert "[2025-11-25 12:00:00 ET] [pa_brooks:ES:RTH:5m] [ESZ2025]" in text and "不构成投资建议" in text

    assert env.con.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
    assert env.con.execute("SELECT COUNT(*) FROM paper_fills").fetchone()[0] == 0
    assert {r[0] for r in env.con.execute("SELECT DISTINCT run_mode FROM scan_events")} == {"fixture"}
    assert env.settings.release_db.read_bytes() == rel_before               # 不碰发布库
    from trading_agent import db
    rel = db.connect(env.settings.release_db, readonly=True)
    assert rel.execute("SELECT COUNT(*) FROM active_pointers").fetchone()[0] == 0
    rel.close()


def test_scan_with_no_data(env, capsys):
    code, out = run(env, capsys, "scan", "--as-of", sessions.iso(H.et(D1, 12, 0)), "--json")
    r = json.loads(out)
    assert r["signal_status"] == "DATA_ERROR" and r["problems"][0]["category"] == "NO_DATA" and r["source"] is None


def test_module_entry_point():
    import subprocess
    import sys
    p = subprocess.run([sys.executable, "-m", "trading_agent", "review"], capture_output=True, text=True, cwd=H.PROJECT)
    assert p.returncode == 3 and json.loads(p.stdout)["status"] == "NOT_IMPLEMENTED"
    assert cli.COMMANDS["replay"] == 5 and cli.COMMANDS["review"] == "6A"
