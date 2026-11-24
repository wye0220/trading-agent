#!/bin/zsh
# 调度包装：运行一个 ops 动作，输出追加到日志；命令失败（退出码非 0）时推一条 Telegram。
# 用法：run-ops.sh tick|daily|weekly|dispatch|watch-check|explain
set -u
PROJECT="${TRADING_AGENT_PROJECT:-/Users/wenye/trading-agent}"
DATA="${TRADING_AGENT_DATA_ROOT:-$HOME/trading-agent-data}"
export TZ=America/New_York
ACTION="${1:?用法：run-ops.sh <动作>}"
LOGDIR="$DATA/logs/ops"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/$(date +%F)-$ACTION.log"
cd "$PROJECT" || exit 2
echo "[$(date '+%F %T')] ops --$ACTION" >>"$LOG"
"${UV_BIN:-$HOME/.local/bin/uv}" run python -m trading_agent ops "--$ACTION" >>"$LOG" 2>&1
rc=$?
if [[ $rc -ne 0 ]]; then
  "$PROJECT/ops/notify-telegram.sh" "trading-agent ops --$ACTION 失败（退出码 $rc）。日志：$LOG" >>"$LOG" 2>&1
fi
exit $rc
