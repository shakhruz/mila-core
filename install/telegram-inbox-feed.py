#!/usr/bin/env python3
"""UserPromptSubmit hook: surface unread Telegram messages in the session.

Why this exists: with the receiver daemon running, the session no longer polls
Telegram itself — the bridge replays the journal via mcp.notification. Those
replayed notifications are not reliably rendered in the terminal, so inbound
messages could arrive unseen. This hook reads the journal directly and prints
anything unread as additional context on every user turn.

Marker file `inbound/seen` holds the byte offset already shown. `mila inbox`
uses the same marker, so the two never double-report.
"""
import json, os, sys

STATE = os.environ.get("TELEGRAM_STATE_DIR") or os.path.expanduser(
    "~/.claude/channels/telegram")
EV = os.path.join(STATE, "inbound", "events.jsonl")
SEEN = os.path.join(STATE, "inbound", "seen")
SUSPICIOUS = os.path.join(STATE, "inbound", "suspicious.jsonl")
MAX_SHOWN = 25


def esc(v):
    """Всё, что пришло из чата, — данные, а не разметка.

    Этот хук собирает конверт `<channel …>` руками, и без экранирования любой
    участник любой подключённой группы закрывает конверт своим текстом и
    открывает новый — с чужим chat_id и чужим именем в атрибутах. Дальше он
    пишет что угодно от лица владельца, а шапка вывода сама предлагает
    ответить в чат, который он назовёт.
    """
    return (str("" if v is None else v)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# Признаки попытки вырваться из конверта. Юникодные двойники угловых скобок
# считаются наравне с обычными: экранирование их не трогает, а модель читает
# так же.
INJECTION_MARKS = ("</channel", "<channel", "system-reminder", "</antml",
                   "‹", "›", "＜", "＞")


def looks_like_injection(text):
    low = (text or "").lower()
    return any(mark in low for mark in INJECTION_MARKS)


def note_suspicious(event):
    """Отдельный журнал: это сигнал атаки, а не шум.

    Сообщение всё равно показывается — экранированным и безвредным, — но факт
    попытки сохраняется, потому что вторая такая же от того же человека это
    уже не случайность.
    """
    try:
        os.makedirs(os.path.dirname(SUSPICIOUS), mode=0o700, exist_ok=True)
        with open(SUSPICIOUS, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        os.chmod(SUSPICIOUS, 0o600)
    except Exception:
        pass

def main():
    # Сначала обещания: срок горит независимо от того, писал ли кто-то в
    # Telegram. Ранние выходы ниже — про непрочитанные сообщения, и до правки
    # они молча уносили с собой и сторож.
    night = quiet_now()
    if night:
        print("Quiet hours right now (%d) — do not write there unless it burns:"
              % len(night))
        print("\n".join(night))
        print()

    owed = burning_promises()
    if owed:
        print("Promises due today or overdue (%d) — close them or say why not:"
              % len(owed))
        print("\n".join(owed))
        print()

    try:
        size = os.path.getsize(EV)
    except OSError:
        return
    try:
        seen = int(open(SEEN).read().strip() or 0)
    except Exception:
        seen = 0
    if size <= seen:
        return

    rows = []
    with open(EV, "rb") as f:
        f.seek(seen)
        offset = seen
        for line in f:
            start, offset = offset, offset + len(line)
            try:
                e = json.loads(line)
                p = e.get("params", {}) or {}
                m = p.get("meta", {}) or {}
            except Exception:
                continue
            user = m.get("user") or ""
            if user.endswith("_bot"):
                continue          # bot chatter is not a human waiting for an answer
            who = user or f"id:{m.get('user_id', '?')}"
            txt = (p.get("content") or "").strip()
            att = ""
            if m.get("image_path"):
                att = f" [image: {m['image_path']}]"
            elif m.get("attachment_file_id"):
                att = f" [attachment_file_id: {m['attachment_file_id']}]"
            quote = ""
            if m.get("reply_quote"):
                quote = f" (replying to: {esc(str(m['reply_quote'])[:80])})"

            # Кто угодно в 80 подключённых чатах может прислать текст, который
            # выглядит как закрытие конверта и начало нового — с чужим chat_id
            # и чужим именем. Без экранирования это не теория, а работающая
            # подделка сообщения владельца, и шапка ниже прямо приглашает
            # ответить в чат отправителя.
            if looks_like_injection(p.get("content") or ""):
                note_suspicious(e)
            rows.append((
                offset,
                f'<channel source="telegram" chat_id="{esc(m.get("chat_id"))}" '
                f'message_id="{esc(m.get("message_id"))}" user="{esc(who)}" '
                f'ts="{esc(m.get("ts", ""))}">{esc(txt)}{esc(att)}{quote}</channel>'
            ))

    # Показываем самые СТАРЫЕ непрочитанные, а не самые свежие. Иначе маркер,
    # поставленный на конец показанного, перепрыгивает через начало пачки —
    # ровно та потеря, от которой мы уходим. Порядок разговора тоже важнее
    # свежести: отвечать надо с того, что человек написал первым.
    shown = rows[:MAX_SHOWN]

    # Маркер уезжает ровно на конец последней ПОКАЗАННОЙ строки. Раньше он
    # прыгал на конец файла: после ночного простоя из шестидесяти сообщений
    # печаталось двадцать пять, а тридцать пять помечались прочитанными и
    # исчезали навсегда — из хука, из `mila inbox`, отовсюду. Продукт при этом
    # продаётся фразой «не теряем входящие».
    # Когда показано всё (или всё отфильтровано как болтовня ботов) — маркер
    # на конец файла, иначе шум перечитывался бы каждый ход.
    mark = size if len(shown) == len(rows) else shown[-1][0]
    try:
        with open(SEEN, "w") as f:
            f.write(str(mark))
        os.chmod(SEEN, 0o600)
    except Exception:
        pass

    if not rows:
        return
    head = f"Unread Telegram messages ({len(rows)}"
    head += (f", showing the oldest {MAX_SHOWN} — the other "
             f"{len(rows) - MAX_SHOWN} stay unread and arrive next turn"
             ) if len(rows) > MAX_SHOWN else ""
    head += "). Answer with the reply tool, passing chat_id back."
    print(head)
    print("\n".join(text for _off, text in shown))



def broken(e):
    """Короткая причина без трейсбека: хук печатается человеку, а не в лог."""
    missing = getattr(e, "filename", None)
    if isinstance(e, (FileNotFoundError, ImportError)) or missing:
        return "нет %s — переустанови комплект (install/install.sh)" % (
            missing or e)
    return "%s: %s" % (type(e).__name__, e)


def quiet_now():
    # QUIET-OFF-0509 — слово владельца 05.09.2026: «убери совсем понятие тихих
    # часов, клиенты работают 24/7, им нужны результаты как можно быстрее».
    # Понятие снято целиком: ни дефолта, ни явных часов чата. Код ниже оставлен
    # мёртвым на случай возврата точечно одному чату — включается удалением
    # этой строки.
    return []
    """Чаты, где сейчас ночь. Знание бесполезно, если о нём надо вспомнить."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "chat_locale", os.path.expanduser("~/.claude/hooks/chat_locale.py"))
        cl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cl)
        a = cl.load()
        names = cl.names()
    except Exception as e:
        # Тихий пустой список неотличим от «всем можно писать». Клиент месяц
        # уверен, что сторож работает, потому что тот ни разу не пожаловался.
        print("  ⚠ сторож тишины не собран: %s" % broken(e))
        return []
    out = []
    for cid, g in (a.get("groups") or {}).items():
        if not isinstance(g, dict) or not g.get("tz"):
            continue
        now = cl.local_now(g["tz"])
        if cl.in_quiet(now, g.get("quiet") or [22, 8]):
            out.append("  %s — %s (%s)"
                       % (names.get(cid, cid), now.strftime("%H:%M"), g["tz"]))
    return out


def burning_promises():
    """Обещания, у которых срок сегодня или уже прошёл.

    Сторож без демона: список и так пересобирается на каждом ходе, а горящее
    обязано попадаться на глаза само. Обещание, о котором помнит только тот,
    кто его дал, — это не обещание, а надежда.
    """
    try:
        sys.path.insert(0, os.path.expanduser("~/.claude/hooks"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "chat_note", os.path.expanduser("~/.claude/hooks/chat_note.py"))
        cn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cn)
    except Exception as e:
        print("  ⚠ сторож обещаний не собран: %s" % broken(e))
        return []

    out = []
    try:
        names = os.listdir(cn.CHATS_DIR)
    except OSError as e:
        print("  ⚠ карточки чатов недоступны: %s" % e)
        return []
    for name in sorted(names):
        if not name.endswith(".md"):
            continue
        try:
            body = open(os.path.join(cn.CHATS_DIR, name), encoding="utf-8").read()
        except OSError:
            continue
        import re as _re
        m = _re.search(r"^#\s+(.+)$", body, _re.M)
        title = (m.group(1) if m else name[:-3]).strip()
        for line in body.split("\n"):
            if not line.strip().startswith("- [ ]"):
                continue
            item = line.strip()[5:].strip()
            label, _d = cn.due_state(item)
            if label.startswith("ПРОСРОЧ") or label == "СЕГОДНЯ":
                text = item.split(" · срок")[0]
                out.append("  %s — %s (%s)" % (title, text, label))
    return out


if __name__ == "__main__":
    main()
