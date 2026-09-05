#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Язык и часовой пояс чата: чтобы «доброе утро» приходило утром.

Тихие часы 22–08 бессмысленны без ответа на вопрос «чьи 22». Пока пояс не
задан, они считаются по поясу владельца — то есть для клиента в Техасе
«утреннее» сообщение приходит ночью. Это не теория: у нас такой клиент есть.

Язык здесь — не догадка по последнему сообщению, а решение. Собеседник может
написать одну фразу по-английски из вежливости, и это не повод перевести на
английский весь разговор.

И язык не один, а три роли, потому что у живого клиента они расходятся:

  говорим (speak)   на каком языке я пишу В ЧАТ
  публикуем (publish) на каком языке выходит РАБОТА — пост, сайт, афиша
  ожидаем (expect)  какие языки могут прийти, не переключая разговор

Клиника переписывается по-русски, а посты выходят по-узбекски. Студия в Дубае
переписывается по-русски, а сайт у неё английский. Агентство говорит по-русски,
но его собственные клиенты пишут и по-узбекски. Одно поле «язык» на всё это
даёт ровно одну ошибку: материал сдаётся на языке переписки, а не на языке
аудитории — и переделывается целиком.

Хранится в самом access.json, рядом с режимом чата: одно место состояния
лучше двух, которые разъедутся.

  chat_locale.py                            # показать всё
  chat_locale.py <id>                       # карточка одного чата
  chat_locale.py <id> --tz Asia/Dubai       # задать пояс
  chat_locale.py <id> --speak ru            # язык переписки
  chat_locale.py <id> --publish uz,ru       # язык материалов, главный первым
  chat_locale.py <id> --expect ru,uz        # что может прийти во входящих
  chat_locale.py <id> --quiet 22-08         # свои тихие часы
  chat_locale.py --now                      # который час у каждого клиента
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

STATE = os.environ.get("TELEGRAM_STATE_DIR",
                       os.path.expanduser("~/.claude/channels/telegram"))
ACCESS = os.path.join(STATE, "access.json")
CHATS_MD = os.path.join(STATE, "CHATS.md")


def load():
    with open(ACCESS, encoding="utf-8") as f:
        return json.load(f)


def save(a):
    tmp = ACCESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(a, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, ACCESS)


def names():
    """Названия из реестра. Форматов два, и это не случайность.

    CHATS.md пишут двое: плагин при каждой авторизации (строками-списком) и
    chats_index.py (таблицей со статистикой). Кто последний, того и формат.
    Спорить с этим здесь не место — просто понимаем оба, иначе имена клиентов
    исчезают ровно тогда, когда кто-то подключил или отключил чат.
    """
    out = {}
    try:
        for line in open(CHATS_MD, encoding="utf-8"):
            m = (re.match(r"\|\s*([^|]+?)\s*\|\s*`(-?\d+)`", line)
                 or re.match(r"[-*]\s*(.+?)\s*·\s*`(-?\d+)`", line))
            if m and not m.group(1).startswith("_"):
                out[m.group(2)] = m.group(1).strip()
    except OSError:
        pass
    return out


LANGS = ("ru", "uz", "en")


def lang_list(text):
    """«uz,ru» → ['uz', 'ru']. Порядок значим: первый — главный.

    Материал на двух языках почти всегда имеет ведущий: на нём пишут, второй
    идёт переводом под ним. Терять этот порядок нельзя — «ru,uz» и «uz,ru»
    дают разные посты, и клиент замечает разницу первым.
    """
    out = []
    for part in re.split(r"[,\s]+", (text or "").strip().lower()):
        if not part:
            continue
        if part not in LANGS:
            return None
        if part not in out:
            out.append(part)
    return out or None


def speaks(g):
    """Что известно про языки чата — в одном месте, чтобы читалось одинаково."""
    return (g.get("lang"), g.get("publish") or [], g.get("expect") or [])


def lang_line(g):
    speak, publish, expect = speaks(g)
    bits = []
    if speak:
        bits.append("говорим %s" % speak)
    if publish:
        bits.append("публикуем %s" % "+".join(publish))
    if expect and expect != ([speak] if speak else []):
        bits.append("ждём %s" % "/".join(expect))
    return " · ".join(bits)


def quiet_parse(text):
    m = re.match(r"^(\d{1,2})\s*[-–]\s*(\d{1,2})$", (text or "").strip())
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if not (0 <= a <= 23 and 0 <= b <= 23):
        return None
    return [a, b]


def local_now(tz):
    if not tz or ZoneInfo is None:
        return None
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:
        return None


def in_quiet(now, quiet):
    """Тихие часы могут переходить через полночь — 22–08 это не 22 < h < 8."""
    if not now or not quiet:
        return False
    a, b = quiet
    h = now.hour
    return (a <= h or h < b) if a > b else (a <= h < b)


def main():
    ap = argparse.ArgumentParser(description="Язык и часовой пояс чата")
    ap.add_argument("chat_id", nargs="?")
    ap.add_argument("--tz", help="часовой пояс IANA, например Asia/Dubai")
    ap.add_argument("--speak", "--lang", dest="speak",
                    help="язык переписки в чате: ru / uz / en")
    ap.add_argument("--publish",
                    help="язык материалов, главный первым: uz,ru")
    ap.add_argument("--expect",
                    help="какие языки могут прийти во входящих: ru,uz")
    ap.add_argument("--quiet", help="тихие часы, например 22-08")
    ap.add_argument("--now", action="store_true",
                    help="который час у каждого чата и можно ли писать")
    args = ap.parse_args()

    try:
        a = load()
    except (OSError, json.JSONDecodeError) as e:
        print("access.json недоступен: %s" % e, file=sys.stderr)
        return 1

    groups = a.get("groups") or {}
    title = names()

    if args.chat_id and (args.tz or args.speak or args.publish
                         or args.expect or args.quiet):
        cid = args.chat_id
        if cid not in groups:
            print("чат %s не подключён" % cid, file=sys.stderr)
            return 1
        g = groups[cid]
        if args.tz:
            if ZoneInfo is None:
                print("нет модуля zoneinfo — обнови Python", file=sys.stderr)
                return 1
            try:
                ZoneInfo(args.tz)
            except Exception:
                print("не знаю такого пояса: %s\n"
                      "нужен формат IANA: Asia/Tashkent, Asia/Dubai, "
                      "America/Chicago, Europe/Nicosia" % args.tz, file=sys.stderr)
                return 1
            g["tz"] = args.tz
        if args.speak:
            if args.speak not in LANGS:
                print("язык переписки: ru, uz или en", file=sys.stderr)
                return 1
            g["lang"] = args.speak
        if args.publish:
            got = lang_list(args.publish)
            if not got:
                print("языки материалов через запятую из ru/uz/en, "
                      "главный первым: --publish uz,ru", file=sys.stderr)
                return 1
            g["publish"] = got
        if args.expect:
            got = lang_list(args.expect)
            if not got:
                print("ожидаемые языки через запятую из ru/uz/en: "
                      "--expect ru,uz", file=sys.stderr)
                return 1
            g["expect"] = got
        if args.quiet:
            q = quiet_parse(args.quiet)
            if not q:
                print("тихие часы задаются как 22-08", file=sys.stderr)
                return 1
            g["quiet"] = q
        save(a)
        print("🟢 %s" % title.get(cid, cid))
        if args.tz:
            print("   пояс %s" % args.tz)
        line = lang_line(g)
        if line:
            print("   %s" % line)
        if args.quiet:
            print("   тишина %s" % args.quiet)
        # Расхождение речи и публикации — не ошибка, а самый частый случай:
        # переписка по-русски, посты по-узбекски. Говорим об этом вслух, чтобы
        # материал не сдавался на языке переписки.
        speak, publish, _ = speaks(g)
        if speak and publish and publish[0] != speak:
            print("   ⚠ пишем в чат на %s, материалы сдаём на %s — "
                  "не перепутать" % (speak, publish[0]))
        return 0

    if args.chat_id:
        cid = args.chat_id
        g = groups.get(cid)
        if not isinstance(g, dict):
            print("чат %s не подключён" % cid, file=sys.stderr)
            return 1
        tz = g.get("tz")
        now = local_now(tz)
        print(title.get(cid, cid))
        print("  пояс     %s" % (
            "%s (%s)" % (tz, now.strftime("%H:%M")) if now else tz or "не задан"))
        speak, publish, expect = speaks(g)
        print("  говорим  %s" % (speak or "по языку собеседника"))
        print("  сдаём    %s" % ("+".join(publish) if publish
                                 else "как в переписке"))
        print("  ждём     %s" % ("/".join(expect) if expect else "—"))
        q = g.get("quiet") or [22, 8]
        print("  тишина   %d–%d%s" % (q[0], q[1],
                                      "  🔴 сейчас" if in_quiet(now, q) else ""))
        return 0

    rows = []
    for cid, g in groups.items():
        if not isinstance(g, dict):
            continue
        tz = g.get("tz")
        lang = lang_line(g)
        quiet = g.get("quiet") or [22, 8]
        now = local_now(tz)
        rows.append((title.get(cid, cid), cid, tz, lang, quiet, now))

    if args.now:
        rows = [r for r in rows if r[2]]
        if not rows:
            print("Ни одному чату не задан часовой пояс.")
            print("Задай: chat_locale.py <id> --tz Asia/Dubai")
            return 0
        rows.sort(key=lambda r: r[5].utcoffset())
        print("Который час у клиентов · %s"
              % datetime.now().astimezone().strftime("%H:%M у меня"))
        print()
        for name, cid, tz, lang, quiet, now in rows:
            quietly = in_quiet(now, quiet)
            print("  %-30s %s  %s  %s"
                  % (name[:30], now.strftime("%H:%M"), tz,
                     "🔴 тихие часы" if quietly else "🟢 можно писать"))
        return 0

    known = [r for r in rows if r[2] or r[3]]
    print("Чатов подключено: %d · настроено: %d" % (len(rows), len(known)))
    print()
    if known:
        for name, cid, tz, lang, quiet, now in sorted(known, key=lambda r: r[0]):
            bits = []
            if tz:
                bits.append(tz + (" (%s)" % now.strftime("%H:%M") if now else ""))
            if lang:
                bits.append(lang)
            if quiet != [22, 8]:
                bits.append("тишина %d–%d" % tuple(quiet))
            print("  %-26s %s" % (name[:26], " · ".join(bits)))
        print()
    print("Остальные считаются по моему поясу и языку собеседника.")
    print("Это молчаливое допущение: для клиента в другом полушарии оно")
    print("означает, что «утреннее» сообщение придёт ему ночью.")
    print()
    print("Язык материалов задаётся отдельно от языка переписки:")
    print("  chat_locale.py <id> --speak ru --publish uz,ru --expect ru,uz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
