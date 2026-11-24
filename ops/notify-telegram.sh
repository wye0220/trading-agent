#!/bin/zsh
# 发一条 Telegram 消息。用法：notify-telegram.sh "消息正文"
# 凭证在项目之外，按顺序找第一个同时有 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID 的文件：
#   $TRADING_AGENT_TELEGRAM_ENV、~/trading-agent-data/secrets/telegram.env、~/trading-desk/.env
# （2026-11-23 用户同意复用 trading-desk 的机器人；这里只解析这两个键，不复制，也不执行整个文件。）
# 只打印 HTTP 状态码，不打印 token。发不出去不算调用方失败：返回 0，并退回到本机通知。
msg="$1"
get() {   # get <文件> <键>
  awk -F= -v k="$2" '
    /^[[:space:]]*#/ {next}
    $1 ~ "^[[:space:]]*(export[[:space:]]+)?"k"[[:space:]]*$" {
      sub(/^[^=]*=/,""); gsub(/\r/,""); gsub(/^[[:space:]]*["'"'"']?|["'"'"']?[[:space:]]*;?[[:space:]]*$/,"");
      print; exit }' "$1"
}
fallback() {
  /usr/bin/osascript -e "display notification \"${msg//\"/\\\"}\" with title \"trading-agent\"" >/dev/null 2>&1
  echo "telegram: $1（已改发本机通知）"
}
token="" chat=""
for f in "${TRADING_AGENT_TELEGRAM_ENV:-}" "$HOME/trading-agent-data/secrets/telegram.env" "$HOME/trading-desk/.env"; do
  [[ -n "$f" && -f "$f" ]] || continue
  token=$(get "$f" TELEGRAM_BOT_TOKEN); chat=$(get "$f" TELEGRAM_CHAT_ID)
  [[ -n "$token" && -n "$chat" ]] && break
done
if [[ -z "$token" || -z "$chat" ]]; then fallback "NOT_CONFIGURED 没有找到 Telegram 凭证"; exit 0; fi
code=$(/usr/bin/curl -sS -m 20 -o /dev/null -w '%{http_code}' \
  "https://api.telegram.org/bot${token}/sendMessage" \
  --form-string "chat_id=${chat}" --form-string "text=${msg}" 2>/dev/null)
if [[ "$code" == "200" ]]; then echo "telegram: HTTP 200"; else fallback "HTTP ${code:-无响应}"; fi
exit 0
