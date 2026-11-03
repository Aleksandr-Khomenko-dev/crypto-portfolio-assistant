#!/bin/zsh
# Gold bot as macOS LaunchAgents: started at login, restarted after a crash.
#
#   scripts/gold_bot.sh install    write both LaunchAgents and start them
#   scripts/gold_bot.sh status     running? last scanner cycle
#   scripts/gold_bot.sh logs       follow the runner log (Ctrl+C stops following only)
#   scripts/gold_bot.sh restart    restart both (e.g. after changing .env)
#   scripts/gold_bot.sh stop       stop both until `start` or the next login
#   scripts/gold_bot.sh start      start both again
#   scripts/gold_bot.sh uninstall  stop and remove the LaunchAgents (no autostart)
#
# runner = python -m app.scanner.runner under `caffeinate -i -s` (the Mac does not
# idle-sleep while it runs; closing the lid on battery still sleeps). Its log
# logs/runner.log rotates at 20 MB (5 old files kept); crashes go to runner.stderr.log.
# api    = uvicorn on 127.0.0.1:8000 (TradingView webhook, dashboard, bot commands).
set -u
ROOT="${0:A:h:h}"
PY="$ROOT/.venv/bin/python"
DOMAIN="gui/$(id -u)"
DIR="$HOME/Library/LaunchAgents"
RUNNER=com.aleksandrhomenko.gold-bot-runner
API=com.aleksandrhomenko.gold-bot-api
AGENTS=($RUNNER $API)

plist() {  # label, stdout/stderr file, extra env (KEY=VALUE or ""), program arguments...
  local label=$1 log=$2 extra=$3; shift 3
  local args="" a
  for a in "$@"; do args+="        <string>${a}</string>"$'\n'; done
  cat > "$DIR/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
$args    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
$( [ -n "$extra" ] && printf '        <key>%s</key>\n        <string>%s</string>' "${extra%%=*}" "${extra#*=}" )
    </dict>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$ROOT/logs/$log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/logs/$log</string>
</dict>
</plist>
EOF
}

loaded() { launchctl print $DOMAIN/$1 >/dev/null 2>&1; }

start() {
  local a i
  for a in $AGENTS; do
    if loaded $a; then launchctl kickstart $DOMAIN/$a; continue; fi
    for i in 1 2 3 4 5; do  # bootstrap fails while a previous instance unloads
      launchctl bootstrap $DOMAIN "$DIR/$a.plist" 2>/dev/null && break
      sleep 2
    done
    loaded $a || echo "FAILED to start $a"
  done
}

stop() {
  local a i
  for a in $AGENTS; do launchctl bootout $DOMAIN/$a 2>/dev/null; done
  # bootout returns before the process exits (the API can take ~20 s): wait for it.
  for i in $(seq 1 40); do
    loaded $RUNNER || loaded $API || pgrep -f "app.scanner.runner|uvicorn app.main" >/dev/null || return 0
    sleep 1
  done
  return 0
}

case "${1:-status}" in
  install)
    if pgrep -f "app.scanner.runner|uvicorn app.main" >/dev/null && ! loaded $RUNNER; then
      echo "A manually started bot is running; stop it first:"
      echo '  pkill -f app.scanner.runner; pkill -f "uvicorn app.main"'
      exit 1
    fi
    mkdir -p "$DIR" "$ROOT/logs"
    # The runner rotates logs/runner.log itself; launchd keeps only crash output.
    plist $RUNNER runner.stderr.log "CPDA_LOG_FILE=$ROOT/logs/runner.log" \
      /usr/bin/caffeinate -i -s "$PY" -m app.scanner.runner
    plist $API api.log "" "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
    plutil -lint "$DIR/$RUNNER.plist" "$DIR/$API.plist" >/dev/null || exit 1
    stop; start
    echo "Installed and started: $AGENTS"
    ;;
  start) start ;;
  stop) stop; echo "Stopped (starts again at the next login; 'uninstall' removes autostart)" ;;
  restart) for a in $AGENTS; do launchctl kickstart -k $DOMAIN/$a; done ;;
  status)
    for a in $AGENTS; do
      if loaded $a; then
        echo "$a: $(launchctl print $DOMAIN/$a | grep -E '^\s*(state|pid) =' | tr -s ' ' | tr '\n' ' ')"
      else
        echo "$a: not loaded"
      fi
    done
    grep -E "Cycle " "$ROOT/logs/runner.log" 2>/dev/null | tail -1 | cut -c1-90
    ;;
  logs) tail -F "$ROOT/logs/runner.log" ;;
  uninstall)
    stop
    rm -f "$DIR/$RUNNER.plist" "$DIR/$API.plist"
    echo "Removed; the bot no longer starts at login"
    ;;
  *) echo "usage: $0 install|start|stop|restart|status|logs|uninstall"; exit 2 ;;
esac
