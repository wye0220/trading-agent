#!/bin/zsh
# 拉取 Telegram 控制通道的新消息并执行（config/control.yaml 里启用并绑定用户之后才会做事）。
set -u
PROJECT="${TRADING_AGENT_PROJECT:-/Users/wenye/trading-agent}"
DATA="${TRADING_AGENT_DATA_ROOT:-$HOME/trading-agent-data}"
LOGDIR="$DATA/logs/ops"; mkdir -p "$LOGDIR"
cd "$PROJECT" || exit 2
exec "${UV_BIN:-$HOME/.local/bin/uv}" run python -m trading_agent control --poll >>"$LOGDIR/$(date +%F)-control.log" 2>&1
