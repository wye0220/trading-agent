#!/bin/zsh
# 以评测身份（trading-eval）运行 evaluate，例如：
#   zsh ops/isolation/as-evaluator.sh --accept-digests
#   zsh ops/isolation/as-evaluator.sh --screen cand_…
# sudo 会要你的密码：评测身份只能由你本人启动，代理（包括 Claude）跑不了这一步。
set -eu
PROJECT=/Users/wenye/trading-agent
cd "$PROJECT"
umask 022                                     # 运行库文件本身已是组可写；摘要清单必须是 644
exec sudo -u trading-eval env -i HOME=/var/empty PATH=/usr/bin:/bin \
  TRADING_AGENT_DATA_ROOT=/Users/wenye/trading-agent-data PYTHONDONTWRITEBYTECODE=1 \
  "$PROJECT/.venv/bin/python" -m trading_agent evaluate "$@"
