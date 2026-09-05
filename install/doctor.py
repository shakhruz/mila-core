#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Самодиагностика канала: почему бот молчит.

Когда канал не работает, человек видит тишину. Тишина одинакова во всех
случаях: нет токена, бот не добавлен в группу, включён режим приватности у
BotFather, забыт флаг запуска, умер демон, мост не читает журнал, права файлов
не те. Найти причину, не зная устройства, невозможно — а ставить это будут
люди, которые устройства не знают.

Здесь проверяется каждое звено по очереди, и у каждой проверки есть ответ на
вопрос «что делать», а не только «плохо». Проверки идут от простого к сложному
и не прекращаются на первой ошибке: часто сломано не одно.

Ничего не чинит сам. Диагност, который лечит без спроса, — это не диагност.

  doctor.py            # проверить всё
  doctor.py --json     # для машины
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

STATE = os.environ.get("TELEGRAM_STATE_DIR",
                       os.path.expanduser("~/.claude/channels/telegram"))
ENV_FILE = os.path.join(STATE, ".env")
ACCESS = os.path.join(STATE, "access.json")
INBOUND = os.path.join(STATE, "inbound")
EVENTS = os.path.join(INBOUND, "events.jsonl")
CURSOR = os.path.join(INBOUND, "cursor")
CHATS_DIR = os.path.join(STATE, "chats")

OK, WARN, BAD = "ok", "warn", "bad"


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, name, detail, fix=""):
        self.rows.append({"level": level, "name": name, "detail": detail, "fix": fix})

    @property
    def bad(self):
        return [r for r in self.rows if r["level"] == BAD]

    @property
    def warn(self):
        return [r for r in self.rows if r["level"] == WARN]


def read_token():
    """Токен из окружения важнее файла — так же, как его читает сервер."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tok:
        return tok.strip(), "переменная окружения"
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"\''), ENV_FILE
    except OSError:
        pass
    return None, None


def api(token, method, timeout=12):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        # Токен не должен утечь в текст ошибки — его вырезаем всегда.
        msg = str(e).replace(token, "<token>") if token else str(e)
        return {"ok": False, "error": msg}


def running(pattern):
    """[(pid, командная строка)] — кроссплатформенно.

    `pgrep -fa` отдаёт командную строку на Linux и молча игнорирует -a на
    macOS, возвращая одни pid. Диагност, написанный на Linux, на macOS
    отвечал «код: неизвестно» и «сессия не найдена» при живых процессах —
    то есть врал ровно в том, ради чего его писали. Спрашиваем pid у pgrep,
    а команду у ps: так работает везде.
    """
    try:
        out = subprocess.run(["pgrep", "-f", pattern],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return []
    pids = [p for p in out.split() if p.isdigit() and p != str(os.getpid())]
    rows = []
    for pid in pids:
        try:
            cmd = subprocess.run(["ps", "-p", pid, "-o", "command="],
                                 capture_output=True, text=True, timeout=8).stdout.strip()
        except Exception:
            cmd = ""
        if cmd and "pgrep" not in cmd:
            rows.append((pid, cmd))
    return rows


def check_token(rep):
    token, src = read_token()
    if not token:
        rep.add(BAD, "токен", "не найден",
                "получи токен у @BotFather и выполни /telegram:configure <токен>")
        return None
    if not re.match(r"^\d{8,12}:[A-Za-z0-9_-]{30,}$", token):
        rep.add(BAD, "токен", "не похож на токен Telegram (%d символов)" % len(token),
                "скопируй токен целиком, вместе с цифрами и двоеточием в начале")
        return None

    me = api(token, "getMe")
    if not me.get("ok"):
        # `%` связывает крепче, чем `or`: раньше выражение сводилось к
        # («…: %s» % error) or description, поэтому description не читался
        # никогда, а на живом отказе печаталось «None». «Unauthorized» и
        # «Conflict: terminated by other getUpdates» лечатся противоположно —
        # клиент выпускал новый токен там, где надо было убить второй процесс.
        rep.add(BAD, "токен", "Telegram не принял: %s"
                % (me.get("description") or me.get("error") or "без объяснения"),
                "токен отозван или неверен — выпусти новый у @BotFather")
        return None
    u = me["result"]
    rep.add(OK, "бот", "@%s (%s), источник: %s"
            % (u.get("username"), u.get("first_name"), src))
    return token


def check_privacy(rep, token):
    """Режим приватности решает, дойдут ли сообщения из групп вообще."""
    me = api(token, "getMe")
    u = me.get("result") or {}
    can_read = u.get("can_read_all_group_messages")
    if can_read is True:
        rep.add(OK, "режим приватности", "выключен — бот видит все сообщения группы")
    elif can_read is False:
        rep.add(WARN, "режим приватности",
                "включён — из групп доходят только упоминания и ответы",
                "это нормально для режима «по упоминанию». Для «отвечать всем» "
                "выключи: @BotFather → /setprivacy → выбери бота → Disable")
    else:
        rep.add(WARN, "режим приватности", "Telegram не сообщил состояние", "")


def check_access(rep):
    try:
        a = json.load(open(ACCESS, encoding="utf-8"))
    except FileNotFoundError:
        rep.add(WARN, "доступ", "access.json ещё нет",
                "напиши боту в личку — он ответит кодом для сопряжения")
        return
    except json.JSONDecodeError as e:
        rep.add(BAD, "доступ", "access.json не разбирается: %s" % e,
                "исправь синтаксис JSON — пока файл битый, канал работает "
                "на настройках по умолчанию")
        return

    allow = a.get("allowFrom") or []
    owners = a.get("owners") or []
    groups = a.get("groups") or {}
    policy = a.get("dmPolicy", "pairing")

    if not allow:
        rep.add(WARN, "личка", "никто не допущен",
                "напиши боту и выполни /telegram:access pair <код>")
    else:
        rep.add(OK, "личка", "допущено %d, политика «%s»" % (len(allow), policy))

    if policy == "pairing" and allow:
        rep.add(WARN, "личка", "режим сопряжения всё ещё открыт",
                "любой, кто найдёт бота, получит код. Закрой: "
                "/telegram:access policy allowlist")

    if len(allow) > 1 and not owners:
        rep.add(WARN, "владельцы", "в личку допущены %d, список owners пуст" % len(allow),
                "решать сможет каждый из них — включая одобрение запуска "
                "инструментов. Задай owners в access.json")
    elif owners:
        rep.add(OK, "владельцы", "решают %d из %d допущенных" % (len(owners), len(allow)))

    rep.add(OK if groups else WARN, "группы",
            "подключено %d" % len(groups),
            "" if groups else "добавь бота в группу и отправь /channel_join")

    loud = [g for g, p in groups.items()
            if isinstance(p, dict) and p.get("requireMention") is False
            and not p.get("readOnly")]
    if loud:
        rep.add(WARN, "группы",
                "%d в режиме «отвечать всем»" % len(loud),
                "в них каждое сообщение любого участника попадает в сессию — "
                "оставляй так только там, где доверяешь всем")


def check_daemon(rep):
    """Демон и сессия не должны опрашивать Telegram одновременно."""
    lines = running("receiver.ts")
    if not lines:
        rep.add(WARN, "демон", "не запущен",
                "без него сообщения, пришедшие при выключенной сессии, теряются. "
                "Запусти receiver.ts как службу с RECEIVER_DAEMON=1")
        return
    if len(lines) > 1:
        rep.add(BAD, "демон", "запущено %d копий" % len(lines),
                "две копии дерутся за один токен, Telegram отвечает 409 и "
                "сообщения теряются. Оставь одну")
        return
    pid, cmd = lines[0]
    m = re.search(r"(\S+receiver\.ts)", cmd)
    src = m.group(1) if m else "путь не разобран"
    rep.add(OK, "демон", "работает (pid %s), код: %s" % (pid, src))


def check_journal(rep):
    if not os.path.exists(EVENTS):
        rep.add(WARN, "журнал", "ещё пуст", "напиши боту — появится первая запись")
        return
    size = os.path.getsize(EVENTS)
    age = time.time() - os.path.getmtime(EVENTS)
    rep.add(OK, "журнал", "%d байт, последняя запись %d мин назад"
            % (size, int(age // 60)))

    try:
        cur = int(open(CURSOR).read().strip() or 0)
    except Exception:
        cur = 0
    behind = size - cur
    if behind > 0:
        rep.add(WARN, "мост", "не дочитано %d байт" % behind,
                "сессия не доиграла входящие. Перезапусти сессию с флагом канала "
                "— всё, что накопилось, придёт")
    else:
        rep.add(OK, "мост", "журнал прочитан целиком")


def check_perms(rep):
    """Кто может писать в журнал — говорит голосом Telegram."""
    targets = (
        (STATE, 0o700), (INBOUND, 0o700), (CHATS_DIR, 0o700),
        (EVENTS, 0o600), (ENV_FILE, 0o600), (ACCESS, 0o600),
        (os.path.join(STATE, "auth-log.jsonl"), 0o600),
        (os.path.join(STATE, "permission-log.jsonl"), 0o600),
        (os.path.join(STATE, "permissions.json"), 0o600),
    )
    for path, want in targets:
        if not os.path.exists(path):
            continue
        st = os.stat(path)
        mode = st.st_mode & 0o777
        name = os.path.basename(path) or path
        # Сравнивать числом нельзя: 0444 меньше 0600, а читать токен всему
        # миру оно разрешает. Смотреть надо на биты, которых быть не должно.
        extra = mode & ~want
        if extra:
            rep.add(BAD, "права", "%s — %o (лишние биты %o)" % (name, mode, extra),
                    "chmod %o %s — иначе чужой процесс на этой машине может "
                    "подделать входящее или прочитать токен" % (want, path))
        elif st.st_uid != os.getuid():
            rep.add(BAD, "права", "%s принадлежит другому пользователю (uid %d)"
                    % (name, st.st_uid),
                    "chown $(id -un) %s — иначе агент не сможет туда писать"
                    % path)
        else:
            rep.add(OK, "права", "%s — %o" % (name, mode))


def check_flag(rep):
    """Подключён ли канал к сессии.

    Искать флаг в командной строке `claude` бесполезно: под шаблон «claude»
    попадают все процессы, у которых в пути есть каталог .claude — демон,
    отправитель, сам MCP-сервер. Точный признак другой: если канал подключён,
    Claude Code ЗАПУСТИЛ mcp-сервер плагина. Нет процесса — нет канала,
    сколько бы флагов ни было в команде.
    """
    # Живой сервер выглядит как «bun server.ts» — без единого упоминания
    # канала: рабочий каталог задан отдельно, в команде его не видно.
    # Поэтому смотрим на две улики: сам server.ts и запускающую его строку
    # «bun run --cwd <плагин> ... start», где имя канала как раз есть.
    servers = [cmd for _pid, cmd in running("server.ts")]
    servers += [cmd for _pid, cmd in running("--cwd")
                if "telegram" in cmd and "start" in cmd]
    if servers:
        rep.add(OK, "канал в сессии", "MCP-сервер плагина запущен")
        return
    sessions = [cmd for _pid, cmd in running("claude")
                if re.search(r"/(claude)(\s|$)", cmd)]
    if sessions:
        rep.add(BAD, "канал в сессии", "сессия работает, сервер канала не запущен",
                "перезапусти через `mila` — без флага канала плагин не "
                "подключается, и входящие остаются в журнале")
    else:
        rep.add(WARN, "канал в сессии", "сессия не найдена",
                "запусти `mila`")


def main():
    ap = argparse.ArgumentParser(description="Диагностика канала Telegram")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    rep = Report()
    token = check_token(rep)
    if token:
        check_privacy(rep, token)
    check_access(rep)
    check_daemon(rep)
    check_journal(rep)
    check_perms(rep)
    check_flag(rep)

    if args.as_json:
        json.dump({"rows": rep.rows, "bad": len(rep.bad), "warn": len(rep.warn)},
                  sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 1 if rep.bad else 0

    mark = {OK: "🟢", WARN: "🟡", BAD: "🔴"}
    print("Диагностика канала · %s" % STATE)
    print()
    for r in rep.rows:
        print("%s %-18s %s" % (mark[r["level"]], r["name"], r["detail"]))
        if r["fix"]:
            for i, chunk in enumerate(wrap(r["fix"], 66)):
                print("   %s %s" % ("→" if i == 0 else " ", chunk))
    print()
    if rep.bad:
        print("Сломано: %d. Канал не работает, пока это не починено." % len(rep.bad))
    elif rep.warn:
        print("Замечаний: %d. Канал работает, но не так, как мог бы." % len(rep.warn))
    else:
        print("🟢 Всё звенья на месте.")
    return 1 if rep.bad else 0


def wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    sys.exit(main())
