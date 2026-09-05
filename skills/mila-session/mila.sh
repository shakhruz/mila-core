#!/bin/bash
# mila — start a Claude Code session with the mila-telegram plugin.
# The full launch flag is too long to type by hand:
#   claude --dangerously-load-development-channels plugin:mila-telegram@mila-marketplace
# This wrapper also makes sure the channel is alive before the session starts.

set -u
PLUGIN="plugin:mila-telegram@mila-marketplace"
FLAG="--dangerously-load-development-channels"
STATE="${TELEGRAM_STATE_DIR:-$HOME/.claude/channels/telegram}"
# Service names are configurable: whoever installs this names their own
# launchd/systemd units. Defaults match the ones the installer suggests.
DAEMON_UNIT="${MILA_DAEMON_UNIT:-com.milacore.telegram-receiver}"
SENDER_UNIT="${MILA_SENDER_UNIT:-com.milacore.telegram-sender}"

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

# The channel must be up BEFORE the session starts: while no session is running,
# the daemon keeps collecting inbound messages into the journal, so none are lost.
ensure_daemon() {
  if ! pgrep -f "receiver.ts" >/dev/null 2>&1; then
    echo "${c_dim}receiver is down — starting it${c_off}"
    launchctl kickstart -k "gui/$(id -u)/$DAEMON_UNIT" 2>/dev/null \
      || launchctl load "$HOME/Library/LaunchAgents/$DAEMON_UNIT.plist" 2>/dev/null
    sleep 2
  fi
  pgrep -f "sender-daemon" >/dev/null 2>&1 || \
    launchctl kickstart -k "gui/$(id -u)/$SENDER_UNIT" 2>/dev/null
}

status() {
  local cur sz behind q
  cur=$(cat "$STATE/inbound/cursor" 2>/dev/null || echo 0)
  sz=$(wc -c < "$STATE/inbound/events.jsonl" 2>/dev/null | tr -d ' ' || echo 0)
  echo "── mila-telegram channel ──"
  if pgrep -f "receiver.ts" >/dev/null 2>&1; then
    echo "  receiver   ${c_ok}up${c_off}     $(pgrep -f receiver.ts | head -1)"
  else
    echo "  receiver   ${c_bad}DOWN${c_off}   inbound messages are being lost"
  fi
  if pgrep -f "sender-daemon" >/dev/null 2>&1; then
    echo "  sender     ${c_ok}up${c_off}"
  else
    echo "  sender     ${c_bad}DOWN${c_off}   replies are piling up in the outbox"
  fi
  if pgrep -f "bun server.ts" >/dev/null 2>&1; then
    echo "  bridge     ${c_ok}up${c_off}"
  else
    echo "  bridge     ${c_dim}no session${c_off}"
  fi
  behind=$(( sz - cur ))
  if [ "$behind" -le 0 ]; then
    echo "  journal    ${c_ok}replayed${c_off} (${sz} B)"
  else
    echo "  journal    ${c_bad}${behind} B not replayed${c_off} (cursor ${cur} / ${sz})"
  fi
  q=$(ls "$STATE/outbox" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$q" != "0" ]; then echo "  outbox     ${c_bad}${q} pending${c_off}"; else echo "  outbox     ${c_ok}empty${c_off}"; fi
}

case "${1:-continue}" in
  status|st)   status ;;
  fix)         launchctl kickstart -k "gui/$(id -u)/$DAEMON_UNIT" 2>/dev/null
               launchctl kickstart -k "gui/$(id -u)/$SENDER_UNIT" 2>/dev/null
               sleep 2; status ;;
  new|n)       ensure_daemon; shift; exec claude $FLAG "$PLUGIN" "$@" ;;
  pick|p)      ensure_daemon; shift; exec claude $FLAG "$PLUGIN" --resume "$@" ;;
  help|-h|--help)
               cat <<'HELP'
mila            resume the last session with the plugin (--continue)
mila new        start a fresh session
mila pick       choose a session from the list (--resume)
mila status     channel health: receiver, sender, bridge, journal, outbox
mila fix        restart receiver and sender (leaves the session alone)

Anything after the command is passed to claude as-is:  mila new --model opus
HELP
               ;;
  *)           ensure_daemon; exec claude $FLAG "$PLUGIN" --continue "$@" ;;
esac
