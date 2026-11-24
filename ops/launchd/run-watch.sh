#!/bin/zsh
# 盘中常驻 watch：交易日 09:25 由 launchd 启动，16:05 ET 自己退出。间隔按 控制通道 > Memory 偏好 > config/watch.yaml。
set -u
PROJECT="${TRADING_AGENT_PROJECT:-/Users/wenye/trading-agent}"
DATA="${TRADING_AGENT_DATA_ROOT:-$HOME/trading-agent-data}"
export TZ=America/New_York
[[ $(date +%u) -le 5 ]] || exit 0
LOGDIR="$DATA/logs/ops"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/$(date +%F)-watch.log"
cd "$PROJECT" || exit 2
"${UV_BIN:-$HOME/.local/bin/uv}" run python -m trading_agent watch >>"$LOG" 2>&1 &
PID=$!
END=$(date -j -f "%H%M" "1605" +%s); NOWS=$(date +%s)
(( END > NOWS )) && sleep $(( END - NOWS ))
kill $PID 2>/dev/null
exit 0
