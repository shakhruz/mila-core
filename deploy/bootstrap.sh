#!/usr/bin/env bash
# Mila Core — bootstrap a fresh Linux server into a working assistant host.
#
# Run as root on a clean Ubuntu/Debian box. Idempotent: safe to run twice, it
# skips what is already in place and says so. Nothing here is irreversible
# without saying it first.
#
#   ./bootstrap.sh --user mila --dry-run     # see every step, change nothing
#   ./bootstrap.sh --user mila
#
# What it does NOT do, on purpose:
#   · never logs anyone into Claude Code — that is the client's own account and
#     their own subscription, and it must be their hands on the keyboard
#   · never writes a bot token — that comes from the client via BotFather
#   · never opens a port to the world
#
# What it leaves you with: a dedicated unprivileged user, the runtimes, the kit,
# a systemd unit for the receiver daemon, hardened permissions, and a printed
# list of the three things a human still has to do.
set -euo pipefail

USER_NAME="mila"
DRY=0
KIT_SRC=""
SSH_KEY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)    USER_NAME="$2"; shift 2 ;;
    --kit)     KIT_SRC="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

HOME_DIR="/home/$USER_NAME"
STATE_DIR="$HOME_DIR/.claude/channels/telegram"
say()  { printf '  %s\n' "$*"; }
head() { printf '\n\033[1m%s\033[0m\n' "$*"; }
run()  { if [[ $DRY -eq 1 ]]; then say "would: $*"; else "$@"; fi; }
asuser() { if [[ $DRY -eq 1 ]]; then say "would (as $USER_NAME): $*"; else su - "$USER_NAME" -c "$*"; fi; }

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -f /etc/os-release ]] || { echo "no /etc/os-release — unsupported system" >&2; exit 1; }
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) echo "tested on Ubuntu/Debian only; found: ${PRETTY_NAME:-$ID}" >&2
     echo "proceed manually or adapt this script" >&2; exit 1 ;;
esac

echo "Mila Core bootstrap"
say "host: $(hostname) · $PRETTY_NAME"
say "user: $USER_NAME"
[[ $DRY -eq 1 ]] && say "DRY RUN — nothing will change"

# ── packages ───────────────────────────────────────────────────────────────
head "System packages"
NEED=()
for p in curl git python3 rsync unzip ca-certificates; do
  dpkg -s "$p" >/dev/null 2>&1 || NEED+=("$p")
done
if [[ ${#NEED[@]} -gt 0 ]]; then
  say "installing: ${NEED[*]}"
  run apt-get update -qq
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${NEED[@]}"
else
  say "all present"
fi

# ── user ───────────────────────────────────────────────────────────────────
head "User"
if id "$USER_NAME" >/dev/null 2>&1; then
  say "$USER_NAME exists"
else
  # No shell password: this account is reached by key or by root, never by
  # guessing. Home is created because everything below lives in it.
  run useradd --create-home --shell /bin/bash "$USER_NAME"
  run passwd -l "$USER_NAME"
  say "$USER_NAME created, password login disabled"
fi

if [[ -n "$SSH_KEY" ]]; then
  if [[ -f "$SSH_KEY" ]]; then
    run install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.ssh"
    run install -m 600 -o "$USER_NAME" -g "$USER_NAME" "$SSH_KEY" "$HOME_DIR/.ssh/authorized_keys"
    say "ssh key installed"
  else
    say "NOTE: --ssh-key $SSH_KEY not found, skipped"
  fi
fi

# User services must survive logout, otherwise the daemon dies with the SSH
# session and the whole point of a durable receiver is lost.
if loginctl show-user "$USER_NAME" 2>/dev/null | grep -q "Linger=yes"; then
  say "lingering already on"
else
  run loginctl enable-linger "$USER_NAME"
  say "lingering enabled — user services survive logout"
fi

# ── runtimes ───────────────────────────────────────────────────────────────
head "Runtimes"
if [[ $DRY -eq 1 && ! -d "$HOME_DIR" ]]; then
  say "bun — would install (home does not exist yet)"
elif [[ -x "$HOME_DIR/.bun/bin/bun" ]]; then
  say "bun present"
else
  say "installing bun for $USER_NAME"
  asuser "curl -fsSL https://bun.sh/install | bash" >/dev/null 2>&1 || true
  [[ $DRY -eq 1 ]] || {
    [[ -x "$HOME_DIR/.bun/bin/bun" ]] && say "bun installed" || say "🔴 bun install failed — see https://bun.sh"
  }
fi

if command -v node >/dev/null 2>&1; then
  say "node $(node -v 2>/dev/null || true)"
else
  say "installing nodejs"
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs npm
fi

if [[ $DRY -eq 1 ]]; then
  say "claude code — check skipped (user does not exist yet in a dry run)"
elif su - "$USER_NAME" -c "command -v claude" >/dev/null 2>&1; then
  say "claude code present"
else
  say "installing claude code for $USER_NAME"
  asuser "curl -fsSL https://claude.ai/install.sh | bash" >/dev/null 2>&1 || \
    say "NOTE: automatic install failed — install Claude Code manually as $USER_NAME"
fi

# ── kit ────────────────────────────────────────────────────────────────────
head "Kit"
if [[ -z "$KIT_SRC" ]]; then
  KIT_SRC="$HOME_DIR/mila-core"
  if [[ -d "$KIT_SRC/.git" ]]; then
    say "kit already cloned at $KIT_SRC"
  else
    run su - "$USER_NAME" -c "git clone -q https://github.com/shakhruz/mila-core $KIT_SRC"
    say "kit cloned to $KIT_SRC"
  fi
fi
if [[ -x "$KIT_SRC/install/install.sh" ]]; then
  run su - "$USER_NAME" -c "$KIT_SRC/install/install.sh$([[ $DRY -eq 1 ]] && echo ' --dry-run')"
elif [[ $DRY -eq 1 ]]; then
  # В холостом прогоне репозитория ещё нет — это не поломка, а следствие
  # того, что клонирование тоже не выполнялось. Красный маркер здесь пугал бы
  # зря, а испуг в установщике стоит дороже, чем кажется.
  say "installer — would run after the clone above"
else
  say "🔴 no installer at $KIT_SRC/install/install.sh"
fi

# ── state dirs ─────────────────────────────────────────────────────────────
head "State"
run install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$STATE_DIR"
run install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$STATE_DIR/inbound"
run install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$STATE_DIR/chats"
say "$STATE_DIR (0700)"

# ── receiver unit ──────────────────────────────────────────────────────────
head "Receiver daemon"
UNIT_DIR="$HOME_DIR/.config/systemd/user"
UNIT="$UNIT_DIR/mila-telegram-receiver.service"
# The plugin lives under whatever the marketplace is called, and that name is
# not ours to choose: installing from GitHub gives "mila", a local development
# marketplace gives its own directory name. Hardcoding one of them wrote a unit
# pointing at a path the client would never have — and with Restart=always and
# RestartSec=5, systemd's default limiter never trips, so the daemon would spin
# forever, filling receiver.log, while the channel simply never worked.
PLUGIN_DIR="$(ls -d "$HOME_DIR"/.claude/plugins/*/mila-telegram 2>/dev/null | head -1)"
run install -d -m 755 -o "$USER_NAME" -g "$USER_NAME" "$UNIT_DIR"
if [[ -z "$PLUGIN_DIR" || ! -f "$PLUGIN_DIR/receiver.ts" ]]; then
  say "plugin not installed yet — unit NOT written"
  say "install it inside Claude Code first (see the three steps below),"
  say "then re-run this script: it will find the plugin and write the unit"
  PLUGIN_DIR=""
elif [[ $DRY -eq 1 ]]; then
  say "would write $UNIT"
else
  cat > "$UNIT" <<UNITEOF
[Unit]
Description=Mila Telegram receiver (durable inbound journal)
After=network-online.target
Wants=network-online.target

[Service]
# The daemon is the reason a restart costs nothing: it keeps polling while the
# session is down and appends to the journal, which the session replays from a
# cursor. Restart=always because a receiver that quietly dies looks exactly
# like a quiet day.
Type=simple
Environment=RECEIVER_DAEMON=1
ExecStart=$HOME_DIR/.bun/bin/bun $PLUGIN_DIR/receiver.ts
Restart=always
RestartSec=5
StandardOutput=append:$HOME_DIR/.claude/receiver.log
StandardError=append:$HOME_DIR/.claude/receiver.log

[Install]
WantedBy=default.target
UNITEOF
  chown "$USER_NAME:$USER_NAME" "$UNIT"
  chmod 644 "$UNIT"
  say "unit written: $UNIT"
  say "not started — the plugin and token must exist first"
fi

# ── hardening ──────────────────────────────────────────────────────────────
head "Permissions"
for p in "$STATE_DIR/.env" "$STATE_DIR/inbound/events.jsonl" "$STATE_DIR/access.json"; do
  [[ -e "$p" ]] || continue
  run chmod 600 "$p"
  say "$(basename "$p") → 600"
done
[[ -e "$STATE_DIR/.env" ]] || say "no token yet — that is expected on a fresh host"

# ── what a human must do ───────────────────────────────────────────────────
head "Three things only a human can do"
cat <<NEXT
  1. Log in to Claude Code as $USER_NAME, with the CLIENT'S OWN account:
       su - $USER_NAME
       claude
     Their subscription, their login. Never yours — sharing a subscription
     breaks the provider's terms and puts both accounts at risk.

  2. Create a bot with @BotFather, then inside Claude Code:
       /plugin marketplace add shakhruz/mila-telegram
       /plugin install mila-telegram@mila
       /telegram:configure <token>

  3. Start the daemon and check the channel:
       systemctl --user enable --now mila-telegram-receiver
       mila doctor

  Then pair from the phone, and lock the door:
       /telegram:access pair <code>
       /telegram:access policy allowlist
NEXT

echo
if [[ $DRY -eq 1 ]]; then
  echo "Dry run complete. Nothing was changed."
else
  echo "Bootstrap complete. The host is ready; the account is not — see above."
fi
