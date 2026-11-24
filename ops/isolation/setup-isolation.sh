#!/bin/zsh
# 隔离设置（阶段 8 的遗留）。要管理员权限，由你本人运行一次：sudo zsh ops/isolation/setup-isolation.sh
# 加 --dry-run 只打印要做的事，不改任何东西。可以重复运行（已完成的步骤会跳过）。
#
# 做的事：
#   1. 建隐藏的系统用户 trading-eval（不能登录），作为评测身份
#   2. 建组 trading-agent，把你和 trading-eval 都加进去；运行库目录和评测报告目录对这个组可写
#      （评测要往运行库写登记、解封账本和候选台账）
#   3. 建保留集 /Users/Shared/trading-holdout（放在 /Users/Shared：那里有粘滞位，你删不了别人的目录）：
#        目录本身 711：你能按确切路径进入，但不能列出内容
#        protected/ 755、摘要清单 644：发布批准时你要能读到它，但不能改
#        data/、evaluation/ 和 CANARY 只有 trading-eval 能读
#   4. 把含截止日之后数据的原始行情移进 data/：两批 Databento 文件，以及 2026-11-23（含）之前的 Webull 封存日。
#      2026-11-24 起放行的 Webull 实时行情留在原处。截止日之前的 K 线已另存为
#      ~/trading-agent-data/databento/dev-extract/*.csv（已核对与运行库一致）
#   5. 旧的 ~/trading-holdout（如果有）里的内容并入新位置的 legacy/；新位置如果已存在、又不属于评测身份，
#      说明是测试或作者身份留下的，整个改名为 trading-holdout-stale-<时间> 留存，不信任
set -euo pipefail
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1
AUTHOR=wenye
EVAL=trading-eval
GROUP=trading-agent
HOLD=/Users/Shared/trading-holdout
DATA=/Users/$AUTHOR/trading-agent-data
PROJECT=/Users/$AUTHOR/trading-agent
LIVE_FROM=2026-11-24

run() { echo "+ $*"; (( DRY )) || "$@"; }
if (( ! DRY )) && [[ $EUID -ne 0 ]]; then echo "要用 sudo 运行：sudo zsh $0"; exit 1; fi

echo "== 1. 评测用户 $EVAL"
if id "$EVAL" >/dev/null 2>&1; then echo "已存在"; else
  # 随机密码，不显示、不保存：这个用户不能登录，只能由你用 sudo -u 启动
  echo "+ sysadminctl -addUser $EVAL -fullName 'trading-agent evaluator' -shell /usr/bin/false -home /var/empty -password <随机，不显示>"
  (( DRY )) || sysadminctl -addUser "$EVAL" -fullName "trading-agent evaluator" -shell /usr/bin/false \
      -home /var/empty -password "$(openssl rand -base64 30)"
  run dscl . create "/Users/$EVAL" IsHidden 1
fi

echo "== 2. 组 $GROUP"
dscl . -read "/Groups/$GROUP" >/dev/null 2>&1 || run dseditgroup -o create -r "trading-agent shared runtime" "$GROUP"
run dseditgroup -o edit -a "$AUTHOR" -t user "$GROUP"
run dseditgroup -o edit -a "$EVAL" -t user "$GROUP"
run chgrp -R "$GROUP" "$DATA/runtime"
run chmod -R g+rwX "$DATA/runtime"
run chmod g+s "$DATA/runtime"
run mkdir -p "$PROJECT/reports/evaluations"
run chgrp "$GROUP" "$PROJECT/reports/evaluations"
run chmod 2775 "$PROJECT/reports/evaluations"

echo "== 3. 保留集 $HOLD"
if [[ -e "$HOLD" ]] && [[ "$(stat -f %Su "$HOLD")" != "$EVAL" ]]; then
  # 不是评测身份建的（例如测试或作者身份留下的）：里面的东西不可信，整个改名留存，重新建一个干净的
  STALE="$HOLD-stale-$(date +%Y%m%d%H%M%S)"
  run mv "$HOLD" "$STALE"
  run chown -R "${EVAL}:staff" "$STALE"
  run chmod -R go-rwx "$STALE"
fi
run mkdir -p "$HOLD/data/databento" "$HOLD/data/webull" "$HOLD/protected" "$HOLD/evaluation"
if [[ -d "/Users/$AUTHOR/trading-holdout" && ! -L "/Users/$AUTHOR/trading-holdout" ]]; then
  echo "+ 并入旧的 /Users/$AUTHOR/trading-holdout"
  (( DRY )) || { rsync -a "/Users/$AUTHOR/trading-holdout/" "$HOLD/legacy/" && rm -rf "/Users/$AUTHOR/trading-holdout"; }
fi
(( DRY )) || [[ -f "$HOLD/CANARY" ]] || print "保留集隔离验收用的探针文件，作者身份不应能读到它" > "$HOLD/CANARY"

echo "== 4. 封存的原始行情移进 $HOLD/data"
for b in "$DATA"/databento/GLBX-*(N/); do run mv "$b" "$HOLD/data/databento/"; done
for f in "$DATA"/webull/*/*/*.csv(N); do
  d=${f:t:r}
  if [[ "$d" < "$LIVE_FROM" ]]; then
    rel=${f#$DATA/webull/}
    run mkdir -p "$HOLD/data/webull/${rel:h}"
    run mv "$f" "$HOLD/data/webull/$rel"
  fi
done

echo "== 5. 属主与权限"
run chown -R "${EVAL}:staff" "$HOLD"
run chmod -R go-rwx "$HOLD"
run chmod 711 "$HOLD"
run chmod 755 "$HOLD/protected"
[[ -f "$HOLD/protected/evaluator-manifest.json" ]] && run chmod 644 "$HOLD/protected/evaluator-manifest.json"
echo
echo "完成。接下来（不用 sudo 的部分）："
echo "  1) 退出终端再打开一次（组成员身份要重新登录才生效）"
echo "  2) 评测身份写摘要清单：zsh ops/isolation/as-evaluator.sh --accept-digests"
echo "  3) 作者身份验收：uv run python -m trading_agent evaluate --isolation-check"
