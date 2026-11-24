#!/bin/zsh
# 盘中：先读本地 TradingView 线（主来源，2026-11-24 起），本地当天没有线时才经 claude -p 取 Webull，然后跑一次节拍。
# 只在交易日 09:30–16:10 ET 内做事；其余时间直接退出（不起 claude 会话）。
# 开销：平时不起 claude 会话；只有用 Webull 备用时每 5 分钟一个。取不到数时节拍照样运行，行情落后会记 LATE_BAR／DATA_STALE 告警，不会产生错误信号。
set -u
PROJECT="${TRADING_AGENT_PROJECT:-/Users/wenye/trading-agent}"
DATA="${TRADING_AGENT_DATA_ROOT:-$HOME/trading-agent-data}"
export TZ=America/New_York
NOW=$(date +%H%M); DOW=$(date +%u)
[[ $DOW -le 5 && $NOW -ge 0930 && $NOW -le 1610 ]] || exit 0
LOGDIR="$DATA/logs/ops"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/$(date +%F)-intraday.log"
cd "$PROJECT" || exit 2
UV="${UV_BIN:-$HOME/.local/bin/uv}"
# 按交易日历：休市日、提前收盘之后都不起 claude 会话（只在 RTH 开盘到收盘后 10 分钟内做事）
"$UV" run python -c "
import sys, time
from trading_agent import sessions
d = sessions.trading_day_at(int(time.time()))
o, c = sessions.rth_bounds(d) if d and sessions.is_trading_day(d) else (0, 0)
sys.exit(0 if o <= time.time() <= c + 600 else 1)" 2>>"$LOG" || exit 0
# 1) 本地 TradingView 线（主来源，只读）：等 price-feed 这一轮的收据出现（每 2 秒查一次，最多 90 秒，整点 180 秒）
FEED="${TRADING_AGENT_TVFEED_ROOT:-/Users/wenye/trading-desk/state/price-feed}"
M=$(date +%M); RND="$(date +%H)$(printf %02d $(( 10#$M / 5 * 5 )))"
RECEIPT="$FEED/$(date +%F)/round-$RND.json"
WAIT=90; [[ "${RND: -2}" == "00" ]] && WAIT=180
for (( i = 0; i < WAIT; i += 2 )); do [[ -f "$RECEIPT" ]] && break; sleep 2; done
[[ -f "$RECEIPT" ]] || echo "[$(date '+%F %T')] 本地线这一轮（$RND）没有等到，用上一轮的数据" >>"$LOG"
"$UV" run python -m trading_agent collect --from-tvfeed >>"$LOG" 2>&1 || echo "[$(date '+%F %T')] 本地线读取失败" >>"$LOG"

# 2) Webull（备用）：只有本地线当天没有 RTH 线（price-feed 没跑，或到期周不用本地线）才起 claude 会话
if "$UV" run python -m trading_agent collect --need-webull >>"$LOG" 2>&1; then
  REQ=$("$UV" run python -m trading_agent collect --mcp-request 2>>"$LOG") || { echo "算不出请求参数" >>"$LOG"; REQ=""; }
  if [[ -n "$REQ" ]]; then
    PROMPT="盘中期货 K 线采集。1) 用 Webull 连接器的 get_futures_bars 取数，参数：$REQ（括号里的交易日只是说明，不是参数）。返回会被存成文件；不要读取、打印或总结里面的价格。2) 运行：uv run python -m trading_agent collect --from-mcp <该文件的路径>。3) 只输出 collect 的结果。不做任何别的事。连接器不可用时输出 CONNECTOR_UNAVAILABLE。"
    claude -p "$PROMPT" --allowedTools "ToolSearch" "mcp__claude_ai_Webull__get_futures_bars" \
      "Bash(uv run python -m trading_agent collect --from-mcp:*)" --model sonnet </dev/null >>"$LOG" 2>&1
    echo "[$(date '+%F %T')] claude 退出码 $?" >>"$LOG"
  fi
fi

# 3) 节拍：有新收线的 5 分钟线才扫描
exec "$PROJECT/ops/launchd/run-ops.sh" tick
