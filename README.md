# Mila Core

The kit that turns a Claude Code subscription into an assistant your team can
actually talk to — in Telegram, in group chats, all day.

Claude Code is a terminal tool. It waits for you at a keyboard. Mila Core is
what we added around it so it could work the way a colleague works: reachable
from a phone, present in client chats, surviving restarts, and behaving itself
in front of people who are not engineers.

This is the kit, extracted from a fleet of 18 assistants that run real client
work every day — travel bookings, a newspaper, clinics, tender monitoring.
Every rule in here cost us something before it became a rule.

---

## What's inside

| Piece | What it does |
| --- | --- |
| `skills/client-chat` | How the assistant behaves in a chat with a client: tone, promises, tasks, attachments, boundaries. Read before the first reply in any new chat. English in `SKILL.en.md`, Russian original in `SKILL.md`. |
| `skills/mila-session` | The four links of the Telegram channel and how to check each one when something goes quiet. |
| `skills/communication` + `CHECKLIST.md` | How every outgoing message is built: result first, one screen, evidence per fact, no self-deprecation, the supplier's frame with a pushy client. The checklist is the 30-second version read before each send. |
| `skills/mila-tasks` | Commitments ledger: tasks, promises to people, decisions waiting on the owner — append-only `ledger.jsonl`, closure only with machine-run proof, `task.py` (stdlib) and two hooks that catch promises in outgoing messages. |
| `skills/tasks-discipline` | For a client-facing director: a named deadline becomes a task, not a phrase; take it, do it, report with proof. |
| `skills/mail-outbound` | Letters leave in two steps (draft → human's tap → send); an address found by search is a hypothesis until checked; no invented contacts. |
| `skills/crm-dialogs`, `inbox-triage`, `lead-qualify`, `reply-from-examples`, `polite-no`, `escalate-to-owner`, `dialog-close` | The communicator set for a director: triage the inbox in one pass, qualify a lead, draft from the owner's own examples, decline without discounts, escalate with one card, close a dialog with a stated reason. |
| `skills/youtube-research` | Research a topic through YouTube transcripts before writing about it. |
| `install/mila` | One-word launcher: `mila` resumes, `mila new` starts fresh, `mila status` checks the channel, `mila inbox` shows unread messages. |
| `install/telegram-inbox-feed.py` | Hook that surfaces incoming Telegram messages in the terminal — and any promise whose deadline is today or past. |
| `install/usage_collect.py` | Token spend by day and model, read straight from the session transcripts. Wired into the launcher as `mila usage`. |
| `install/chats_index.py` | Chat registry: which chats are connected, who talks in them, when they last did. `mila chats`. |
| `install/chat_locale.py` | Timezone and language per chat. `mila when` shows what time it is for every client and who is in quiet hours. |
| `install/chat_note.py` | Writes into a chat card — purpose, participants, promises, notes. `mila owe` lists every open promise across all chats. |
| `install/permission_policy.ts` + `permissions.example.json` | Decides what can be decided by rule, so a human is woken only for their own calls. |
| `install/backup.py` | Archives what cannot be recovered: journals, chat cards, access list, skills. Starts with `status`, because a backup nobody has verified is a hope. `mila backup`. |
| `install/doctor.py` | Why the bot is silent: token, privacy mode, access list, daemon, journal, bridge cursor, file permissions, whether the channel is actually attached to the session. `mila doctor`. |
| `install/design_check.py` | Mechanical acceptance for a page: overflow at four widths, headline line count, box nesting, focus states, font coverage, theme tokens. |
| `skills/design-gates` | The rules behind that check — what is measured, why, and what is simply banned. |
| `install/install.sh` | Puts all of the above where Claude Code looks for it. Backs up anything it would overwrite. |

The Telegram channel itself lives in a separate repository:
**[mila-telegram](https://github.com/shakhruz/mila-telegram)** — a fork of the
official plugin with a restart-proof daemon, one-tap group approval, voice
notes, reply-quote context, and incoming emoji reactions — the agent sees
which of its messages the user liked, even when they say nothing.

**Two repositories, one kit.** This one holds the skills, the tools and the
deployment. The Telegram channel itself is
[mila-telegram](https://github.com/shakhruz/mila-telegram) — install both.

---

## Deploying to someone else's server

```sh
scp deploy/bootstrap.sh root@host:/tmp/
ssh root@host 'bash /tmp/bootstrap.sh --user mila --dry-run'
```

User, runtimes, kit, state directories, a systemd unit for the receiver,
hardened permissions — idempotent, with a dry run that changes nothing and
prints every step. See **[deploy/README.md](./deploy/README.md)**.

Three things it will not do: log anyone in, write a bot token, open a port. The
first is not a limitation — the account and the subscription must be the
client's own, with their hands on the keyboard. Sharing one across people breaks
the provider's terms and risks every account involved.

## Install

Requires an existing Claude Code installation and [Bun](https://bun.sh) for the
Telegram plugin.

```sh
git clone https://github.com/shakhruz/mila-core
cd mila-core
./install/install.sh --dry-run   # see exactly what it will touch
./install/install.sh
```

Then, inside a Claude Code session:

```
/plugin marketplace add shakhruz/mila-telegram
/plugin install mila-telegram@mila
/telegram:configure <bot token from @BotFather>
```

Restart with `mila`, message your bot, and pair:

```
/telegram:access pair <the code the bot replies with>
/telegram:access policy allowlist
```

That last line matters. Pairing exists to capture your own ID; leave it on and
strangers who guess the bot's username keep getting pairing codes.

---

## What it looks like when it works

```
$ mila status
receiver   running (pid 32455)          the daemon that polls Telegram
sender     running                       outbound queue
bridge     cursor 268968 / 268968        session has read the whole journal
journal    268968 bytes, 1633 events     nothing lost across restarts
outbox     empty
```

Messages sent while the session was down are waiting in the journal and arrive
when it comes back. That is the part that makes this usable as a daily tool
rather than a demo: you can restart, upgrade, or crash, and the conversation
does not lose a message.

---

## What a day of this costs

```
$ mila usage --days 7 --by-model
```

Reads the session transcripts, adds up input, output, cache writes and cache
reads per model, and prices them from `~/.claude/prices.json`.

One honest caveat, printed above every report: if you are running on a Claude
Code subscription, nobody charges you those dollars. The number answers a
different question — what the same volume would have cost at API list price.
That is a measure of what the subscription gives back, and on a busy day it is
a startling number. With a BYOK key it becomes the real bill, and then it is
the figure to reconcile against the provider's statement.

`--by-chat` answers a different question: which client costs what. There is no
direct link from a model turn to a chat — one session serves twenty of them at
once — so the attribution is indirect and stated as such: work that ended in a
reply to chat X was done for X. Good enough to see who is expensive, not good
enough to invoice.

Cache is why this is worth having. Reading cache is roughly ten times cheaper
than fresh input and writing it is more expensive — until you see them apart, an
expensive day and a merely long day look identical.

---

## Knowing where the work is

```
$ mila chats --stale 14
```

A registry built from the access list plus the inbound journal: chat title, who
writes there, how many messages, how long since the last one. Chats are ordered
by how recently something happened, because that is the question people actually
ask of a registry. Every connected chat also gets a card in `chats/<id>.md` with
sections for purpose, participants, agreements and summary — the mechanics write
the skeleton and never overwrite what the assistant put there.

```
$ mila owe
Открытые обещания · 8
  Acme · working chat
    · visual prototype: desktop + mobile · due before rollout
  Northside Studio · client chat
    · stories poll · due tonight
```

Promises scattered across twenty cards are an archive, not a list. This is the
list. A promise is closed the same way it was opened — by name — so "I did that"
without a matching entry does not close anything.

Quiet hours only mean something once you answer "whose 22:00". Until a chat has
a timezone they are computed in yours — which for a client in another hemisphere
means the "morning" message lands at night. Set it once per chat and the inbox
hook names anyone currently asleep, before you write to them.

Deadlines are written the way a person said them ("tonight", "26.08 in the
afternoon", "Thursday"), parsed where they parse, and honestly marked `?` where
they don't — a guessed deadline is worse than none, because people relax about
it. Anything due today or overdue is printed by the inbox hook on every turn,
whether or not there are new messages: a deadline burns in silence too.

Titles only appear for chats that have seen a message since the receiver started
recording them. The Bot API offers no way to list the chats a bot belongs to, so
the past cannot be backfilled — a chat without a title is not an error, just one
nobody has written in lately.

---

## Deciding without waking anyone

A card per tool call is fine at five requests a day and unusable at fifty: the
owner starts tapping Allow without reading, which is worse than no gate — it
looks like control and is not.

`permissions.json` holds the rules. **allow** runs silently but logged, **deny**
refuses without sending a card at all, **ask** sends one with a deadline — an
unanswered card is a refusal, and the assistant is expected to say out loud that
it did not do the thing.

A malformed policy becomes all-ask. Never all-allow: a syntax error must not
open the door. Never all-deny: that paralyses instead of asking.

Start from `install/permissions.example.json` — 15 rules, two hard denials, and
a comment on every rule saying why it exists. Rules that block routine work get
deleted by whoever is trying to work, so the dangerous ones use regular
expressions and draw the line precisely: root and system directories refused,
`rm -rf /tmp/scratch` left alone.

## What is worth backing up

```
$ mila backup status
```

The code is in git and the model lives at the provider. What exists nowhere else
is the memory: the inbound journal, the chat cards with their agreements, the
access list, the skills assembled out of real mistakes, the spend log. Losing
the machine means losing that, not the program.

```
$ mila backup run --to user@host:/srv/archives
```

An archive on the same machine protects against a mistake, not against losing
the machine — two different products, and a client should be told which one they
bought. `--to` ships a copy over ssh, then asks the far side how big the file
is: "scp exited zero" and "the file is there intact" are different claims, and
they diverge silently. Locally it keeps a week, remotely thirty.

One thing to check on the receiving host: whether your existing backup covers
that directory. Ours excluded the server's backups directory, so putting archives there
would have kept them off the cloud copy entirely — the folder named "backups"
was the one place a backup should not go.

It starts with `status` on purpose — an honest answer to "what is saved and
when" before any saving happens. Secrets are excluded by a filter that runs on
every file, not just the top-level paths: a token that rides along in an archive
resurfaces later in somebody else's copy. Every archive is reopened and read
back after writing, because an archive that does not open is worse than none —
people rely on it.

## When the bot goes quiet

```
$ mila doctor
```

Silence looks the same whatever caused it: a missing token, privacy mode still
on at BotFather, the bot never added to the group, a forgotten launch flag, a
dead daemon, a bridge that stopped reading. This walks every link in order and
tells you what to do about each one, not just that it is red. It fixes nothing
on its own — a doctor that treats without asking is not a doctor.

## Checking a page before it goes out

```
$ python3 ~/.claude/hooks/design_check.py https://your.site/page
```

Measures at 320, 375, 414 and 1280: horizontal overflow, how many lines the
headline actually takes, nested box depth, digits without tabular numerals, a
transparent body background, empty sections, missing `:focus-visible` and
`prefers-reduced-motion`, fonts without Cyrillic coverage, and theme variables
declared only inside a dark-mode block.

That last one is the classic way to ship an unreadable page: a colour defined
only under `@media (prefers-color-scheme: dark)` does not exist in the default
"system" state, so the page renders one theme's text on the other theme's
ground.

A screenshot does not substitute for a measurement. A headless browser renders
a phone viewport like a desktop; overflow only shows up as a number.

We pointed it at our own pages first. It found four real defects, including a
headline that took four lines on a phone and no keyboard focus state anywhere.

---

## The rules that actually matter

The skills are the interesting half of this kit. A summary of what they enforce,
because these are the mistakes that cost us clients' time:

**Say what you did, with evidence.** "Updated the panel" is worthless unless the
file changed. Our assistant claimed that once while the files sat untouched for
five days — now the panel is generated from data and the claim is checkable.

**A promise is a record, not a sentence.** "I'll have it by six" goes into a
list with a watchdog, and gets reported at six even if the answer is "not
ready".

**Silence is not a status.** Stuck on a broken tool is a thing to say out loud
within minutes, not a thing to hide behind a plausible answer.

**Never invent a fact.** Not a phone number, not a price, not a deadline. If
the source didn't say it, the answer is "not specified".

**Own the failure without grovelling.** State what happened, what caused it,
what changes in the mechanism. Apologies devalue the service; fixes don't.

---

## Security, honestly

The Telegram channel is a public door into a session that can read files and run
commands. Before you put this on a machine that matters, read
[SECURITY.md in mila-telegram](https://github.com/shakhruz/mila-telegram/blob/main/SECURITY.md)
and the access model in
[ACCESS.md](https://github.com/shakhruz/mila-telegram/blob/main/ACCESS.md).

Two settings do most of the work:

- `owners` — who may *decide* (approve a tool run, connect a group), as opposed
  to who may *talk*. Set it the moment a second person is on your DM allowlist.
- `requireMention: true` on groups — the assistant answers only when addressed.
  Turning it off means every message from every member enters the session.

Messages are untrusted input. Anyone in an allowlisted chat can try to instruct
the assistant, and a message asking it to approve a pairing is exactly what an
attack looks like. Approvals happen in your own private chat, on a button.

---

## Status

Built and running in production; packaged for other people as of 25 August 2026.
Being honest about the seams:

- `client-chat` exists in both languages. The Russian original is the one that
  grew out of the work; the English version is a translation, so if the two ever
  disagree, the Russian one is what actually happened.
- Per-chat cost is attributed by "which reply did this work end in" — it cannot
  split work done for two clients at once.
- Chat cards are written through `mila note`; the assistant still decides when
  to write, nothing is extracted from the conversation automatically.

---

## Installing this is a day. The assistant is the other part.

Everything needed to run it is here, the licence says you may, and
`deploy/README.md` walks the whole path. Do that and by tonight you have a
working channel: messages arrive, nothing is lost on restart, spend is counted.

What you do not have yet is an assistant that knows anything. The kit is empty
of your business on purpose — it ships mechanics, not judgement. Everything that
makes the difference between a channel and a colleague is learned:

- which of your chats is a client, who decides in it, what was promised
- how your work is delivered — the language, the format, what never ships
  without a human reading it first
- the rules that only exist because something went wrong once, written down so
  it does not go wrong twice

Every skill here started that way. `client-chat` is a page of rules, and each
line cost either a client's hour or an awkward message. We can hand you the
page. What takes a month is your own version of it.

**That is what the paid month is.** Week one the server and its security, then
images and video, then social accounts and a site, then your own data — and the
point of the fourth week is that your team runs it without us. Your account,
your machine, your assistant, and the working habit around it. The training
happens in a shared chat with our own assistant: your agent learns from ours,
because a skill here is a file that one agent reads and another can be taught
to write.

Details and contact:
**[milagpt.io/mila-core](https://milagpt.io/mila-core?from=github)**

The code being open is the reason this is worth buying rather than the reason
it isn't: you can read every line you are paying someone to teach you to run.

---

## Licence

MIT for this kit. The Telegram plugin is Apache-2.0, as a fork of Anthropic's
official channel plugin — see its NOTICE file.
