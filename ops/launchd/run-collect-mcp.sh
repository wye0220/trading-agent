#!/bin/zsh
# 每日期货 K 线采集（流水任务）：无人值守地让 Claude Code 调 Webull MCP 连接器取 ES、NQ 的 5 分钟线，
# 再由 collect --from-mcp 落盘。结果由本脚本自己核对（不看 Claude 怎么说）；失败或失效时发 Telegram。
#
# 只读行情。允许 Claude 使用的工具只有：取期货 K 线的连接器、本项目的 collect 命令、工具检索。
# 用法：run-collect-mcp.sh [YYYY-MM-DD]   缺省核对今天（纽约时间）。
set -u
PROJECT="${TRADING_AGENT_PROJECT:-/Users/wenye/trading-agent}"
DATA="${TRADING_AGENT_DATA_ROOT:-$HOME/trading-agent-data}"
export TZ=America/New_York
DAY="${1:-$(date +%F)}"
LOGDIR="$DATA/logs/collect-mcp"
LOG="$LOGDIR/$DAY.log"
mkdir -p "$LOGDIR"
cd "$PROJECT" || exit 2
notify() { "$PROJECT/ops/notify-telegram.sh" "$1" >>"$LOG" 2>&1; }
fail() {
  echo "[$(date '+%F %T')] 失败：$1" >>"$LOG"
  notify "${TRADING_AGENT_NOTIFY_TAG:-}trading-agent 每日采集失败（$DAY）：$1。日志：$LOG"
  exit 1
}
echo "[$(date '+%F %T')] 开始，核对交易日 $DAY" >>"$LOG"

# 不是交易日就安静退出
if uv run python -m trading_agent collect --check-day "$DAY" --json 2>>"$LOG" | grep -q '"is_trading_day": false'; then
  echo "[$(date '+%F %T')] 不是交易日，退出" >>"$LOG"; exit 0
fi

REQ=$(uv run python -m trading_agent collect --mcp-request 2>>"$LOG") || fail "算不出请求参数"
PROMPT="每日期货 K 线采集。1) 用 Webull 连接器的 get_futures_bars 取数，参数：$REQ（括号里的交易日只是说明，不是参数）。返回很大，会被自动存成文件；不要读取、打印或总结文件里的价格。2) 运行：uv run python -m trading_agent collect --from-mcp <该文件的路径>。3) 只输出 collect 的结果。不做任何别的事，不改任何文件。连接器不可用或要求登录时，直接输出 CONNECTOR_UNAVAILABLE 和原因。"

claude -p "$PROMPT" \
  --allowedTools "ToolSearch" "mcp__claude_ai_Webull__get_futures_bars" "Bash(uv run python -m trading_agent collect --from-mcp:*)" \
  --model sonnet </dev/null >>"$LOG" 2>&1
rc=$?
echo "[$(date '+%F %T')] claude 退出码 $rc" >>"$LOG"

# 独立核对：当天两个品种的文件必须覆盖全部 RTH 5 分钟线
CHECK=$(uv run python -m trading_agent collect --check-day "$DAY" 2>>"$LOG")
ok=$?
echo "$CHECK" >>"$LOG"
if [[ $ok -ne 0 ]]; then
  if [[ $rc -ne 0 ]]; then why="Claude Code 没有跑成功（退出码 $rc，可能是登录或连接器授权失效）"
  elif grep -q CONNECTOR_UNAVAILABLE "$LOG"; then why="Webull 连接器不可用（可能授权失效）"
  else why="数据不完整"; fi
  fail "$why；${CHECK//$'\n'/；}"
fi
echo "[$(date '+%F %T')] 完成" >>"$LOG"
exit 0
