---
name: mila-session
description: Start or resume a Claude Code session with the mila-telegram plugin using one short command, and check channel health. Use when starting or restarting a session, diagnosing a silent channel (receiver, bridge, sender, journal, outbox), or repairing it without restarting the session.
---

# mila — one command for the session

The real launch line is unreadable and nobody types it by hand:

```
claude --dangerously-load-development-channels plugin:mila-telegram@mila-marketplace --continue
```

`mila` wraps it, and brings the channel up first if it is down.

## Commands

| Command | What it does |
|---|---|
| `mila` | resume the last session (`--continue`) with the plugin |
| `mila new` | start a fresh session |
| `mila pick` | choose a session from the list (`--resume`) |
| `mila status` | channel health: receiver · sender · bridge · journal · outbox |
| `mila fix` | restart receiver and sender, **leaving the session alone** |
| `mila help` | usage |

Anything after the command is passed to `claude` as-is: `mila new --model opus`.

## Before launch

It checks the receiver daemon and the sender daemon and starts them via the
process supervisor if they are down. The order matters: **the daemon must be
running before the session starts** — while no session exists it keeps
collecting inbound messages into the journal, so nothing is lost.

## Reading `mila status`

Four links in the chain, each fails differently:

- **receiver** (`receiver.ts`, `RECEIVER_DAEMON=1`) — polls Telegram and appends
  to the journal. Down → inbound messages are lost for good.
- **sender** — the independent outbound path. Down → replies pile up in `outbox/`.
  Fix with `mila fix`. After a long idle the first attempt may fail with
  `fetch failed`; the retry succeeds.
- **bridge** (`bun server.ts`) — part of the session's MCP server, replays the
  journal into the session. It lives only as long as the session does. Dead
  bridge with a live receiver is **silent deafness**: messages are accepted but
  never reach the model.
- **journal** — cursor compared to the size of `events.jsonl`. A gap while the
  receiver is alive is the tell-tale sign of a dead bridge.


## Seeing inbound messages in the session

With the daemon running, the session no longer polls Telegram — the bridge
replays the journal via `mcp.notification`. Those replayed notifications are not
reliably rendered in the terminal, so an inbound message can arrive unseen.

Two mechanisms close that gap, and they share one marker file
(`inbound/seen`, a byte offset), so nothing is reported twice:

- **`UserPromptSubmit` hook** (`telegram-inbox-feed.py`) — on every user turn it
  reads the journal and prints anything unread as `<channel …>` blocks, the same
  shape the upstream plugin produces. This is what makes messages visible in the
  session itself.
- **Status line badge** — `📨 N` shows the unread count between turns, `📤 N`
  shows a stuck outbox.
- **`mila inbox`** — prints unread messages on demand and marks them seen.

Bot-to-bot chatter is filtered out by sender name; only humans surface.

## What to do when

- Channel silent, receiver up → look at the bridge. If it is dead, restart the
  session (`mila`); no edit fixes it.
- Replies not going out, outbox growing → `mila fix`.
- After any restart → `mila status`: the cursor must catch up with the journal
  size. That is the only check proving the backlog was replayed.

## Why it exists

A dead bridge once cost the owner 34 minutes of writing into silence: the
receiver was healthy, the journal was filling up, and from the outside the
channel looked exactly like a working one. `mila status` makes that state
visible in a second.
