# Deploying to a client's server

This is the part we sell as "week one": a machine that runs the assistant, set
up so nobody has to remember how it was done.

The script does everything a machine can do. Three things it deliberately
leaves to a person, and the first one is the important one.

---

## The subscription is theirs

**Log in to Claude Code with the client's own account, on their own
subscription, with their hands on the keyboard.**

Not yours, not a shared one, not "we'll put it on ours for now". Sharing a
subscription across people breaks the provider's terms and puts both accounts at
risk — including yours, and including every other client you host. The whole
offer rests on this: they own the account, they own the machine, they own the
assistant. We set it up and teach them to run it.

If a client asks you to just use your login "to get started", that is the
moment to explain this, not later.

---

## What the script does

```sh
scp deploy/bootstrap.sh root@their-server:/tmp/
ssh root@their-server 'bash /tmp/bootstrap.sh --user mila --dry-run'
ssh root@their-server 'bash /tmp/bootstrap.sh --user mila'
```

Run the dry run first and read it out loud to yourself. It changes nothing and
prints every step it would take.

- **Packages** — curl, git, python3, rsync, unzip, ca-certificates
- **User** — a dedicated unprivileged account, password login disabled, reachable
  by key or by root only. Optionally `--ssh-key ~/.ssh/id_ed25519.pub`
- **Lingering** — user services keep running after logout. Without this the
  daemon dies with the SSH session, which defeats the entire point of a durable
  receiver
- **Runtimes** — Bun for the plugin, Node, Claude Code
- **Kit** — clones `mila-core` and runs its installer (skills, launcher, hooks)
- **State** — `~/.claude/channels/telegram/` and subdirectories at `0700`
- **Daemon** — a systemd user unit for the receiver, written but not started:
  starting it before the plugin and token exist would only produce a crash loop
- **Permissions** — token, journal and access list forced to `0600`

Idempotent. Run it twice and it skips what is already there and says so.

---

## After the script

```sh
su - mila
claude                       # client logs in with THEIR account
```

Inside Claude Code:

```
/plugin marketplace add shakhruz/mila-telegram
/plugin install mila-telegram@mila
/telegram:configure <token from @BotFather>
```

Then:

```sh
systemctl --user enable --now mila-telegram-receiver
mila doctor
```

`mila doctor` is the acceptance test. Every link green means the channel works;
anything else prints what to do about it. Do not hand over a server that has not
passed it.

Finally, from the client's phone: message the bot, then in the session

```
/telegram:access pair <code>
/telegram:access policy allowlist
```

That last line matters. Until it runs, anyone who guesses the bot's username
gets a pairing code.

---

## Handing it over

Walk them through these five, on their machine, with them driving:

1. `mila` — start or resume a session
2. `mila doctor` — what to run when it goes quiet
3. `mila owe` — what the assistant currently owes people
4. `mila usage` — what a day costs
5. `/mila_join` in a group — how to connect a new chat, and `/mila_leave` to
   disconnect it

A client who can run those five does not need us for the everyday. That is the
point: we are selling a working assistant and the ability to keep it, not a
dependency.

---

## What still needs deciding per client

- **Backups.** `mila backup run` archives state and skills; schedule it daily
  and, more importantly, decide where the archive goes. On the same disk it
  protects against mistakes, not against losing the machine — say which of the
  two you are selling them.
- **Model routing.** Which model does the daily work — this changes the bill
  more than anything else.
- **Who is `owners`.** The moment a second person is on the DM allowlist, set
  `owners` explicitly, or everyone with access can approve tool runs.
