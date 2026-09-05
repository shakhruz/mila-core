#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""limit_watch.py — датчик лимита подписки Claude Code (mila-core, 05.09.2026).

Зачем. Когда подписка упирается в лимит, Claude Code отвечает HTTP 429
«You've hit your session limit · resets 2:10pm (Asia/Tashkent)», а агент с той
минуты просто МОЛЧИТ: человек пишет в Telegram, ответа нет, и никто не говорит
ему, что случилось и когда вернётся. Этот модуль читает хвост транскрипта
Claude Code, находит запись 429, берёт из неё время сброса (никогда не выдумывает
своё) и один раз на инцидент говорит человеку — с точным временем и ссылкой,
по которой он сам переключит подписку без терминала.

Что ловит (по факту из транскриптов ~/.claude/projects/*/*.jsonl):
  · запись type=assistant с error="rate_limit", apiErrorStatus=429 и quotaLimits
    {"resetsAt": <epoch>, "rateLimitType": "five_hour"|"seven_day"} —
    текст «You've hit your session limit · resets 2:10pm (TZ)» /
    «You've hit your weekly limit · resets Sep 2 at 10am (TZ)»;
  · «You've reached your <Model> limit. Run /usage-credits…» — лимит модели,
    времени сброса в нём нет → resets_at=None (так и говорим);
  · у субагентов — те же записи в <session>/subagents/*.jsonl и
    <task-notification><status>failed</status> с тем же текстом в главном.
Восстановление = первая запись ассистента без ошибки ПОСЛЕ инцидента.
«Тихо» (только для Милы Админ) = входящее человека в журнале плагина без ответа
сессии дольше N минут при отсутствии признаков лимита.

Использование (одна машина = один вызов раз в минуту, состояние в limit.json):
  limit_watch.py --json                         # что видит датчик
  limit_watch.py --notify --outbox DIR --chat ID # Мила Админ: через outbox sender-демона
  limit_watch.py --notify --tg-token-env VAR --chat ID --switch-link URL --mode companion
  limit_watch.py --dry-run                      # текст сообщения в stdout, состояние не трогает
  limit_watch.py --probe FILE.jsonl             # консерва: разбор одного файла, без состояния
  limit_watch.py --baseline                     # пометить текущий инцидент как уже объявленный
Библиотечно (кормилец Компаньона): hold = limit_watch.hold(claude_dir, pid_file).
Секретов не печатает: e-mail аккаунта маскируется, токены не читает.
"""
import argparse
import datetime as _dt
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

try:
    from zoneinfo import ZoneInfo
except Exception:                                   # noqa: BLE001
    ZoneInfo = None

CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
TG_STATE = os.environ.get("TELEGRAM_STATE_DIR", os.path.join(CLAUDE_DIR, "channels", "telegram"))
DEFAULT_TZ = os.environ.get("MILA_TZ", os.environ.get("TZ") or "Asia/Tashkent")
TAIL_BYTES = 3 * 1024 * 1024
WINDOW_H = 24                      # инциденты старше окна не считаем живыми
SILENT_MIN = 15
FILES_MAX = 6                      # сколько свежих транскриптов читать

RE_RESET = re.compile(r"resets\s+(?P<when>[^()\n]+?)\s*\((?P<tz>[^)]+)\)")
RE_LIMIT_TXT = re.compile(r"You've (?:hit|reached) your (?P<what>[^.·\n]+?) limit", re.I)
KIND_RU = {"five_hour": "сессионный, окно 5 часов",
           "seven_day": "недельный",
           "model": "лимит модели"}


# ── время ─────────────────────────────────────────────────────────────────────
def _tz(name):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:                               # noqa: BLE001
        try:
            return ZoneInfo(DEFAULT_TZ)
        except Exception:                           # noqa: BLE001
            return None


def iso_to_epoch(s):
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:                               # noqa: BLE001
        return None


def fmt_hm(epoch, tz_name):
    if not epoch:
        return "?"
    tz = _tz(tz_name)
    d = _dt.datetime.fromtimestamp(epoch, tz) if tz else _dt.datetime.fromtimestamp(epoch)
    return d.strftime("%H:%M")


def fmt_date_hm(epoch, tz_name):
    if not epoch:
        return "?"
    tz = _tz(tz_name)
    d = _dt.datetime.fromtimestamp(epoch, tz) if tz else _dt.datetime.fromtimestamp(epoch)
    return d.strftime("%d.%m %H:%M")


def parse_reset_text(text, hit_epoch):
    """«resets 2:10pm (Asia/Tashkent)» / «resets 12pm (…)» / «resets Sep 2 at 10am (…)»
    → (epoch, tz). Время только из текста ошибки; не разобрали — (None, tz)."""
    m = RE_RESET.search(text or "")
    if not m:
        return None, None
    when, tzn = m.group("when").strip(), m.group("tz").strip()
    tz = _tz(tzn)
    if tz is None:
        return None, tzn
    base = _dt.datetime.fromtimestamp(hit_epoch or time.time(), tz)
    fmts_time = ("%I:%M%p", "%I%p")
    for f in fmts_time:
        try:
            t = _dt.datetime.strptime(when.upper(), f)
            cand = base.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if cand < base - _dt.timedelta(minutes=5):
                cand += _dt.timedelta(days=1)
            return cand.timestamp(), tzn
        except ValueError:
            pass
    for f in ("%b %d at %I:%M%p", "%b %d at %I%p", "%B %d at %I:%M%p", "%B %d at %I%p"):
        try:
            t = _dt.datetime.strptime(when.replace("AM", "am").replace("PM", "pm"), f)
            cand = base.replace(month=t.month, day=t.day, hour=t.hour, minute=t.minute,
                                second=0, microsecond=0)
            if cand < base - _dt.timedelta(days=1):
                cand = cand.replace(year=cand.year + 1)
            return cand.timestamp(), tzn
        except ValueError:
            pass
    return None, tzn


# ── транскрипты ───────────────────────────────────────────────────────────────
def find_transcripts(claude_dir, limit=FILES_MAX):
    pats = [os.path.join(claude_dir, "projects", "*", "*.jsonl"),
            os.path.join(claude_dir, "projects", "*", "*", "subagents", "*.jsonl")]
    files = []
    for p in pats:
        files.extend(glob.glob(p))
    files = [f for f in files if os.path.isfile(f)]
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return files[:limit]


def _tail_lines(path, nbytes=TAIL_BYTES):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(size - nbytes)
                f.readline()                        # не рвать строку
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()


def _content_text(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for it in c:
            if isinstance(it, dict) and it.get("type") == "text":
                out.append(str(it.get("text") or ""))
            elif isinstance(it, dict) and it.get("type") == "tool_result":
                inner = it.get("content")
                if isinstance(inner, str):
                    out.append(inner)
                elif isinstance(inner, list):
                    out.extend(str(x.get("text") or "") for x in inner if isinstance(x, dict))
        return "\n".join(out)
    return ""


def _kind_from(text, ql):
    rt = (ql or {}).get("rateLimitType")
    if rt in ("five_hour", "seven_day"):
        return rt
    t = (text or "").lower()
    if "weekly limit" in t:
        return "seven_day"
    if "session limit" in t:
        return "five_hour"
    return "model"


def scan_file(path):
    """Хвост одного транскрипта → {'hits': [...], 'last_ok': epoch, 'last_user': epoch}."""
    hits, last_ok, last_user = [], None, None
    for ln in _tail_lines(path):
        if not ln or '"type"' not in ln:
            continue
        # дешёвый предварительный фильтр: строки без признаков нам не нужны целиком
        interesting = ('"apiErrorStatus"' in ln or '"rate_limit"' in ln or
                       "hit your" in ln or "reached your" in ln or '"assistant"' in ln
                       or '"user"' in ln)
        if not interesting:
            continue
        try:
            d = json.loads(ln)
        except Exception:                           # noqa: BLE001
            continue
        typ = d.get("type")
        ts = iso_to_epoch(d.get("timestamp"))
        msg = d.get("message") or {}
        if typ == "assistant":
            is_err = bool(d.get("error") or d.get("isApiErrorMessage") or d.get("apiErrorStatus"))
            if is_err:
                st = d.get("apiErrorStatus")
                text = _content_text(msg)
                if st == 429 or d.get("error") == "rate_limit" or RE_LIMIT_TXT.search(text):
                    ql = d.get("quotaLimits") or {}
                    api_reset = ql.get("resetsAt")
                    # Человеку — время из ТЕКСТА (его же показывает Claude в окне);
                    # машинное resetsAt кладём рядом: у недельного лимита они расходились
                    # на 8 ч (консерва 28.08), правым считаем то, что видит человек.
                    resets, tzn = parse_reset_text(text, ts)
                    if not resets and api_reset:
                        resets = float(api_reset)
                    hits.append({"at": ts, "text": text.strip()[:200], "kind": _kind_from(text, ql),
                                 "resets_at": float(resets) if resets else None,
                                 "resets_at_api": float(api_reset) if api_reset else None,
                                 "tz": tzn, "model": msg.get("model"),
                                 "request_id": d.get("requestId"), "file": path,
                                 "sidechain": bool(d.get("isSidechain"))})
            else:
                if msg.get("content") and ts:
                    last_ok = max(last_ok or 0, ts)
        elif typ == "user":
            text = _content_text(msg)
            if "<task-notification>" in text and "<status>failed" in text:
                m = RE_LIMIT_TXT.search(text)
                if m:
                    resets, tzn = parse_reset_text(text, ts)
                    hits.append({"at": ts, "text": "субагент: " + m.group(0),
                                 "kind": _kind_from(text, None), "resets_at": resets,
                                 "tz": tzn, "model": None, "request_id": None,
                                 "file": path, "sidechain": True})
            elif isinstance(msg.get("content"), str) and not d.get("isSidechain") and ts:
                last_user = max(last_user or 0, ts)
    return {"hits": hits, "last_ok": last_ok, "last_user": last_user, "file": path}


def detect(claude_dir=CLAUDE_DIR, files=None, window_h=WINDOW_H, now=None):
    now = now or time.time()
    files = files or find_transcripts(claude_dir)
    hits, last_ok = [], None
    scanned = []
    for f in files:
        r = scan_file(f)
        scanned.append(f)
        hits.extend(r["hits"])
        if r["last_ok"]:
            last_ok = max(last_ok or 0, r["last_ok"])
    hits = [h for h in hits if h["at"] and now - h["at"] <= window_h * 3600]
    hits.sort(key=lambda h: h["at"])
    out = {"now": now, "files": scanned, "hit": None, "limited": False, "recovered_at": None,
           "last_ok": last_ok, "why": "записей 429 в окне нет"}
    if not hits:
        return out
    h = hits[-1]
    # первая запись инцидента: та же группа сброса (или тот же час для лимита модели)
    key_of = lambda x: "%s:%s" % (x["kind"], int(x["resets_at"]) if x["resets_at"] else int(x["at"] // 3600))  # noqa: E731
    first = next(x for x in hits if key_of(x) == key_of(h))
    out["hit"] = dict(h, first_at=first["at"], count=sum(1 for x in hits if key_of(x) == key_of(h)),
                      incident=key_of(h))
    if last_ok and last_ok > h["at"]:
        out["recovered_at"] = last_ok
        out["why"] = "после 429 был удачный ход"
        return out
    if h["resets_at"] and now >= h["resets_at"]:
        out["why"] = "время сброса прошло, удачного хода ещё не было"
        out["limited"] = False
        out["reset_passed"] = True
        return out
    out["limited"] = True
    out["why"] = "лимит держится"
    return out


# ── «тихо» ────────────────────────────────────────────────────────────────────
def last_inbound(journal):
    """Последнее входящее ЧЕЛОВЕКА в журнале плагина (боты не считаются)."""
    best = 0
    for ln in _tail_lines(journal, 512 * 1024):
        if '"notifications/claude/channel"' not in ln:
            continue
        try:
            e = json.loads(ln)
        except Exception:                           # noqa: BLE001
            continue
        m = ((e.get("params") or {}).get("meta") or {})
        if (m.get("user") or "").endswith("_bot"):
            continue
        ts = e.get("ts")
        if isinstance(ts, (int, float)):
            best = max(best, ts / 1000.0 if ts > 1e11 else ts)
        else:
            t = iso_to_epoch(m.get("ts"))
            if t:
                best = max(best, t)
    return best or None


def silence(journal, last_ok, now, silent_min=SILENT_MIN):
    li = last_inbound(journal) if journal and os.path.exists(journal) else None
    if not li:
        return {"silent": False, "last_inbound": None}
    unanswered = (last_ok or 0) < li
    age = now - li
    return {"silent": bool(unanswered and age >= silent_min * 60), "last_inbound": li,
            "unanswered_min": int(age // 60) if unanswered else 0}


# ── аккаунт ───────────────────────────────────────────────────────────────────
def mask_email(e):
    e = str(e or "")
    if "@" not in e:
        return e[:1] + "***" if e else ""
    u, d = e.split("@", 1)
    if len(u) <= 2:
        return u[:1] + "***@" + d
    return u[0] + "***" + u[-1] + "@" + d


def account_info(cmd=None, timeout=20):
    """`claude auth status --json` (или своя команда) → {'email': маска, 'plan': …}. Пусто — не знаем."""
    argv = cmd if cmd else [_claude_bin(), "auth", "status", "--json"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           shell=isinstance(argv, str))
        d = json.loads(p.stdout or "{}")
    except Exception:                               # noqa: BLE001
        return {}
    if not d.get("loggedIn"):
        return {"logged_in": False}
    return {"logged_in": True, "email": mask_email(d.get("email")),
            "plan": d.get("subscriptionType") or d.get("authMethod") or ""}


def _claude_bin():
    for c in (os.environ.get("CLAUDE_BIN"), "/usr/local/bin/claude", os.path.expanduser("~/.local/bin/claude"),
              "/opt/homebrew/bin/claude", "/Applications/cmux.app/Contents/Resources/bin/claude"):
        if c and os.path.exists(c):
            return c
    return "claude"


# ── сообщения ─────────────────────────────────────────────────────────────────
def _minutes_left(resets_at, now):
    return max(0, int((resets_at - now + 59) // 60))


def message_hit(det, tz_name, mode="admin", switch_link="", account=None, now=None):
    """Текст для человека. Возвращает список кусков [(вид, текст)], вид ∈ text|code."""
    now = now or time.time()
    h = det["hit"]
    tz_name = h.get("tz") or tz_name
    kind_ru = KIND_RU.get(h["kind"], h["kind"])
    who = "Лимит подписки Claude" if mode == "admin" else "Лимит подписки Claude у вашей Милы"
    parts = []
    head = "🔴 %s исчерпан в %s (%s)." % (who, fmt_hm(h["first_at"], tz_name), kind_ru)
    if h["resets_at"]:
        left = _minutes_left(h["resets_at"], now)
        same_day = fmt_date_hm(h["resets_at"], tz_name)[:5] == fmt_date_hm(now, tz_name)[:5]
        when = fmt_hm(h["resets_at"], tz_name) if same_day else fmt_date_hm(h["resets_at"], tz_name)
        if left >= 120:
            left_s = "через %d ч %02d мин" % (left // 60, left % 60)
        else:
            left_s = "через %d мин" % left
        head += "\nВосстановится в %s — %s." % (when, left_s)
    else:
        head += "\nВремени восстановления в ответе Claude нет — как только лимит вернётся, скажу."
    head += "\nСообщения не теряются: отвечу, как только лимит вернётся."
    if account and account.get("email"):
        head += "\nАккаунт: %s%s." % (account["email"], " (%s)" % account["plan"] if account.get("plan") else "")
    parts.append(("text", head))
    if mode == "admin":
        parts.append(("text", "Переключить на другую подписку — командой в окне сессии Claude Code:"))
        parts.append(("code", "/login"))
        parts.append(("text", "Откроется страница входа Claude — войти другим аккаунтом, код вставить "
                              "в то же окно. С телефона без терминала переключить нельзя — это граница "
                              "штатного Claude Code на Маке."))
    else:
        if switch_link:
            # ссылка обычным текстом: Telegram делает её кнопкой-переходом, в моноблоке тап не открыл бы
            parts.append(("text", "Переключить на другую подписку — без терминала, две минуты с телефона:\n" + switch_link))
            parts.append(("text", "Ссылка действует 15 минут и один раз. Или просто подождите — "
                                  "лимит вернётся сам."))
        else:
            parts.append(("text", "Хотите переключить на другую подписку — напишите «сменить подписку», "
                                  "пришлю ссылку."))
    return parts


def message_recovered(det, tz_name, account=None):
    at = det.get("recovered_at") or time.time()
    s = "🟢 Лимит подписки Claude вернулся в %s — работаю, отвечаю на накопившееся по порядку." % fmt_hm(at, tz_name)
    if account and account.get("email"):
        s += "\nАккаунт: %s%s." % (account["email"], " (%s)" % account["plan"] if account.get("plan") else "")
    return [("text", s)]


def message_silent(sil, tz_name):
    return [("text", "🟡 Тихо: последнее входящее в %s без ответа %d мин. Признаков лимита в транскрипте "
                     "нет — возможно, сессия занята длинным ходом или стоит мост канала (`mila doctor`)."
             % (fmt_hm(sil["last_inbound"], tz_name), sil["unanswered_min"]))]


_MD2 = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def render_plain(parts):
    return "\n\n".join(t for _, t in parts)


def render_md2(parts):
    out = []
    for kind, t in parts:
        if kind == "code":
            out.append("```\n%s\n```" % t.replace("\\", "\\\\").replace("`", "\\`"))
        else:
            out.append(_MD2.sub(r"\\\1", t))
    return "\n\n".join(out)


# ── доставка ──────────────────────────────────────────────────────────────────
def send_outbox(outbox, chat, parts):
    os.makedirs(outbox, mode=0o700, exist_ok=True)
    job = {"chat_id": int(chat), "text": render_md2(parts), "parse_mode": "MarkdownV2"}
    name = "limit-%d-%d.json" % (int(time.time() * 1000), os.getpid())
    tmp = os.path.join(outbox, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, os.path.join(outbox, name))
    return name


def send_telegram(token, chat, parts):
    body = urllib.parse.urlencode({"chat_id": chat, "text": render_md2(parts),
                                   "parse_mode": "MarkdownV2"}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=body)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception:                               # noqa: BLE001
        # разметка могла не пройти — второй заход без неё
        body = urllib.parse.urlencode({"chat_id": chat, "text": render_plain(parts)}).encode()
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=body)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status == 200
        except Exception:                           # noqa: BLE001
            return False


# ── состояние ─────────────────────────────────────────────────────────────────
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                               # noqa: BLE001
        return {}


def save_state(path, st):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_pid(pid_file):
    try:
        return int(open(pid_file).read().strip() or 0)
    except Exception:                               # noqa: BLE001
        return 0


def hold(claude_dir=CLAUDE_DIR, pid_file=None, _cache={}):
    """Для кормильца Компаньона: (держать ли входящие, до когда, почему).
    Держим только когда время сброса ИЗВЕСТНО и не прошло; сессия перезапущена
    (другой pid — после смены подписки) → отпускаем сразу. Кэш 5 с, чтобы не
    перечитывать транскрипт на каждое сообщение."""
    now = time.time()
    if _cache.get("at", 0) > now - 5:
        return _cache["v"]
    det = detect(claude_dir, now=now)
    pid = _read_pid(pid_file) if pid_file else 0
    v = (False, None, det["why"])
    if det["limited"] and det["hit"].get("resets_at"):
        hit_pid = _cache.get("hit_pid")
        if hit_pid is None or _cache.get("incident") != det["hit"]["incident"]:
            _cache["hit_pid"], _cache["incident"] = pid, det["hit"]["incident"]
            hit_pid = pid
        if pid and hit_pid and pid != hit_pid:
            v = (False, None, "сессия перезапущена после лимита — отпускаю")
        else:
            until = max(det["hit"]["resets_at"], det["hit"].get("resets_at_api") or 0)
            v = (True, until, "лимит до %s" % fmt_hm(until, det["hit"].get("tz") or DEFAULT_TZ))
    _cache["at"], _cache["v"] = now, v
    return v


# ── главный цикл ──────────────────────────────────────────────────────────────
def run(args):
    now = time.time()
    files = [args.probe] if args.probe else (args.transcripts or None)
    if args.det_json:
        # разбор сделан заранее (Компаньон: под `podman unshare`, где читаются файлы volume)
        with open(args.det_json, encoding="utf-8") as f:
            det = json.load(f)
        det["now"] = now
    else:
        det = detect(args.claude_dir, files=files, window_h=args.window_hours, now=now)
    tz_name = args.tz
    sil = silence(args.journal, det["last_ok"], now, args.silent_min) if (args.journal and not args.probe) else {"silent": False}
    det["silence"] = sil

    if args.probe or args.json and not (args.notify or args.dry_run or args.baseline):
        if args.probe and det["hit"]:
            det["message_preview"] = render_plain(message_hit(det, tz_name, args.mode, args.switch_link, None, now))
        print(json.dumps(det, ensure_ascii=False, indent=1, default=str))
        return 0

    st = load_state(args.state)
    actions = []
    h = det["hit"]
    inc = h["incident"] if h else None

    def deliver(parts, what):
        if args.dry_run:
            print("── %s (dry-run, не отправлено) ──" % what)
            print(render_plain(parts))
            return True
        ok = False
        if args.outbox and args.chat:
            ok = bool(send_outbox(args.outbox, args.chat, parts))
        elif args.tg_token_env and args.chat:
            ok = send_telegram(os.environ.get(args.tg_token_env, ""), args.chat, parts)
        else:
            print(render_plain(parts))
            ok = True
        actions.append("%s: %s" % (what, "ушло" if ok else "🔴 не ушло"))
        return ok

    if args.baseline:
        if h:
            st = {"incident": inc, "hit_at": h["first_at"], "resets_at": h["resets_at"], "kind": h["kind"],
                  "notified_hit": now, "recovered_at": det.get("recovered_at"),
                  "notified_recovered": now if det.get("recovered_at") or not det["limited"] else None,
                  "baseline": now}
        else:
            st = {"baseline": now}
        save_state(args.state, st)
        print(json.dumps({"baseline": True, "state": st}, ensure_ascii=False, indent=1))
        return 0

    if det["limited"]:
        if st.get("incident") != inc:
            acc = account_info(args.account_cmd) if args.account else None
            st = {"incident": inc, "hit_at": h["first_at"], "resets_at": h["resets_at"], "kind": h["kind"],
                  "text": h["text"], "account": acc, "notified_hit": None, "recovered_at": None,
                  "notified_recovered": None, "silent_since": st.get("silent_since")}
        if not st.get("notified_hit"):
            link = args.switch_link
            if not link and args.switch_link_cmd and not args.dry_run:
                try:
                    link = subprocess.run(args.switch_link_cmd, shell=True, capture_output=True,
                                          text=True, timeout=90).stdout.strip().splitlines()[-1]
                except Exception:                   # noqa: BLE001
                    link = ""
                if not link.startswith("http"):
                    link = ""
            if deliver(message_hit(det, tz_name, args.mode, link, st.get("account"), now), "лимит"):
                st["notified_hit"] = now
                st["switch_link_issued"] = bool(link)
    else:
        # инцидент был объявлен, а теперь есть удачный ход → одно «вернулся»
        if st.get("incident") and st.get("notified_hit") and not st.get("notified_recovered"):
            if det.get("recovered_at") and det["recovered_at"] > (st.get("hit_at") or 0):
                acc = account_info(args.account_cmd) if args.account else None
                if deliver(message_recovered(det, tz_name, acc), "восстановление"):
                    st["recovered_at"], st["notified_recovered"] = det["recovered_at"], now
                    st["account_after"] = acc
        # тихо — только вне лимита и один раз на эпизод
        if sil.get("silent") and args.mode == "admin":
            if st.get("silent_since") != sil["last_inbound"]:
                if deliver(message_silent(sil, tz_name), "тихо"):
                    st["silent_since"] = sil["last_inbound"]
        elif not sil.get("silent"):
            st.pop("silent_since", None)

    if not args.dry_run:
        st["checked_at"] = now
        save_state(args.state, st)
    if args.json or args.dry_run:
        print(json.dumps({"limited": det["limited"], "why": det["why"], "hit": h, "recovered_at": det.get("recovered_at"),
                          "silence": sil, "actions": actions, "state": st}, ensure_ascii=False, indent=1, default=str))
    elif actions:
        print("; ".join(actions))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="датчик лимита подписки Claude Code")
    ap.add_argument("--claude-dir", default=CLAUDE_DIR)
    ap.add_argument("--transcripts", nargs="*", help="явные файлы вместо поиска свежих")
    ap.add_argument("--probe", help="консерва: один jsonl, без состояния и отправки")
    ap.add_argument("--state", default=os.path.join(TG_STATE, "limit.json"))
    ap.add_argument("--journal", default=os.path.join(TG_STATE, "inbound", "events.jsonl"))
    ap.add_argument("--tz", default=DEFAULT_TZ)
    ap.add_argument("--mode", choices=("admin", "companion"), default="admin")
    ap.add_argument("--switch-link", default="")
    ap.add_argument("--switch-link-cmd", help="команда, печатающая ссылку смены подписки (зовётся только при отправке)")
    ap.add_argument("--det-json", help="готовый разбор detect() из файла вместо чтения транскриптов")
    ap.add_argument("--window-hours", type=float, default=WINDOW_H)
    ap.add_argument("--silent-min", type=int, default=SILENT_MIN)
    ap.add_argument("--account", action="store_true", help="спросить claude auth status (e-mail маскируется)")
    ap.add_argument("--account-cmd", help="своя команда вместо claude auth status --json (строка shell)")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--outbox", help="каталог outbox sender-демона (Мила Админ)")
    ap.add_argument("--tg-token-env", help="имя переменной с токеном бота (Компаньон)")
    ap.add_argument("--chat", help="chat_id адресата")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.account_cmd:
        args.account = True
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
