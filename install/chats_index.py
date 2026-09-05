#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Реестр чатов: кто подключён, кто живой, кто молчит месяц.

Реестр, собранный из одних числовых id, бесполезен — восемьдесят строк вида
`-100XXXXXXXXXX · отвечать всем` не отвечают ни на один вопрос, который про
чаты вообще задают. Этот скрипт добавляет к списку доступов то, что известно
из журнала входящих: название чата, кто в нём пишет, когда было последнее
сообщение и сколько их всего.

Названия появляются у тех чатов, где после 25.08.2026 было хотя бы одно
сообщение: до этого приёмник их не сохранял. Bot API не отдаёт список чатов,
в которых состоит бот, — достроить прошлое неоткуда, реестр наполняется по мере
разговоров. Там, где названия нет, так и написано.

Пишет CHATS.md (перезаписывает целиком — это производная от данных) и заводит
карточку chats/<id>.md для чатов, где её ещё нет. Карточки НЕ перезаписывает:
их содержимое ведёт агент, и затирать его нельзя.

  chats_index.py            # обновить реестр и завести недостающие карточки
  chats_index.py --quiet    # без вывода, для крона
  chats_index.py --stale 14 # показать молчащих дольше 14 дней
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

STATE = os.environ.get("TELEGRAM_STATE_DIR",
                       os.path.expanduser("~/.claude/channels/telegram"))
ACCESS = os.path.join(STATE, "access.json")
EVENTS = os.path.join(STATE, "inbound", "events.jsonl")
CHATS_DIR = os.path.join(STATE, "chats")
INDEX = os.path.join(STATE, "CHATS.md")

MODE_LABEL = {
    "all": "отвечаю всем",
    "mention": "по упоминанию",
    "read": "только читаю",
}


def load_access():
    try:
        with open(ACCESS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print("access.json не прочитан: %s" % e, file=sys.stderr)
        return {}


def mode_of(policy):
    if not isinstance(policy, dict):
        return "all"
    if policy.get("readOnly"):
        return "read"
    return "mention" if policy.get("requireMention") else "all"


def scan_events():
    """{chat_id: {title, type, users{}, count, first, last}} — из журнала."""
    seen = defaultdict(lambda: {"title": "", "type": "", "users": defaultdict(int),
                                "count": 0, "first": "", "last": ""})
    if not os.path.exists(EVENTS):
        return seen
    with open(EVENTS, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            m = (d.get("params") or {}).get("meta") or {}
            cid = m.get("chat_id")
            if not cid:
                continue
            rec = seen[str(cid)]
            rec["count"] += 1
            if m.get("chat_title"):
                rec["title"] = m["chat_title"]
            if m.get("chat_type"):
                rec["type"] = m["chat_type"]
            user = m.get("user") or m.get("user_id") or "?"
            rec["users"][user] += 1
            ts = m.get("ts") or ""
            if ts:
                if not rec["first"] or ts < rec["first"]:
                    rec["first"] = ts
                if ts > rec["last"]:
                    rec["last"] = ts
    return seen


def days_since(iso):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).days


def ensure_card(cid, title, mode, live):
    """Создаёт скелет карточки, если её нет.

    Существующую не переписывает — там содержание, которое ведёт агент. Но
    заголовок обновляет: карточки, заведённые до того, как приёмник научился
    сохранять название, называются «Чат -100XXXXXXXXXX», и сводка обещаний
    получается списком чисел вместо списка клиентов. Заголовок — часть
    скелета, а не содержания.
    """
    path = os.path.join(CHATS_DIR, "%s.md" % cid)
    if os.path.exists(path):
        if title:
            try:
                body = open(path, encoding="utf-8").read()
                if re.match(r"^#\s+Чат\s+-?\d+\s*$", body.split("\n")[0]):
                    lines = body.split("\n")
                    lines[0] = "# %s" % title
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines))
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, path)
            except OSError:
                pass
        return False
    os.makedirs(CHATS_DIR, mode=0o700, exist_ok=True)
    who = ", ".join("@%s" % u if not u.isdigit() else u
                    for u, _ in sorted(live["users"].items(),
                                       key=lambda kv: -kv[1])[:8]) or "(пока не видела)"
    body = (
        "# %s\n\n"
        "chat_id: %s\n"
        "режим: %s\n"
        "сообщений в журнале: %d%s\n\n"
        "## Назначение\n(заполняет Мила)\n\n"
        "## Участники\n%s\n\n"
        "## Договорённости\n(что обещано, кому и к какому сроку)\n\n"
        "## Резюме\n(заполняет Мила после первых разговоров)\n"
        % (title or "Чат %s" % cid, cid, MODE_LABEL.get(mode, mode),
           live["count"],
           " · последнее %s" % live["last"][:10] if live["last"] else "",
           who)
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o600)
    return True


def main():
    ap = argparse.ArgumentParser(description="Реестр подключённых чатов")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--stale", type=int, default=0,
                    help="показать чаты, молчащие дольше N дней")
    args = ap.parse_args()

    access = load_access()
    groups = access.get("groups") or {}
    live = scan_events()

    rows = []
    for cid, policy in groups.items():
        l = live.get(str(cid), {"title": "", "type": "", "users": {},
                                "count": 0, "first": "", "last": ""})
        rows.append({
            "id": str(cid),
            "title": l["title"],
            "mode": mode_of(policy),
            "count": l["count"],
            "last": l["last"],
            "days": days_since(l["last"]),
            "users": dict(l["users"]),
        })

    # Личные чаты из allowFrom тоже часть картины: это люди, а не группы.
    for uid in access.get("allowFrom") or []:
        l = live.get(str(uid))
        rows.append({
            "id": str(uid), "title": "личка", "mode": "dm",
            "count": l["count"] if l else 0,
            "last": l["last"] if l else "",
            "days": days_since(l["last"]) if l else None,
            "users": dict(l["users"]) if l else {},
            "owner": str(uid) in (access.get("owners") or []),
        })

    # Сначала те, где сегодня что-то происходило: реестр читают, чтобы
    # понять, где работа, а не чтобы любоваться алфавитом.
    rows.sort(key=lambda r: (r["days"] if r["days"] is not None else 10**6,
                             -r["count"]))

    created = 0
    for r in rows:
        if r["mode"] == "dm":
            continue
        if ensure_card(r["id"], r["title"],
                       r["mode"], live.get(r["id"], {"users": {}, "count": 0, "last": ""})):
            created += 1

    named = sum(1 for r in rows if r["title"] and r["title"] != "личка")
    active = sum(1 for r in rows if r["days"] is not None and r["days"] <= 7)

    out = ["# Подключённые чаты", "",
           "> Собирается из access.json и журнала входящих. Карточки: chats/<id>.md",
           "> Обновлено: %s" % datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
           "",
           "Всего %d · с названием %d · активных за неделю %d"
           % (len(rows), named, active), ""]

    def block(title, items):
        if not items:
            return
        out.append("## %s" % title)
        out.append("")
        out.append("| Чат | id | режим | сообщений | последнее | кто пишет |")
        out.append("| --- | --- | --- | ---: | --- | --- |")
        for r in items:
            who = ", ".join("@%s" % u if not str(u).isdigit() else str(u)
                            for u, _ in sorted(r["users"].items(),
                                               key=lambda kv: -kv[1])[:3])
            when = r["last"][:10] if r["last"] else "—"
            if r["days"] is not None:
                when += " (%d дн.)" % r["days"] if r["days"] else " (сегодня)"
            name = r["title"] or "_без названия_"
            out.append("| %s | `%s` | %s | %d | %s | %s |"
                       % (name, r["id"], MODE_LABEL.get(r["mode"], r["mode"]),
                          r["count"], when, who or "—"))
        out.append("")

    block("Живые — писали за последнюю неделю",
          [r for r in rows if r["days"] is not None and r["days"] <= 7])
    block("Тихие — писали раньше",
          [r for r in rows if r["days"] is not None and r["days"] > 7])
    block("Молчат — в журнале ни одного сообщения",
          [r for r in rows if r["days"] is None])

    out.append("---")
    out.append("")
    out.append("Названия появляются у чатов, где было сообщение после 25.08.2026: "
               "до этого приёмник их не сохранял, а Bot API списка чатов не отдаёт. "
               "Чат без названия — не ошибка, просто в нём с тех пор не писали.")

    with open(INDEX, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(INDEX, 0o600)

    if not args.quiet:
        print("Реестр обновлён: %s" % INDEX)
        print("  чатов %d · с названием %d · активных за неделю %d · новых карточек %d"
              % (len(rows), named, active, created))
        if args.stale:
            stale = [r for r in rows
                     if r["days"] is not None and r["days"] >= args.stale]
            print()
            print("Молчат дольше %d дней: %d" % (args.stale, len(stale)))
            for r in stale[:20]:
                print("  %-34s %s  %d дн."
                      % ((r["title"] or r["id"])[:34], r["id"], r["days"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
