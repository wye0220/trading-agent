#!/bin/zsh
# 运行一次 Webull 只读采集。launchd 样例调用它；也可以手工运行。
# 等到当前 5 分钟整点之后 WAIT 秒再取数，保证上一根 K 线已收线。
set -u
cd "$(dirname "$0")/../.." || exit 1
WAIT=${COLLECT_WAIT_SECONDS:-10}
sleep "$WAIT"
exec "${UV_BIN:-$HOME/.local/bin/uv}" run python -m trading_agent collect --json
