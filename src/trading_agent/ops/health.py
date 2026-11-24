"""健康检查与恢复命令。"""
from __future__ import annotations

import sqlite3
import time

from . import state

RECOVERY = {
    "失去行情或迟到": ["python -m trading_agent collect --check-day <YYYY-MM-DD>   # 看缺哪几根",
                   "ops/launchd/run-collect-mcp.sh <YYYY-MM-DD>               # 重新采集当天（收盘后）",
                   "python -m trading_agent ops --tick                         # 补上之后再推进一次（落下的信号只进历史）"],
    "停机或睡眠之后": ["python -m trading_agent ops --health",
                  "python -m trading_agent ops --tick                         # 补算落下的线，不恢复过期挂单",
                  "python -m trading_agent ops --daily --day <YYYY-MM-DD>     # 补跑漏掉的盘后流水"],
    "重复运行": ["节拍有运行锁和水位：重复运行返回 LOCKED 或 NO_NEW_BAR，不会重复扫描",
             "锁卡住（进程已死）时等它过期（ops.yaml 的 lock_ttl_seconds），或 ops --health 看持有者"],
    "控制通道离线": ["python -m trading_agent control --exec \"freeze ALL 通道离线\"   # 本机命令行通道",
               "python -m trading_agent freeze --reason ...                # 直接用命令"],
    "券商只读接口失效": ["python -m trading_agent watch --once                    # 看报错",
                  "python -m trading_agent ingest --alpaca                     # 接口恢复后补导成交",
                  "watch 停止期间持仓没有盘中提醒：ops --health 会报 WATCH_STOPPED"],
    "死信队列": ["python -m trading_agent ops --dlq                            # 列出",
             "python -m trading_agent ops --dlq-replay <task_id|ALL>      # 放回队列",
             "python -m trading_agent ops --explain                        # 重新处理"],
}


def report(con: sqlite3.Connection, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    st = state.all_state(con)
    hb = {k.split(":", 1)[1]: {**v["value"], "age_seconds": now - v["updated_at_utc"]}
          for k, v in st.items() if k.startswith("heartbeat:")}
    wm = {k.split(":", 1)[1]: v["value"] for k, v in st.items() if k.startswith("watermark:")}
    watch = [{"account_id": a, "age_seconds": now - int((__import__("json").loads(v)).get("at_utc") or 0)}
             for a, v in con.execute("SELECT account_id, value_json FROM watch_state WHERE key='heartbeat'")]
    dlq = con.execute("SELECT COUNT(*) FROM dead_letter_queue WHERE status='DEAD'").fetchone()[0]
    pending = con.execute("SELECT COUNT(*) FROM explanation_requests WHERE status='PENDING'").fetchone()[0]
    undelivered = con.execute("SELECT COUNT(*) FROM alerts a LEFT JOIN alert_deliveries d ON d.alert_id=a.alert_id"
                              " WHERE d.alert_id IS NULL").fetchone()[0]
    recent = [dict(r) for r in con.execute("SELECT alert_id, level, kind, scope, message, created_at_utc FROM alerts"
                                           " WHERE level IN ('CRITICAL','WARN') ORDER BY alert_id DESC LIMIT 5")]
    return {"status": "OK", "as_of_utc": now, "heartbeats": hb, "watermarks": wm, "watch": watch,
            "locks": state.locks(con, now), "dead_letters": dlq, "pending_explanations": pending,
            "undelivered_alerts": undelivered, "recent_alerts": recent,
            "watch_interval_override": (state.get(con, "watch_interval_override") or {}).get("seconds"),
            "recovery": RECOVERY}
