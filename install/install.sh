#!/usr/bin/env bash
# Mila Core — installs the assistant kit into an existing Claude Code setup.
#
# What it does, in order, and nothing else:
#   1. copies the skills into ~/.claude/skills/
#   2. installs the `mila` launcher into ~/.local/bin/
#   3. installs the inbox hook and registers it in ~/.claude/settings.json
#
# It does NOT install the Telegram plugin — that is a separate step you run
# inside Claude Code (see README). It does not touch your bot token, your
# access list, or anything under ~/.claude/channels/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
STATE_DIR="${TELEGRAM_STATE_DIR:-$CLAUDE_DIR/channels/telegram}"
BIN_DIR="$HOME/.local/bin"
DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

say() { printf '  %s\n' "$*"; }
run() { if [[ $DRY -eq 1 ]]; then say "would: $*"; else "$@"; fi; }

echo "Mila Core installer"
echo "  target: $CLAUDE_DIR"
[[ $DRY -eq 1 ]] && echo "  DRY RUN — nothing will be written"
echo

# ── skills ────────────────────────────────────────────────────────────────
echo "Skills"
run mkdir -p "$CLAUDE_DIR/skills"
for d in "$ROOT"/skills/*/; do
  name="$(basename "$d")"
  target="$CLAUDE_DIR/skills/$name"
  if [[ -e "$target" && $DRY -eq 0 ]]; then
    # Never overwrite silently: a customised skill is someone's work.
    backup="$target.bak-$(date +%Y%m%d-%H%M%S)"
    say "$name — exists, keeping a copy at $(basename "$backup")"
    mv "$target" "$backup"
  fi
  run cp -R "$d" "$target"
  say "$name installed"
done
echo

# ── launcher ──────────────────────────────────────────────────────────────
echo "Launcher"
run mkdir -p "$BIN_DIR"
run cp "$HERE/mila" "$BIN_DIR/mila"
run chmod 755 "$BIN_DIR/mila"
say "mila → $BIN_DIR/mila"
case ":$PATH:" in
  *":$BIN_DIR:"*) say "$BIN_DIR is on PATH" ;;
  *) say "NOTE: $BIN_DIR is not on your PATH — add it to your shell profile:"
     say "      export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
echo

# ── inbox hook ────────────────────────────────────────────────────────────
echo "Inbox hook"
HOOK_DIR="$CLAUDE_DIR/hooks"
run mkdir -p "$HOOK_DIR"
run cp "$HERE/telegram-inbox-feed.py" "$HOOK_DIR/telegram-inbox-feed.py"
run chmod 755 "$HOOK_DIR/telegram-inbox-feed.py"
say "hook → $HOOK_DIR/telegram-inbox-feed.py"
echo

# ── tools the launcher calls ──────────────────────────────────────────────
# The launcher looks for every one of these in $HOOK_DIR. Installing only the
# hook left six of the eight `mila` commands answering "reinstall the kit", and
# the promise watchdog silently returning an empty list — which reads exactly
# like "nothing is due".
echo "Tools"
for f in usage_collect.py chats_index.py chat_note.py chat_locale.py \
         doctor.py backup.py design_check.py; do
  if [[ -f "$HERE/$f" ]]; then
    run cp "$HERE/$f" "$HOOK_DIR/$f"
    run chmod 755 "$HOOK_DIR/$f"
    say "$f"
  else
    say "🔴 missing from the kit: $f"
  fi
done

# The permission policy decides what runs without waking anyone. With no file
# at all the code falls back to STRICT — every single tool call becomes a card
# in Telegram, and past twelve a minute they are auto-denied. A first evening
# like that reads as "the assistant is broken".
POLICY="$STATE_DIR/permissions.json"
if [[ -f "$POLICY" ]]; then
  say "permissions.json — exists, left alone"
elif [[ -f "$HERE/permissions.example.json" ]]; then
  run mkdir -p "$STATE_DIR"
  run cp "$HERE/permissions.example.json" "$POLICY"
  run chmod 600 "$POLICY"
  say "permissions.json installed from the example — READ IT before trusting it"
  say "  it decides what runs without asking you: $POLICY"
fi

if [[ $DRY -eq 0 ]]; then
  python3 - "$CLAUDE_DIR" << 'PY'
import json, os, sys
cfg_dir = sys.argv[1]
p = os.path.join(cfg_dir, 'settings.json')
hook_cmd = os.path.join(cfg_dir, 'hooks', 'telegram-inbox-feed.py')
try:
    with open(p, encoding='utf-8') as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
except json.JSONDecodeError:
    print('  settings.json is not valid JSON — hook NOT registered, add it by hand')
    raise SystemExit(0)

hooks = cfg.setdefault('hooks', {})
entries = hooks.setdefault('UserPromptSubmit', [])
already = any(hook_cmd in json.dumps(e) for e in entries)
if already:
    print('  already registered in settings.json')
else:
    entries.append({'hooks': [{'type': 'command', 'command': hook_cmd}]})
    backup = p + '.bak-milacore'
    if os.path.exists(p):
        with open(backup, 'w', encoding='utf-8') as f:
            json.dump(json.load(open(p, encoding='utf-8')), f, ensure_ascii=False, indent=2)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print('  registered in settings.json (previous version kept as settings.json.bak-milacore)')
PY
fi
echo
echo "Done. Next, inside Claude Code:"
echo "  /plugin marketplace add shakhruz/mila-telegram"
echo "  /plugin install mila-telegram@mila"
echo "  /telegram:configure <your bot token>"
echo
echo "Then start a session with:  mila"
