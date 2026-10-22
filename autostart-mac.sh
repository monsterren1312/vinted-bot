#!/bin/bash
# Installs (or with "remove" uninstalls) launchd agents that run at login and restart on crashes:
#   com.vinted-bot         — the bot
#   com.vinted-bot.tunnel  — cloudflared quick tunnel for the Mini App (kept separate so restarting
#                            the bot doesn't change the app's public URL)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
BOT=com.vinted-bot
TUNNEL=com.vinted-bot.tunnel

for label in $BOT $TUNNEL; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
done
if [ "$1" = "remove" ]; then
  rm -f "$AGENTS/$BOT.plist" "$AGENTS/$TUNNEL.plist"
  echo "Автозапуск удалён, бот и туннель остановлены."
  exit 0
fi

if ! grep -qE '^TELEGRAM_TOKEN=[0-9]{6,}:[A-Za-z0-9_-]{30,}' "$DIR/.env" 2>/dev/null; then
  echo "Сначала впиши токен бота в $DIR/.env (TELEGRAM_TOKEN=...)"
  exit 1
fi
[ -x "$DIR/.venv/bin/python" ] || { python3 -m venv "$DIR/.venv" && "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"; }
mkdir -p "$DIR/data" "$AGENTS"
sleep 2  # let launchd finish unloading before loading again

agent() {  # agent <label> <log file> <program> [args...]
  local label=$1 logfile=$2; shift 2
  local args=""
  for a in "$@"; do args="$args<string>$a</string>"; done
  cat > "$AGENTS/$label.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$logfile</string>
  <key>StandardErrorPath</key><string>$logfile</string>
</dict>
</plist>
PL
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
}

if [ -x "$DIR/bin/cloudflared" ]; then
  agent $TUNNEL "$DIR/data/tunnel.log" "$DIR/bin/cloudflared" tunnel --no-autoupdate \
    --metrics 127.0.0.1:20241 --url http://127.0.0.1:8787
else
  echo "bin/cloudflared не найден — бот будет работать без приложения (см. README)."
fi
agent $BOT "$DIR/data/bot.log" /bin/bash "$DIR/run.sh"

echo "Готово: бот запущен и будет стартовать сам при входе в систему."
echo "Лог: $DIR/data/bot.log   Удалить автозапуск: ./autostart-mac.sh remove"
