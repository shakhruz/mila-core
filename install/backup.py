#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Бэкап того, что нельзя восстановить.

Код лежит в git, модель живёт у поставщика, а вот это — нигде: журнал
входящих, карточки чатов с договорённостями, реестр доступов, скиллы,
собранные из боевых ошибок, журнал расхода. Потерять сервер значит потерять
именно память, а не программу.

Начинаем со `status`, а не с самих бэкапов: сначала честный ответ на вопрос
«что и когда сохранено», потом уже сохранение. Бэкап, о котором никто не
знает, работает ли он, — это не бэкап, а надежда.

Секреты в архив НЕ попадают: токен бота и ключи остаются на машине. Архив
переживает и переезжает, а секрет, уехавший в архив, потом всплывает в
чужой копии.

  backup.py status          # что сохранено, когда, чего не хватает
  backup.py run             # создать архив
  backup.py run --to user@host:/srv/archives   # и отправить копию
  backup.py run --out DIR   # положить в конкретное место
  backup.py list            # какие архивы есть
"""
import argparse
import io
import json
import os
import re
import sys
import tarfile
import time
from datetime import datetime, timezone

CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
STATE = os.environ.get("TELEGRAM_STATE_DIR",
                       os.path.join(CLAUDE_DIR, "channels", "telegram"))
DEFAULT_OUT = os.path.expanduser("~/mila-backups")

# Что кладём. Каждая строка — ответ на вопрос «что нельзя восстановить, если
# машины не станет».
ITEMS = [
    ("skills",       os.path.join(CLAUDE_DIR, "skills"),
     "навыки — правила, собранные из боевых ошибок"),
    ("hooks",        os.path.join(CLAUDE_DIR, "hooks"),
     "хуки и инструменты комплекта"),
    ("settings",     os.path.join(CLAUDE_DIR, "settings.json"),
     "настройки сессии"),
    ("chats",        os.path.join(STATE, "chats"),
     "карточки чатов: назначение, участники, договорённости"),
    ("registry",     os.path.join(STATE, "CHATS.md"),
     "реестр подключённых чатов"),
    ("access",       os.path.join(STATE, "access.json"),
     "кто допущен и в каком режиме"),
    ("auth-log",     os.path.join(STATE, "auth-log.jsonl"),
     "журнал авторизаций — кто, когда, что решил"),
    ("journal",      os.path.join(STATE, "inbound", "events.jsonl"),
     "журнал входящих: вся переписка, пришедшая через канал"),
    ("usage",        os.path.join(CLAUDE_DIR, "usage-log.jsonl"),
     "журнал расхода"),
    ("prices",       os.path.join(CLAUDE_DIR, "prices.json"),
     "таблица цен"),
]

# Не кладём никогда. Не «забыли», а решили.
NEVER = [
    (os.path.join(STATE, ".env"), "токен бота"),
    (os.path.expanduser("~/.claude/.credentials.json"), "учётные данные"),
]

# Секрет живёт не только в файле с именем «.env». Он лежит внутри settings.json
# (блок env — штатное место ключа модели), внутри журнала входящих (человек
# прислал ключ в чат), внутри карточки чата, куда его переписали. Исключить
# файл по имени недостаточно: надо чистить содержимое.
SECRET_PATTERNS = [
    (re.compile(r"\bsk[-_][A-Za-z0-9][A-Za-z0-9_-]{18,}", re.I), "api-key"),
    (re.compile(r"\b(?:pk|rk|tok|glpat|dop_v1|npm|shpat|shpss)[-_]"
                r"[A-Za-z0-9][A-Za-z0-9_-]{18,}", re.I), "api-key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}", re.I), "github-token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}", re.I), "slack-token"),
    (re.compile(r"\bSG\.[\w-]{16,}\.[\w-]{16,}"), "sendgrid"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-key-id"),
    (re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]+"), "jwt"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "telegram-token"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
                r"-----END [A-Z ]*PRIVATE KEY-----"), "private-key"),
    (re.compile(r"(?i)\b(pass(?:word)?|secret|api[_-]?key|access[_-]?token|"
                r"bearer)\b\s*[:=]\s*[\"']?([^\s\"',}]{8,})"), "assignment"),
]


def walk_files(src):
    """(абсолютный путь, путь внутри архива) для файла или всего каталога."""
    if os.path.isfile(src):
        yield src, ""
        return
    for root, _dirs, files in os.walk(src):
        for name in sorted(files):
            p = os.path.join(root, name)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            yield p, os.path.relpath(p, src)


def scrub(text):
    """Вырезать секреты из текста. Возвращает (очищенный текст, сколько)."""
    hits = 0

    def cut(m):
        nonlocal hits
        hits += 1
        # У правила про присваивание маскируем только значение, иначе из
        # архива исчезнет и само имя поля — а по нему потом ищут.
        if m.re.groups >= 2 and m.group(2):
            return m.group(0).replace(m.group(2), "[секрет вырезан]")
        return "[секрет вырезан]"

    for rx, _kind in SECRET_PATTERNS:
        text = rx.sub(cut, text)
    return text, hits


def human_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return "%.0f %s" % (n, unit) if unit != "Б" else "%d Б" % n
        n /= 1024.0
    return "%.1f ТБ" % n


def path_size(p):
    if os.path.isfile(p):
        return os.path.getsize(p)
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def archives(out_dir):
    try:
        names = [n for n in os.listdir(out_dir) if n.endswith(".tar.gz")]
    except OSError:
        return []
    rows = []
    for n in sorted(names):
        p = os.path.join(out_dir, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        rows.append((n, st.st_size, st.st_mtime))
    return rows


def remote_latest(target):
    """Последний архив на той стороне: имя и возраст в часах.

    Локальный список ничего не говорит о копии за пределами машины, а именно
    она защищает от потери машины. Сменился ключ — scp молча перестал ходить,
    а `status` месяц отвечал «🟢 Свежий», глядя на локальный каталог.
    """
    import shlex
    import subprocess
    if not target or ":" not in target:
        return None
    host, remote_dir = target.split(":", 1)
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
         "cd %s 2>/dev/null && ls -1t mila-*.tar.gz 2>/dev/null | head -1"
         % shlex.quote(remote_dir)],
        capture_output=True, text=True)
    if r.returncode != 0:
        return ("недоступен", None, (r.stderr or "").strip()[:80])
    name = r.stdout.strip()
    if not name:
        return ("пусто", None, "")
    age = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host,
         "cd %s && date -r %s +%%s 2>/dev/null || stat -f %%m %s"
         % (shlex.quote(remote_dir), shlex.quote(name), shlex.quote(name))],
        capture_output=True, text=True).stdout.strip()
    hours = None
    if age.isdigit():
        hours = (time.time() - int(age)) / 3600.0
    return (name, hours, "")


def cmd_status(out_dir, target=None):
    print("Что нельзя восстановить, если машины не станет")
    print()
    missing = []
    total = 0
    for key, path, why in ITEMS:
        if os.path.exists(path):
            size = path_size(path)
            total += size
            print("  🟢 %-10s %8s  %s" % (key, human_size(size), why))
        else:
            missing.append(key)
            print("  ⚪ %-10s %8s  %s" % (key, "нет", why))
    print()
    print("  Всего к сохранению: %s" % human_size(total))
    print()

    print("Не сохраняется намеренно")
    for path, what in NEVER:
        mark = "есть на машине" if os.path.exists(path) else "нет"
        print("  · %-22s %s" % (what, mark))
    print("  Секрет, уехавший в архив, потом всплывает в чужой копии.")
    print()

    rows = archives(out_dir)
    if not rows:
        print("🔴 Архивов нет ни одного. Никакая история пока не сохранена.")
        print("   Создать: backup.py run")
        return 1

    name, size, mtime = rows[-1]
    age_h = (time.time() - mtime) / 3600.0
    when = datetime.fromtimestamp(mtime).strftime("%d.%m %H:%M")
    print("Архивы: %d, последний %s (%s, %s)"
          % (len(rows), name, human_size(size), when))
    if age_h > 48:
        print("🔴 Последнему архиву %.0f часов. Всё, что случилось после, "
              "существует в одном экземпляре." % age_h)
        return 1
    bad = False
    if age_h > 24:
        print("🟡 Последнему локальному архиву %.0f часов." % age_h)
    else:
        print("🟢 Локальный свежий.")

    if not target:
        print()
        print("⚠ Копии за пределами машины нет. Это защита от ошибки, но не от")
        print("  потери машины — задай адрес: backup.py run --to user@host:/path")
        print("  (или переменную MILA_BACKUP_TARGET).")
        return 0

    name, hours, err = remote_latest(target)
    if name == "недоступен":
        print("🔴 Сервер копий не отвечает: %s" % (err or target))
        bad = True
    elif name == "пусто":
        print("🔴 На сервере копий нет ни одного архива: %s" % target)
        bad = True
    elif hours is None:
        print("🟡 На сервере есть %s, возраст определить не удалось." % name)
    elif hours > 48:
        print("🔴 Копии на сервере %.0f часов: %s" % (hours, name))
        bad = True
    else:
        print("🟢 Копия на сервере свежая: %s (%.0f ч)" % (name, hours))
    return 1 if bad else 0


def cmd_run(out_dir, target=None, keep=7):
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    host = os.uname().nodename.split(".")[0]
    path = os.path.join(out_dir, "mila-%s-%s.tar.gz" % (host, stamp))
    tmp = path + ".part"

    manifest = {"created": datetime.now(timezone.utc).astimezone().isoformat(),
                "host": host, "items": [], "excluded": [w for _p, w in NEVER]}

    added = 0
    cleaned = 0
    with tarfile.open(tmp, "w:gz") as tar:
        for key, src, why in ITEMS:
            if not os.path.exists(src):
                continue

            # Каждый файл — через вычищенный поток, и одиночный, и лежащий в
            # глубине каталога. Исключить по имени мало: ключ модели штатно
            # лежит внутри settings.json, ключ, присланный клиентом, — внутри
            # журнала, а переписанный руками — внутри карточки чата. Раньше всё
            # это уезжало по scp под подписью «секреты исключены».
            for abs_path, rel in walk_files(src):
                base = os.path.basename(abs_path)
                if base in (".env", ".credentials.json") or base.endswith(".key"):
                    continue
                raw = open(abs_path, "rb").read()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    pass          # двоичное не чистим и не читаем
                else:
                    text, hits = scrub(text)
                    raw = text.encode("utf-8")
                    cleaned += hits
                info = tarfile.TarInfo(
                    os.path.join("mila", key, rel) if rel
                    else os.path.join("mila", key))
                info.size = len(raw)
                info.mode = 0o600
                info.mtime = int(os.path.getmtime(abs_path))
                tar.addfile(info, io.BytesIO(raw))
            manifest["items"].append({"key": key, "source": src,
                                      "bytes": path_size(src), "why": why})
            added += 1

        manifest["secrets_removed"] = cleaned
        info = tarfile.TarInfo("mila/MANIFEST.json")
        blob = json.dumps(manifest, ensure_ascii=False, indent=1).encode()
        info.size = len(blob)
        tar.addfile(info, io.BytesIO(blob))

    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    size = os.path.getsize(path)
    print("🟢 %s" % path)
    print("   %s · частей: %d · вырезано секретов: %d"
          % (human_size(size), added, cleaned))

    # Проверяем то, что записали: архив, который не открывается, хуже, чем
    # его отсутствие — на него рассчитывают. И проверяем содержимое: раньше
    # здесь стояла строка «секреты исключены» — обещание, а не улика.
    try:
        found = []
        with tarfile.open(path) as tar:
            names = tar.getnames()
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                try:
                    body = fh.read().decode("utf-8")
                except UnicodeDecodeError:
                    continue
                for rx, kind in SECRET_PATTERNS:
                    if rx.search(body):
                        found.append("%s (%s)" % (member.name, kind))
                        break
        print("   проверен: %d записей читаются, просмотрено на секреты"
              % len(names))
    except Exception as e:
        print("🔴 архив не открывается: %s" % e, file=sys.stderr)
        return 1

    if found:
        # Не предупреждение, а отказ: архив с секретом внутри поедет по scp на
        # чужой хост и будет лежать там тридцать копий.
        print("🔴 в архиве остались секреты — удаляю его:", file=sys.stderr)
        for f in found[:5]:
            print("     %s" % f, file=sys.stderr)
        os.remove(path)
        return 1

    ok = True
    if target:
        ok = ship(path, target)
    # Ротацию делаем только после удачной отправки: иначе неудачная серия
    # съест локально последнюю копию, которая успела уехать.
    if ok:
        rotate_local(out_dir, keep)
    else:
        print("   локальные архивы не ротирую — копия не уехала")
    return 0 if ok else 1


def ship(path, target, keep_remote=30):
    """Отправка архива за пределы машины.

    Архив, лежащий на той же машине, защищает от ошибки и не защищает от
    потери машины — а это разные вещи, и клиенту надо говорить, какую из
    двух он купил. Здесь вторая: копия уезжает по ssh на другой хост.

    target — «user@host:/path». Ключ должен быть уже настроен: пароль в
    скрипте бэкапа означает, что бэкап однажды остановится и никто не заметит.
    """
    import shlex, subprocess
    if ":" not in target:
        print("цель должна быть вида user@host:/path", file=sys.stderr)
        return False
    host, remote_dir = target.split(":", 1)

    r = subprocess.run(
        ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
         path, target],
        capture_output=True, text=True)
    if r.returncode != 0:
        print("🔴 отправка не удалась: %s" % (r.stderr.strip() or r.returncode),
              file=sys.stderr)
        return False

    name = os.path.basename(path)
    remote_file = os.path.join(remote_dir, name)
    # Проверяем ТАМ, а не здесь: «scp вернул ноль» и «файл лежит целым» —
    # разные утверждения, и расходятся они молча.
    local_size = os.path.getsize(path)
    check = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host,
         "stat -c %%s %s 2>/dev/null || stat -f %%z %s" % (
             shlex.quote(remote_file), shlex.quote(remote_file))],
        capture_output=True, text=True)
    remote_size = (check.stdout.strip() or "0").split()[0]
    if not remote_size.isdigit() or int(remote_size) != local_size:
        print("🔴 на сервере файл другого размера (%s против %d) — не считаю "
              "отправленным" % (remote_size, local_size), file=sys.stderr)
        return False
    print("   отправлено: %s · сверен размер" % target)

    # Владельца выравниваем по каталогу: файл, положенный root'ом в каталог
    # обычного пользователя, тот не сможет ни прочитать, ни удалить — а
    # восстанавливаются из бэкапа обычно не под root.
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host,
         "owner=$(stat -c %%U %s) && chown $owner:$owner %s 2>/dev/null || true"
         % (shlex.quote(remote_dir), shlex.quote(remote_file))],
        capture_output=True, text=True)

    # Ротация на той стороне: без неё диск сервера тихо заполнится за год.
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host,
         "cd %s && ls -1t mila-*.tar.gz 2>/dev/null | tail -n +%d | xargs -r rm -f"
         % (shlex.quote(remote_dir), keep_remote + 1)],
        capture_output=True, text=True)
    return True


def rotate_local(out_dir, keep=7):
    """Локально держим неделю: остальное живёт на сервере."""
    rows = archives(out_dir)
    for name, _size, _mtime in rows[:-keep] if len(rows) > keep else []:
        try:
            os.remove(os.path.join(out_dir, name))
            print("   удалён старый: %s" % name)
        except OSError:
            pass


def cmd_list(out_dir):
    rows = archives(out_dir)
    if not rows:
        print("Архивов нет: %s" % out_dir)
        return 1
    print("Архивы в %s" % out_dir)
    print()
    for name, size, mtime in rows:
        print("  %-42s %8s  %s"
              % (name, human_size(size),
                 datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M")))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Бэкап памяти агента")
    ap.add_argument("command", nargs="?", default="status",
                    choices=["status", "run", "list"])
    ap.add_argument("--out", default=DEFAULT_OUT, help="куда складывать архивы")
    ap.add_argument("--to", default=os.environ.get("MILA_BACKUP_TARGET"),
                    help="куда отправить копию: user@host:/path "
                         "(или переменная MILA_BACKUP_TARGET)")
    ap.add_argument("--keep", type=int, default=7,
                    help="сколько архивов держать локально")
    args = ap.parse_args()

    out = os.path.expanduser(args.out)
    if args.command == "status":
        return cmd_status(out, args.to)
    if args.command == "run":
        return cmd_run(out, args.to, args.keep)
    return cmd_list(out)


if __name__ == "__main__":
    sys.exit(main())
