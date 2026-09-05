#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Запись в карточку чата: назначение, участники, договорённости, заметки.

Карточку заводит механика (chats_index.py), содержание ведёт агент. До этого
инструмента «ведёт» означало «открывает файл и переписывает руками», а значит
на практике не ведёт: в двадцати чатах карточки оставались скелетами, и к концу
дня никто не помнил, о чём договорились в десятом.

Здесь запись — одна команда, поэтому она случается по ходу разговора, а не
когда-нибудь потом.

  chat_note.py <chat_id> --purpose "О чём этот чат"
  chat_note.py <chat_id> --member "@ivan — принимает решения по срокам"
  chat_note.py <chat_id> --promise "прототип глобуса" --due "27.08"
  chat_note.py <chat_id> --done "прототип глобуса"
  chat_note.py <chat_id> --note "клиент просит не писать после 22:00"
  chat_note.py <chat_id> --show

Договорённости хранятся строками с датой и состоянием. Закрытие требует
совпадения по тексту — «сделала» без записи не проходит: обещание закрывается
тем же способом, каким открывалось.
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta

STATE = os.environ.get("TELEGRAM_STATE_DIR",
                       os.path.expanduser("~/.claude/channels/telegram"))
CHATS_DIR = os.path.join(STATE, "chats")

SECTIONS = ("Назначение", "Участники", "Договорённости", "Заметки", "Резюме")
PLACEHOLDERS = ("(заполняет Мила", "(пока не видела", "(что обещано",
                "Пока нет", "(заполняет")


WEEKDAYS = {"понедельник": 0, "вторник": 1, "среда": 2, "среду": 2, "четверг": 3,
            "пятница": 4, "пятницу": 4, "суббота": 5, "субботу": 5,
            "воскресенье": 6, "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4}

# Месяцы в том падеже, в каком их пишут в сроке: «до 27 августа».
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
          "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10,
          "ноября": 11, "декабря": 12}


def due_date(text, now=None):
    """Дата из свободной записи срока. None — срок не машиночитаемый.

    Сроки пишутся так, как их произнёс человек: «сегодня вечером 25.08»,
    «26.08 днём», «четверг 27.08», «до выкатки, дата не названа». Разбираем
    то, что разбирается, а остальное честно помечаем как непроверяемое —
    догадка о сроке хуже отсутствия срока, потому что по ней успокаиваются.
    """
    now = now or datetime.now().astimezone()
    t = (text or "").lower()

    # Слова разбираем ПЕРВЫМИ. Числовой поиск, стоявший здесь раньше, читал
    # «сегодня к 8.10» как восьмое октября: обещание на сегодняшний вечер
    # уезжало на полтора месяца вперёд и сторож молчал именно в тот день,
    # когда должен был кричать. «Сегодня» в записи всегда сильнее любых цифр
    # рядом — это то, что человек сказал вслух.
    if "сегодня" in t:
        return now.date()
    if "послезавтра" in t:
        return (now + timedelta(days=2)).date()
    if "завтра" in t:
        return (now + timedelta(days=1)).date()

    for name, idx in WEEKDAYS.items():
        if re.search(r"\b%s\b" % name, t):
            ahead = (idx - now.weekday()) % 7
            return (now + timedelta(days=ahead or 7)).date()

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=now.tzinfo).date()
        except ValueError:
            return None

    m = re.search(r"\b(\d{1,2})\s+(%s)\b" % "|".join(MONTHS), t)
    if m:
        try:
            return datetime(now.year, MONTHS[m.group(2)], int(m.group(1)),
                            tzinfo=now.tzinfo).date()
        except ValueError:
            return None

    for m in re.finditer(r"(?:^|[^\d])(?:(к|в|до|к\s)\s*)?"
                         r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b", t):
        prefix, day, mon = m.group(1), int(m.group(2)), int(m.group(3))
        # «к 8.10», «в 14.30» — это время суток, а не дата. Признак: предлог
        # времени впереди и оба числа укладываются в часы и минуты.
        if prefix and day <= 23 and mon <= 59:
            continue
        year = int(m.group(4) or now.year)
        if year < 100:
            year += 2000
        try:
            return datetime(year, mon, day, tzinfo=now.tzinfo).date()
        except ValueError:
            continue
    return None


def due_part(line):
    """Вырезает только срок из строки обещания.

    Строка выглядит так: «текст · срок 26.08 днём · записано 25.08». Первая
    версия разбирала её целиком и находила дату записи — три обещания со
    сроком «до выкатки, дата не названа» получили метку СЕГОДНЯ, потому что
    рядом стояло «записано 25.08». Служебный хвост не срок.
    """
    m = re.search(r"·\s*срок\s*(.+?)(?:·\s*записано|$)", line or "")
    return m.group(1) if m else ""


def due_state(text, now=None):
    """(метка, дата) — «просрочено», «сегодня», «завтра», «через N дней», «—»."""
    now = now or datetime.now().astimezone()
    d = due_date(due_part(text), now)
    if d is None:
        return ("срок не назван", None)
    delta = (d - now.date()).days
    if delta < 0:
        return ("ПРОСРОЧЕНО на %d дн." % -delta, d)
    if delta == 0:
        return ("СЕГОДНЯ", d)
    if delta == 1:
        return ("завтра", d)
    return ("через %d дн." % delta, d)


def card_path(chat_id):
    return os.path.join(CHATS_DIR, "%s.md" % chat_id)


def read_card(chat_id):
    p = card_path(chat_id)
    if not os.path.exists(p):
        print("карточки нет: %s\nсначала обнови реестр: chats_index.py" % p,
              file=sys.stderr)
        raise SystemExit(1)
    return open(p, encoding="utf-8").read()


def write_card(chat_id, text):
    p = card_path(chat_id)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def ensure_section(text, name):
    """Секции может не быть — старые карточки заводились без «Договорённостей»."""
    if re.search(r"^##\s+%s\s*$" % re.escape(name), text, re.M):
        return text
    return text.rstrip() + "\n\n## %s\n\n" % name


def section_bounds(text, name):
    m = re.search(r"^##\s+%s\s*$" % re.escape(name), text, re.M)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^##\s+", text[start:], re.M)
    end = start + nxt.start() if nxt else len(text)
    return start, end


def append_to(text, name, line):
    text = ensure_section(text, name)
    start, end = section_bounds(text, name)
    body = text[start:end].strip("\n")
    # Заглушку не копим — заменяем: «(заполняет Мила)» рядом с настоящей
    # записью выглядит как недоделка, хотя работа уже сделана.
    kept = [l for l in body.split("\n")
            if l.strip() and not any(l.strip().startswith(p) for p in PLACEHOLDERS)]
    # Механика вписывает участников голыми именами («@ivan, @petr»), агент
    # потом добавляет их с ролью. Без этой сверки один человек стоит в списке
    # дважды, и карточка сразу выглядит как черновик.
    handles = re.findall(r"@[\w\d_]+", line)
    if handles:
        kept = [l for l in kept
                if not (any(h in l for h in handles) and "—" not in l and "·" not in l)]
        kept = [l for l in kept if l.strip() not in handles]
    if line not in kept:
        kept.append(line)
    return text[:start] + "\n" + "\n".join(kept) + "\n\n" + text[end:].lstrip("\n")


def replace_section(text, name, body):
    text = ensure_section(text, name)
    start, end = section_bounds(text, name)
    return text[:start] + "\n" + body.strip() + "\n\n" + text[end:].lstrip("\n")


def today():
    return datetime.now().astimezone().strftime("%d.%m")


def main():
    ap = argparse.ArgumentParser(description="Запись в карточку чата")
    ap.add_argument("chat_id", nargs="?", default="--open",
                    help="id чата; без него — список всех открытых обещаний")
    ap.add_argument("--purpose", help="о чём этот чат (заменяет раздел целиком)")
    ap.add_argument("--member", help="участник и его роль")
    ap.add_argument("--promise", help="что обещано")
    ap.add_argument("--due", default="", help="срок обещания")
    ap.add_argument("--done", help="закрыть обещание (по совпадению текста)")
    ap.add_argument("--note", help="факт или правило этого чата")
    ap.add_argument("--show", action="store_true", help="показать карточку")
    args = ap.parse_args()

    # Ради этого всё и затевалось: один список того, что я должна людям.
    # Обещания, разложенные по двадцати карточкам, — это не список, это архив.
    if args.chat_id in ("--open", "open", "all"):
        rows = []
        for name in sorted(os.listdir(CHATS_DIR)) if os.path.isdir(CHATS_DIR) else []:
            if not name.endswith(".md"):
                continue
            body = open(os.path.join(CHATS_DIR, name), encoding="utf-8").read()
            title = (re.search(r"^#\s+(.+)$", body, re.M) or [None, name[:-3]])[1]
            for line in body.split("\n"):
                if line.strip().startswith("- [ ]"):
                    rows.append((title.strip(), line.strip()[5:].strip()))
        if not rows:
            print("открытых обещаний нет.")
            return 0
        scored = []
        for title, item in rows:
            label, d = due_state(item)
            # Сначала горящее: список читают, чтобы не пропустить срок,
            # а не чтобы полюбоваться алфавитом клиентов.
            rank = 0 if label.startswith("ПРОСРОЧ") else (
                1 if label == "СЕГОДНЯ" else (2 if label == "завтра" else (
                    4 if d is None else 3)))
            scored.append((rank, d or datetime.max.date(), title, item, label))
        scored.sort(key=lambda x: (x[0], x[1]))
        hot = sum(1 for r in scored if r[0] <= 1)
        print("Открытые обещания · %d%s"
              % (len(scored), (" · горит %d" % hot) if hot else ""))
        print()
        last = None
        for rank, _d, title, item, label in scored:
            if title != last:
                print("  %s" % title)
                last = title
            mark = {0: "🔴", 1: "🔴", 2: "⏳", 3: "·", 4: "?"}[rank]
            print("    %s %-58s %s" % (mark, item[:58], label))
        if any(r[0] == 4 for r in scored):
            print()
            print("«?» — срок записан словами, которые я не умею проверить.")
            print("Перепиши датой (--promise ... --due 27.08), иначе сторож их не увидит.")
        return 0

    text = read_card(args.chat_id)

    if args.show:
        print(text)
        return 0

    changed = []

    if args.purpose:
        text = replace_section(text, "Назначение", args.purpose)
        changed.append("назначение")

    if args.member:
        text = append_to(text, "Участники", "· %s" % args.member)
        changed.append("участник")

    if args.promise:
        due = (" · срок %s" % args.due) if args.due else " · срок не назван"
        text = append_to(text, "Договорённости",
                         "- [ ] %s%s · записано %s" % (args.promise, due, today()))
        changed.append("обещание")

    if args.done:
        start_end = section_bounds(ensure_section(text, "Договорённости"),
                                   "Договорённости")
        text = ensure_section(text, "Договорённости")
        start, end = section_bounds(text, "Договорённости")
        body = text[start:end]
        needle = args.done.lower()
        lines, hit = [], False
        for line in body.split("\n"):
            if not hit and line.strip().startswith("- [ ]") and needle in line.lower():
                line = line.replace("- [ ]", "- [x]", 1) + " · закрыто %s" % today()
                hit = True
            lines.append(line)
        if not hit:
            print("не нашла открытого обещания со словами «%s».\n"
                  "Обещание закрывается тем же способом, каким открывалось — "
                  "проверь текст в карточке (--show)." % args.done, file=sys.stderr)
            return 1
        text = text[:start] + "\n".join(lines) + text[end:]
        changed.append("закрыто обещание")

    if args.note:
        text = append_to(text, "Заметки", "· %s (%s)" % (args.note, today()))
        changed.append("заметка")

    if not changed:
        print("нечего записывать — укажи --purpose / --member / --promise / "
              "--done / --note, либо --show", file=sys.stderr)
        return 1

    write_card(args.chat_id, text)
    print("🟢 %s: %s" % (args.chat_id, ", ".join(changed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
