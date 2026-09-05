#!/usr/bin/env python3
"""task.py — УЛИКА: обязательства Милы Админ (спека ~/.claude/skills/mila-tasks/SKILL.md).

Три сущности: обязательство (M-…), обещание человеку (P-…), развилка владельца (A-…).
Истина — append-only леджер tasks/ledger.jsonl; OPEN.md генерируется; улики — tasks/proof/.
Без зависимостей, python3 ≥ 3.9. Ничего не запускает сам, в Telegram не пишет.

Команды: new · take · note · done · drop · promise · keep · broken · ask · decide ·
         next · open · scan-sent · report · regen · nowblock
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import shlex
import statistics
import subprocess
import sys
import urllib.request

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Tashkent")
except Exception:  # pragma: no cover
    TZ = dt.timezone(dt.timedelta(hours=5))

# ---------- пути (переопределяются env для тестов) ----------
HOME = os.path.expanduser("~")
BASE = os.environ.get("MILA_TASKS_DIR") or os.path.join(HOME, "milagpt", "tasks")
LEDGER = os.path.join(BASE, "ledger.jsonl")
OPEN_MD = os.path.join(BASE, "OPEN.md")
PROOF_DIR = os.path.join(BASE, "proof")
ESTIMATES = os.path.join(BASE, "estimates.json")
SENT_LOG = os.path.join(BASE, "sent.jsonl")          # исходящие через reply-хук
STATE = os.path.join(BASE, "state.json")              # last_run команд
TG_SENT_DIR = os.environ.get("MILA_TG_SENT_DIR") or os.path.join(HOME, ".claude", "channels", "telegram", "sent")
TG_FAILED_DIR = os.environ.get("MILA_TG_FAILED_DIR") or os.path.join(HOME, ".claude", "channels", "telegram", "failed")
NOW_MD = os.environ.get("MILA_NOW_MD") or os.path.join(HOME, "milagpt", "NOW.md")

ACTIVE_MAX = 3
ASK_MAX = 3
STALL_HOURS = 24
OLD_DAYS = 10
MIN_FACTS = 3   # с какого числа фактов медиана заменяет стартовую оценку

# Стартовые оценки — «оценка до накопления фактов». Пересчитываются медианой по done.
DEFAULT_ESTIMATES = {
    "_note": "оценка до накопления фактов; minutes — от take до done, tokens — --tokens при done",
    "reply":     {"minutes": 5,   "tokens": 8000},
    "doc":       {"minutes": 45,  "tokens": 60000},
    "translate": {"minutes": 30,  "tokens": 40000},
    "build":     {"minutes": 180, "tokens": 250000},
    "research":  {"minutes": 40,  "tokens": 50000},
    "fix":       {"minutes": 30,  "tokens": 40000},
    "design":    {"minutes": 60,  "tokens": 80000},
    "report":    {"minutes": 30,  "tokens": 35000},
    "misc":      {"minutes": 20,  "tokens": 25000},
}

PROOF_FORMS = ("cmd:", "probe:", "url:", "msg:", "git:", "human:")


# ---------- утилиты ----------
def now():
    return dt.datetime.now(TZ)


def iso(d):
    return d.isoformat(timespec="seconds")


def parse_ts(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d.astimezone(TZ)


def fmt_dt(d):
    if not d:
        return "—"
    return d.strftime("%d.%m %H:%M")


def ensure_dirs():
    os.makedirs(PROOF_DIR, exist_ok=True)
    if not os.path.exists(LEDGER):
        open(LEDGER, "a").close()
    if not os.path.exists(ESTIMATES):
        with open(ESTIMATES, "w") as f:
            json.dump(DEFAULT_ESTIMATES, f, ensure_ascii=False, indent=1)


def read_ledger():
    ensure_dirs()
    out = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue  # битую строку не правим и не удаляем — леджер append-only
    return out


def append(ev):
    ensure_dirs()
    ev = dict(ev)
    ev.setdefault("ts", iso(now()))
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def touch_state(key):
    st = load_state()
    st[key] = iso(now())
    with open(STATE, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def load_estimates():
    ensure_dirs()
    try:
        with open(ESTIMATES) as f:
            return json.load(f)
    except Exception:
        return dict(DEFAULT_ESTIMATES)


# ---------- разбор срока ----------
WEEKDAYS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресень": 6}
NUM_WORDS = {"час": 1, "полчаса": 0.5, "пару": 2, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
             "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40}


def _at(base, h, m=0):
    return base.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def parse_due(text, base=None):
    """Срок из человеческой фразы → aware datetime или None. Абсолютный срок важнее относительного."""
    if not text:
        return None
    t = text.lower().replace("ё", "е")
    base = base or now()
    # 1. явная дата и время: 05.09 18:00 / 05.09.2026 18:00
    m = re.search(r"(\d{1,2})\.(\d{2})(?:\.(\d{4}))?\s+(\d{1,2}):(\d{2})", t)
    if m:
        y = int(m.group(3) or base.year)
        try:
            return dt.datetime(y, int(m.group(2)), int(m.group(1)), int(m.group(4)), int(m.group(5)), tzinfo=TZ)
        except ValueError:
            pass
    # 2. ISO
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[t ](\d{2}):(\d{2}))?", t)
    if m:
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                               int(m.group(4) or 18), int(m.group(5) or 0), tzinfo=TZ)
        except ValueError:
            pass
    # 2б. дата без времени: 05.09 / до 07.09
    m = re.search(r"(?<!\d)(\d{1,2})\.(\d{2})(?!\.\d|\d)", t)
    if m:
        try:
            return dt.datetime(base.year, int(m.group(2)), int(m.group(1)), 18, 0, tzinfo=TZ)
        except ValueError:
            pass
    # 3. через N часов/минут/дней
    m = re.search(r"через\s+(\d+|полчаса|час|пару|два|две|три|четыре|пять|десять|пятнадцать|двадцать|тридцать|сорок)\s*(час|ч\b|мин|дн|день|сут)?", t)
    if m:
        q = m.group(1)
        n = float(q) if q.isdigit() else NUM_WORDS.get(q, 1)
        unit = m.group(2) or ("час" if q in ("час", "полчаса") else "час")
        if q == "полчаса":
            return base + dt.timedelta(minutes=30)
        if unit.startswith("мин"):
            return base + dt.timedelta(minutes=n)
        if unit.startswith(("дн", "день", "сут")):
            return _at(base + dt.timedelta(days=n), 18)
        return base + dt.timedelta(hours=n)
    # 4. в течение …
    m = re.search(r"в течение\s+(получаса|часа|дня|(\d+)\s*(час|мин|дн))", t)
    if m:
        if m.group(1) == "получаса":
            return base + dt.timedelta(minutes=30)
        if m.group(1) == "часа":
            return base + dt.timedelta(hours=1)
        if m.group(1) == "дня":
            return _at(base, 20)
        n = int(m.group(2))
        u = m.group(3)
        if u == "мин":
            return base + dt.timedelta(minutes=n)
        if u == "дн":
            return _at(base + dt.timedelta(days=n), 18)
        return base + dt.timedelta(hours=n)
    # день недели — с предлогом или без («понедельник 12:00» давало СЕГОДНЯ 12:00: ветка
    # голого HH:MM срабатывала раньше; 05.09 M-0004 получила срок в прошлом)
    m = re.search(r"(?:(?<![а-яa-z])(?:до|к|в|во)\s+)?(понедельник|вторник|сред|четверг|пятниц|суббот|воскресень)\w*(?:\s+(?:к|до|в)?\s*(\d{1,2})(?::(\d{2}))?)?", t)
    if m:
        wd = WEEKDAYS[m.group(1)]
        delta = (wd - base.weekday()) % 7 or 7
        hh = int(m.group(2)) if m.group(2) and int(m.group(2)) <= 23 else 18
        return _at(base + dt.timedelta(days=delta), hh, m.group(3) or 0)
    # 5. день + время: (сегодня|завтра)? (к|до|в) HH(:MM)?
    day = base
    if "послезавтра" in t:
        day = base + dt.timedelta(days=2)
    elif "завтра" in t:
        day = base + dt.timedelta(days=1)
    m = re.search(r"(?<![а-яa-z])(?:к|до|в)\s+(\d{1,2})(?::(\d{2}))?(?!\d|\.\d|:\d)", t)
    if m and int(m.group(1)) <= 23:
        d = _at(day, m.group(1), m.group(2) or 0)
        if d < base and day.date() == base.date():
            d += dt.timedelta(days=1)
        return d
    # 6. части суток
    if re.search(r"к вечеру|вечером|до вечера", t):
        d = _at(day, 19)
        return d if d > base else base + dt.timedelta(hours=2)
    if re.search(r"к обеду|до обеда", t):
        d = _at(day, 13)
        return d if d > base else _at(day + dt.timedelta(days=1), 13)
    if re.search(r"к утру|утром|с утра|до утра", t):
        d = _at(day, 10)
        return d if d > base else _at(day + dt.timedelta(days=1), 10)
    if re.search(r"до конца дня|к концу дня", t):
        return _at(day, 20)
    if re.search(r"к ночи", t):
        return _at(day, 23)
    # 7. до пятницы / к понедельнику
    # 8. голое HH:MM (с учётом «сегодня/завтра» как базы дня)
    m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", t)
    if m and int(m.group(1)) <= 23:
        d = _at(day, m.group(1), m.group(2))
        return d if d > base else d + dt.timedelta(days=1)
    # 9. просто сегодня/завтра
    if "послезавтра" in t or "завтра" in t:
        return _at(day, 12)
    if "сегодня" in t:
        d = _at(base, 20)
        return d if d > base else base + dt.timedelta(hours=2)
    return None


# Признаки обещания в исходящем. Ошибка в сторону лишней карточки дешевле пропуска (§6).
PROMISE_STRONG_RE = re.compile(
    r"(пришл[юу]|сдела[юе]|отправл[юу]|подготовл[юу]|собер[уе]|верн[уе]сь|напиш[уе]|долож[уе]|"
    r"отдам|скину|покаж[уе]|посчита[юе]|провер[юе]|разбер[уе]сь|займ[уе]сь|беру\b|возьм[уе]|обеща[юе]|"
    r"готово будет|будет готов|в течение|через (?:\d+|час|полчаса|пару|два|три|пять|десять|минут))",
    re.IGNORECASE)
# Слабые признаки — только срок без глагола. Сами по себе обещанием НЕ считаются:
# «сегодня» в отчёте о сделанном и «срок 12:00» в пересказе чужого срока дали
# ложные P-0003/P-0004 05.09. Учитываются только рядом с сильным признаком.
PROMISE_WEAK_RE = re.compile(
    r"(к вечеру|к утру|к обеду|сегодня|завтра|послезавтра|до \d|"
    r"до (?:понедельника|вторника|среды|четверга|пятницы|субботы|воскресенья|вечера|утра|обеда|конца дня))",
    re.IGNORECASE)
PROMISE_RE = re.compile(PROMISE_STRONG_RE.pattern[:-1] + "|" + PROMISE_WEAK_RE.pattern[1:], re.IGNORECASE)
# NARRATIVE-0509: пересказ случившегося, вопрос-предложение и отчёт о сделанном — не обещание.
# Ложные P-0031 (05.09: «…через 3 минуты потребовал…»), у директоров тот же класс (TASKS-FILTER-FIX-0509).
NOT_PROMISE_RE = re.compile(
    r"(?<![а-я])(?:хотите|хочешь|могу|можно|предлага[юе]|давайте|ответьте|напишите|если\s+(?:нужно|хотите|скажете)|"
    r"случилось|записал[аои]?|потребовал[аои]?|сделал[аои]?|отправил[аои]?|проверил[аои]?|вступил[аои]?|"
    r"перезапущен[аыо]?|снят[аыо]?|готово(?!\s+будет)|исправлен[аыо]?|закрыт[аыо]?|выполнен[аыо]?|было|была|были)(?![а-я])",
    re.IGNORECASE)


def _is_narrative(sent):
    s = sent.strip()
    return bool(NOT_PROMISE_RE.search(s)) or s.endswith("?")


def _sentences(text):
    return [x.strip() for x in re.split(r"(?<=[.!?\n])\s+", (text or "").replace("ё", "е")) if x.strip()]


def looks_like_promise(text):
    """Признак обещания — СИЛЬНЫЙ маркер (глагол обязательства / «в течение» / «через N»)
    в каком-либо предложении. Слабые («сегодня», «до 18:00») без сильного — нет."""
    for sent in _sentences(text):
        if _is_narrative(sent):
            continue
        m = PROMISE_STRONG_RE.search(sent)
        if m:
            return m.group(0)
    return None


def promise_gist_and_due(text, base=None):
    """Предложение с сильным маркером и срок ИЗ ЭТОГО предложения (не из всего текста:
    05.09 «01.09» из цитаты решения стало сроком в прошлом). Срок в прошлом → None
    + пометка. Возвращает (hit, gist, due, note)."""
    base = base or now()
    for sent in _sentences(text):
        if _is_narrative(sent):
            continue
        m = PROMISE_STRONG_RE.search(sent)
        if not m:
            continue
        due = parse_due(sent, base)
        note = None
        if due and due < base - dt.timedelta(minutes=5):
            note = "срок в прошлом (%s) — из цитаты? уточнить" % fmt_dt(due)
            due = None
        return m.group(0), sent[:120], due, note
    return None, None, None, None


# ---------- свёртка леджера в состояние ----------
def build_state(events):
    tasks = {}
    counters = {"M": 0, "P": 0, "A": 0}
    for ev in events:
        e = ev.get("ev")
        tid = ev.get("id")
        if tid and re.match(r"^[MPA]-\d+$", tid):
            p, n = tid.split("-")
            counters[p] = max(counters[p], int(n))
        if e in ("new", "promise", "ask"):
            t = dict(ev)
            t["type"] = {"new": "task", "promise": "promise", "ask": "ask"}[e]
            t["status"] = "open"
            t["created"] = ev["ts"]
            t["touched"] = ev["ts"]
            t["notes"] = []
            t["events"] = [ev]
            t.setdefault("kind", "reply" if e == "promise" else "misc")
            tasks[tid] = t
            continue
        t = tasks.get(tid)
        if not t:
            continue
        t["events"].append(ev)
        t["touched"] = ev["ts"]
        if e == "take":
            t["status"] = "active"
            t["taken"] = ev["ts"]
            t["stalled"] = False
        elif e == "note":
            t["notes"].append(ev)
        elif e == "stalled":
            t["status"] = "open"
            t["stalled"] = True
        elif e == "done":
            t["status"] = "done"
            t["done"] = ev
        elif e == "drop":
            t["status"] = "dropped"
            t["drop"] = ev
        elif e == "keep":
            t["status"] = "kept"
            t["keep"] = ev
        elif e == "broken":
            t["status"] = "broken"
            t["broken"] = ev
        elif e == "decide":
            t["status"] = "decided"
            t["decide"] = ev
        elif e == "delivered":
            t["delivered"] = ev
    return tasks, counters


def next_id(counters, prefix):
    counters[prefix] += 1
    return "%s-%04d" % (prefix, counters[prefix])


def find(tasks, tid):
    t = tasks.get(tid)
    if not t:
        die("нет такого id: %s" % tid)
    return t


def die(msg, code=2):
    print("🔴 " + msg)
    raise SystemExit(code)


# ---------- оценки ----------
def facts_by_kind(tasks):
    facts = {}
    for t in tasks.values():
        if t["status"] != "done":
            continue
        d = t["done"]
        k = t.get("kind", "misc")
        facts.setdefault(k, {"minutes": [], "tokens": []})
        if d.get("minutes") is not None:
            facts[k]["minutes"].append(d["minutes"])
        if d.get("tokens") is not None:
            facts[k]["tokens"].append(d["tokens"])
    return facts


def estimate(kind, tasks, est=None):
    """(minutes, tokens, source) — медиана по фактам, иначе стартовая оценка."""
    est = est or load_estimates()
    facts = facts_by_kind(tasks).get(kind, {"minutes": [], "tokens": []})
    base = est.get(kind) or est.get("misc") or DEFAULT_ESTIMATES["misc"]
    # медиана включается с трёх фактов: один факт «0 мин» подменил бы стартовую оценку
    enough_m = len(facts["minutes"]) >= MIN_FACTS
    enough_t = len(facts["tokens"]) >= MIN_FACTS
    mins = statistics.median(facts["minutes"]) if enough_m else base["minutes"]
    toks = statistics.median(facts["tokens"]) if enough_t else base["tokens"]
    src = "медиана %d факт." % len(facts["minutes"]) if enough_m else "оценка, фактов %d/%d" % (len(facts["minutes"]), MIN_FACTS)
    return int(round(mins)), int(round(toks)), src


def fmt_est(t, tasks, est=None):
    m, k, src = estimate(t.get("kind", "misc"), tasks, est)
    return "≈%d мин · %dk ток (%s)" % (m, k / 1000, src)


# ---------- порядок «следующая» (§4) ----------
def bucket(t, at):
    """Корзина 1..4; внутри корзины — сортировка по возрасту (старшая первая), «залежалось» наверх."""
    if t["type"] == "promise":
        due = parse_ts(t.get("due"))
        if due is None:
            return 1, 0  # срок не уточнён — тоже горит: часы чужие, а мы их не знаем
        if due < at + dt.timedelta(minutes=60):
            return 1, 0
        return 4, 0
    if t["type"] == "ask":
        return 9, 0
    due = parse_ts(t.get("due"))
    if t.get("bleed") or t.get("paid") or (due and due <= at.replace(hour=23, minute=59)):
        return 2, 0
    if t.get("src") == "owner" and parse_ts(t["created"]).date() == at.date():
        return 3, 0
    touched = parse_ts(t["touched"])
    old = 0 if (at - touched).days >= OLD_DAYS else 1
    return 4, old


def reason(t, b, at):
    if b == 1:
        due = parse_ts(t.get("due"))
        if due is None:
            return "обещание без срока — уточнить срок и держать"
        return "обещание %s — %s" % ("СОРВАНО" if due < at else "горит", fmt_dt(due))
    if b == 2:
        if t.get("bleed"):
            return "кровь: %s" % t["bleed"]
        if t.get("paid"):
            return "взяты деньги"
        return "срок %s" % fmt_dt(parse_ts(t.get("due")))
    if b == 3:
        return "владелец сказал сегодня"
    if (at - parse_ts(t["touched"])).days >= OLD_DAYS:
        return "залежалось %d дн без касания" % (at - parse_ts(t["touched"])).days
    return "очередь по возрасту"


def queue(tasks, at=None):
    at = at or now()
    items = [t for t in tasks.values() if t["status"] == "open" and t["type"] != "ask"]
    items.sort(key=lambda t: (bucket(t, at), parse_ts(t["created"])))
    return items


# ---------- улики ----------
def run_cmd(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return None, "timeout %ss" % timeout
    except Exception as ex:
        return None, "exec error: %s" % ex


def _sent_records():
    """Все известные исходящие: sent.jsonl (reply-хук) + результаты outbox плагина."""
    recs = []
    if os.path.exists(SENT_LOG):
        with open(SENT_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                for mid in r.get("ids") or []:
                    recs.append({"chat": str(r.get("chat_id")), "id": str(mid), "text": r.get("text") or "",
                                 "ts": r.get("ts"), "src": "reply"})
    if os.path.isdir(TG_SENT_DIR):
        for fn in os.listdir(TG_SENT_DIR):
            if not fn.endswith(".result.json"):
                continue
            try:
                with open(os.path.join(TG_SENT_DIR, fn), encoding="utf-8") as f:
                    r = json.load(f).get("result") or {}
                recs.append({"chat": str((r.get("chat") or {}).get("id")), "id": str(r.get("message_id")),
                             "text": r.get("text") or r.get("caption") or "",
                             "ts": dt.datetime.fromtimestamp(r.get("date", 0), TZ).isoformat(timespec="seconds") if r.get("date") else None,
                             "src": "outbox"})
            except Exception:
                continue
    return recs


def _in_failed(text):
    if not text or not os.path.isdir(TG_FAILED_DIR):
        return False
    for fn in os.listdir(TG_FAILED_DIR):
        try:
            with open(os.path.join(TG_FAILED_DIR, fn), encoding="utf-8") as f:
                if text.strip() and text.strip() in f.read():
                    return True
        except Exception:
            continue
    return False


def check_msg(ref, chat_expected=None):
    """msg:<chat>/<id> → (result, detail). result ∈ ok|fail|unknown."""
    m = re.match(r"^(-?\d+)/(\d+)$", ref.strip())
    if not m:
        return "fail", "формат msg:<chat>/<message_id>"
    chat, mid = m.group(1), m.group(2)
    if chat_expected and str(chat_expected) != chat:
        return "fail", "адресат карточки %s, а сообщение ушло в %s" % (chat_expected, chat)
    for r in _sent_records():
        if r["chat"] == chat and r["id"] == mid:
            if _in_failed(r["text"]):
                return "fail", "тот же текст лежит в failed/ — доставка не подтверждена"
            return "ok", "доставлено %s (%s): %s" % (r.get("ts") or "?", r["src"], (r["text"] or "")[:80].replace("\n", " "))
    return "unknown", "в журнале исходящих такого сообщения нет (sent.jsonl + %s)" % TG_SENT_DIR


def execute_proof(t, proof, timeout=60):
    """Исполняет улику. Возвращает (result ok|fail|unknown, detail, output)."""
    form = next((p for p in PROOF_FORMS if proof.startswith(p)), None)
    if not form:
        return "fail", "улика должна начинаться с одного из: %s" % ", ".join(PROOF_FORMS), ""
    body = proof[len(form):].strip()
    if form in ("cmd:", "probe:"):
        rc, out = run_cmd(body, timeout)
        if rc is None:
            return "unknown", out, out
        return ("ok" if rc == 0 else "fail"), "exit %s" % rc, out
    if form == "url:":
        url, _, finger = body.partition("|")
        finger = finger.strip() or (t.get("check") or "")
        try:
            req = urllib.request.Request(url.strip(), headers={"User-Agent": "Mozilla/5.0 mila-task-proof"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                code = resp.getcode()
                bodytxt = resp.read(2_000_000).decode("utf-8", "replace")
        except Exception as ex:
            return "unknown", "запрос не прошёл: %s" % ex, ""
        if code != 200:
            return "fail", "код %s" % code, bodytxt[:500]
        if finger and finger not in bodytxt:
            return "fail", "код 200, но отпечатка «%s» в теле нет (Pages отдаёт 200 на любой путь)" % finger[:60], bodytxt[:500]
        return "ok", "код 200, отпечаток найден", bodytxt[:300]
    if form == "msg:":
        res, det = check_msg(body, t.get("chat_id"))
        return res, det, ""
    if form == "git:":
        sha, _, repo = body.partition("@")
        repo = repo.strip() or os.getcwd()
        rc, out = run_cmd("git -C %s branch -r --contains %s" % (shlex.quote(repo), shlex.quote(sha.strip())), 30)
        if rc is None or rc != 0:
            return "unknown", "git не ответил: %s" % out[:200], out
        if not out.strip():
            return "fail", "sha %s не в origin — git ≠ выкачено" % sha[:10], out
        return "ok", "sha в origin: %s (проверка на бою — отдельной уликой)" % out.strip().splitlines()[0], out
    if form == "human:":
        if len(body) < 8:
            return "fail", "human: требует цитату ответа человека и его message_id", ""
        return "ok", "принято со слов человека: %s" % body[:100], ""
    return "fail", "неизвестная форма", ""


def write_proof(tid, proof, result, detail, output, verify=None, verify_res=None):
    ts = now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(PROOF_DIR, "%s-%s.txt" % (tid, ts))
    lines = ["id: %s" % tid, "time: %s" % iso(now()), "proof: %s" % proof, "result: %s" % result, "detail: %s" % detail]
    if verify:
        lines += ["verify: %s" % verify, "verify_result: %s" % verify_res]
    lines += ["--- output (first 40 lines) ---"] + (output or "").splitlines()[:40]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def overlaps_check(proof, check):
    """Улика обязана пересекаться с текстом check: хотя бы одно значимое слово общее."""
    words = lambda s: {w for w in re.findall(r"[a-zа-я0-9_]{4,}", (s or "").lower())}
    a, b = words(proof), words(check)
    return bool(a & b)


# ---------- команды ----------
def cmd_new(a):
    events = read_ledger()
    tasks, counters = build_state(events)
    if not a.check:
        die("new без --check отказывает: критерий закрытия пишется при заведении, не при закрытии")
    if a.src == "self" and not a.bleed:
        print("🟡 src=self без --bleed: самонайденное не обгонит поручение владельца")
    tid = next_id(counters, "M")
    ev = {"ev": "new", "id": tid, "kind": a.kind, "src": a.src, "who": a.who, "chat_id": a.chat,
          "anchor": a.anchor, "title": a.title, "check": a.check, "verify": a.verify,
          "due": iso(parse_due(a.due)) if a.due else None, "bleed": a.bleed, "paid": bool(a.paid),
          "promised_due": iso(parse_due(a.promised_due)) if a.promised_due else None,
          "promised_msg": a.promised_msg}
    if a.due and not ev["due"]:
        print("🟡 срок «%s» не разобран — due=null, уточнить срок" % a.due)
    append(ev)
    tasks, _ = build_state(read_ledger())
    print("взяла: %s %s · %s" % (tid, a.title, fmt_est(tasks[tid], tasks)))
    regen(tasks)
    return 0


def cmd_take(a):
    tasks, _ = build_state(read_ledger())
    t = find(tasks, a.id)
    if t["type"] != "task":
        die("take только для обязательств (M-…); обещания и развилки не берутся в работу")
    if t["status"] == "active":
        print("уже в работе"); return 0
    if t["status"] not in ("open",):
        die("статус %s — взять нельзя" % t["status"])
    active = [x for x in tasks.values() if x["status"] == "active"]
    if len(active) >= ACTIVE_MAX:
        die("в работе уже %d (потолок %d): %s" % (len(active), ACTIVE_MAX, ", ".join(x["id"] for x in active)))
    if t.get("kind") == "build":
        if any(x.get("kind") == "build" for x in active):
            die("стройка уже в работе — ровно одна стройка одновременно")
        today = now().date()
        for x in tasks.values():
            if x.get("kind") == "build" and x.get("taken") and parse_ts(x["taken"]).date() == today and x["id"] != t["id"]:
                die("слот стройки на сегодня взят (%s); один слот в сутки" % x["id"])
    append({"ev": "take", "id": t["id"]})
    tasks, _ = build_state(read_ledger())
    print("в работе: %s %s · прогноз %s" % (t["id"], t["title"], fmt_est(t, tasks)))
    regen(tasks)
    return 0


def cmd_note(a):
    tasks, _ = build_state(read_ledger())
    t = find(tasks, a.id)
    append({"ev": "note", "id": t["id"], "text": a.text})
    print("записано: %s — %s" % (t["id"], a.text[:80]))
    return 0


def cmd_done(a):
    tasks, _ = build_state(read_ledger())
    t = find(tasks, a.id)
    if t["type"] != "task":
        die("done только для обязательств; обещание закрывается keep, развилка — decide")
    if t["status"] in ("done", "dropped"):
        die("уже %s" % t["status"])
    if not a.proof:
        die("done без --proof не пишет ничего (код 2). Формы: %s" % " ".join(PROOF_FORMS))
    proof = a.proof.strip()
    form = next((p for p in PROOF_FORMS if proof.startswith(p)), None)
    if not form:
        die("улика должна начинаться с одной из форм: %s" % ", ".join(PROOF_FORMS))
    if t.get("src") == "client" and form not in ("human:", "msg:") and not a.force:
        die("клиентская задача закрывается только чужой рукой — human: или msg: (или --force, и это останется в строке)")
    if not overlaps_check(proof, t.get("check")) and form not in ("msg:", "human:") and not a.force:
        print("🟡 улика не пересекается с текстом check «%s» — проверь, что она доказывает обещанное" % (t.get("check") or "")[:60])
    # verify из карточки — исполняется первым, если это команда
    verify, vres = t.get("verify"), None
    if verify and not a.force:
        rc, out = run_cmd(verify, a.timeout)
        vres = "unknown" if rc is None else ("ok" if rc == 0 else "fail(exit %s)" % rc)
    res, detail, output = execute_proof(t, proof, a.timeout)
    path = write_proof(t["id"], proof, res, detail, output, verify, vres)
    if res != "ok" and not a.force:
        append({"ev": "note", "id": t["id"], "text": "done отклонён: улика %s — %s" % (res, detail), "proof_file": path})
        print("🔴 не закрыто: улика %s — %s\n   proof: %s" % (res.upper(), detail, path))
        return 3
    if vres and vres.startswith("fail") and not a.force:
        append({"ev": "note", "id": t["id"], "text": "done отклонён: verify %s" % vres, "proof_file": path})
        print("🔴 не закрыто: verify из карточки упал (%s)\n   proof: %s" % (vres, path))
        return 3
    start = parse_ts(t.get("taken") or t["created"])
    minutes = int((now() - start).total_seconds() // 60)
    ev = {"ev": "done", "id": t["id"], "proof": proof, "proof_result": res, "proof_file": path,
          "minutes": minutes, "tokens": a.tokens, "force": bool(a.force),
          "timed_from": "take" if t.get("taken") else "new"}
    append(ev)
    tasks, _ = build_state(read_ledger())
    est = load_estimates()
    m, k, src = estimate(t.get("kind", "misc"), tasks, est)
    print("🟢 закрыто: %s %s · улика %s%s\n   факт %d мин%s · теперь %s: ≈%d мин / %dk (%s)" % (
        t["id"], t["title"], res, " · БЕЗ МАШИННОЙ ПРОВЕРКИ (--force)" if a.force else "",
        minutes, (" · %d ток" % a.tokens) if a.tokens else "", t.get("kind", "misc"), m, k / 1000, src))
    regen(tasks)
    return 0


def cmd_drop(a):
    tasks, _ = build_state(read_ledger())
    t = find(tasks, a.id)
    if not a.why:
        die("drop требует --why")
    # Кандидат от сканера исходящих (auto=True) — не поручение человека, а догадка
    # машины; разметка «не обещание» = drop с причиной, без --force (спека §6:
    # ложное срабатывание допустимо, но не должно висеть красным).
    if t.get("src") in ("owner", "client") and not a.force and not t.get("auto"):
        die("задачу владельца или клиента дропнуть нельзя — заведи развилку: task.py ask --task %s …" % t["id"])
    append({"ev": "drop", "id": t["id"], "why": a.why, "auto_candidate": bool(t.get("auto"))})
    print("снято: %s — %s" % (t["id"], a.why))
    regen(build_state(read_ledger())[0])
    return 0


def cmd_promise(a):
    events = read_ledger()
    tasks, counters = build_state(events)
    # идемпотентность: тот же ключ (chat/message_id) или та же открытая суть — без дубля
    if a.key:
        for t in tasks.values():
            if t["type"] == "promise" and t.get("key") == a.key:
                print("есть: %s (ключ %s)" % (t["id"], a.key)); return 0
    for t in tasks.values():
        if (t["type"] == "promise" and t["status"] == "open" and str(t.get("chat_id")) == str(a.to)
                and (t.get("what") or "").strip().lower() == (a.what or "").strip().lower()):
            print("есть: %s (то же обещание открыто)" % t["id"]); return 0
    due = parse_due(a.due) if a.due else None
    pid = next_id(counters, "P")
    ev = {"ev": "promise", "id": pid, "chat_id": str(a.to), "who": a.who, "what": a.what, "title": a.what,
          "due": iso(due) if due else None, "due_raw": a.due, "kind": a.kind, "key": a.key,
          "src": a.src, "promised_msg": a.msg, "auto": bool(a.auto)}
    append(ev)
    flag = "" if due else " · 🟡 уточнить срок"
    print("обещание %s → %s (%s): %s · до %s%s" % (pid, a.who or a.to, a.to, a.what, fmt_dt(due), flag))
    regen(build_state(read_ledger())[0])
    return 0


def cmd_keep(a):
    tasks, _ = build_state(read_ledger())
    t = find(tasks, a.id)
    if t["type"] != "promise":
        die("keep только для обещаний (P-…)")
    if t["status"] != "open":
        die("статус %s" % t["status"])
    if not a.msg:
        die("keep требует --msg <chat>/<message_id> — доставленное сообщение")
    res, det = check_msg(a.msg, t.get("chat_id"))
    if res != "ok" and not a.force:
        print("🔴 не закрыто: %s — %s" % (res.upper(), det)); return 3
    due = parse_ts(t.get("due"))
    late = bool(due and now() > due)
    append({"ev": "keep", "id": t["id"], "msg": a.msg, "late": late, "force": bool(a.force), "detail": det})
    print("🟢 сдержано%s: %s %s · %s" % (" (с опозданием)" if late else "", t["id"], t.get("what"), det))
    regen(build_state(read_ledger())[0])
    return 0


def cmd_broken(a):
    tasks, _ = build_state(read_ledger())
    t = find(tasks, a.id)
    if t["type"] != "promise":
        die("broken только для обещаний")
    append({"ev": "broken", "id": t["id"], "why": a.why})
    print("🔴 сорвано и записано навсегда: %s %s — %s. Сказать человеку раньше, чем он заметит." % (t["id"], t.get("what"), a.why or "без причины"))
    regen(build_state(read_ledger())[0])
    return 0


def cmd_ask(a):
    events = read_ledger()
    tasks, counters = build_state(events)
    missing = [k for k in ("a", "b", "default", "undo", "deadline") if not getattr(a, k)]
    if missing:
        die("ask без %s не работает: вопрос без дефолта — домашняя работа, а не вопрос" % ", ".join("--" + m for m in missing))
    if a.default not in ("a", "b"):
        die("--default a|b")
    open_asks = [t for t in tasks.values() if t["type"] == "ask" and t["status"] == "open"]
    if len(open_asks) >= ASK_MAX:
        append({"ev": "ask-refused", "id": None, "title": a.title, "open": [t["id"] for t in open_asks]})
        die("открытых развилок уже %d — сначала закрой одну (%s). Отказ записан в леджер." % (len(open_asks), ", ".join(t["id"] for t in open_asks)))
    deadline = parse_due(a.deadline)
    if not deadline:
        die("срок «%s» не разобран" % a.deadline)
    aid = next_id(counters, "A")
    append({"ev": "ask", "id": aid, "title": a.title, "a": a.a, "b": a.b, "default": a.default, "undo": a.undo,
            "deadline": iso(deadline), "need": a.need, "task": a.task, "cost": a.cost, "src": "owner"})
    print("развилка %s: %s\n   А: %s\n   Б: %s\n   дефолт %s · срок %s · откат: %s\n   часы пойдут после delivered (task.py note %s delivered:<chat>/<id>)" % (
        aid, a.title, a.a, a.b, a.default.upper(), fmt_dt(deadline), a.undo, aid))
    regen(build_state(read_ledger())[0])
    return 0


def cmd_decide(a):
    tasks, _ = build_state(read_ledger())
    t = find(tasks, a.id)
    if t["type"] != "ask":
        die("decide только для развилок")
    if t["status"] != "open":
        die("статус %s" % t["status"])
    append({"ev": "decide", "id": t["id"], "choice": a.choice, "by": a.by, "text": a.text})
    print("решено %s: %s → %s (%s)" % (t["id"], t["title"], a.choice.upper(), a.by))
    regen(build_state(read_ledger())[0])
    return 0


def autodecide(tasks):
    """Развилки с истёкшим сроком и подтверждённой доставкой — исполняется дефолт, честной формулировкой."""
    out = []
    at = now()
    for t in tasks.values():
        if t["type"] != "ask" or t["status"] != "open":
            continue
        dl = parse_ts(t.get("deadline"))
        if not dl or at < dl:
            continue
        deliv = t.get("delivered")
        if not deliv:
            out.append("⏳ %s ждёт доставки — таймер стоит, строка в «твои руки»: %s" % (t["id"], t["title"]))
            continue
        shown = fmt_dt(parse_ts(deliv["ts"]))
        hours = int((at - parse_ts(deliv["ts"])).total_seconds() // 3600)
        text = "принято Милой по умолчанию (%s), показано %s %s, ответа не было %d ч, отменяемо: %s" % (
            t["default"].upper(), shown, deliv.get("msg", ""), hours, t.get("undo"))
        append({"ev": "decide", "id": t["id"], "choice": t["default"], "by": "default", "text": text})
        out.append("🧭 %s: %s → %s" % (t["id"], t["title"], text))
    return out


def mark_stalled(tasks):
    out = []
    at = now()
    for t in tasks.values():
        if t["status"] != "active":
            continue
        last = parse_ts(t["notes"][-1]["ts"]) if t["notes"] else parse_ts(t.get("taken"))
        if last and (at - last) > dt.timedelta(hours=STALL_HOURS):
            append({"ev": "stalled", "id": t["id"]})
            out.append("🟡 stalled: %s %s — в работе без note %d ч, вернула в очередь" % (t["id"], t["title"], int((at - last).total_seconds() // 3600)))
    return out


def cmd_next(a):
    tasks, _ = build_state(read_ledger())
    at = now()
    est = load_estimates()
    active = [t for t in tasks.values() if t["status"] == "active"]
    q = queue(tasks, at)
    if active:
        print("в работе: " + " · ".join("%s %s" % (t["id"], t["title"][:40]) for t in active))
    if not q:
        print("очередь пуста"); return 0
    for i, t in enumerate(q[: a.n]):
        b, _ = bucket(t, at)
        head = "СЛЕДУЮЩАЯ" if i == 0 else "%d." % (i + 1)
        print("%s %s %s — %s · %s" % (head, t["id"], t["title"], reason(t, b, at), fmt_est(t, tasks, est)))
    return 0


def _promise_lines(tasks, at):
    burning, broken = [], []
    for t in tasks.values():
        if t["type"] != "promise":
            continue
        if t["status"] == "broken" and (at - parse_ts(t["broken"]["ts"])).days < 7:
            broken.append("🔴 сорвано (записано): %s %s → %s" % (t["id"], t.get("what"), t.get("who") or t.get("chat_id")))
        if t["status"] != "open":
            continue
        due = parse_ts(t.get("due"))
        who = t.get("who") or t.get("chat_id")
        if due is None:
            burning.append("🟡 без срока: %s «%s» → %s · уточнить срок" % (t["id"], t.get("what"), who))
        elif due < at:
            broken.append("🔴 СОРВАНО %s: %s «%s» → %s (chat %s) — сказать человеку первой" % (fmt_dt(due), t["id"], t.get("what"), who, t.get("chat_id")))
        elif due < at + dt.timedelta(minutes=60):
            burning.append("🔥 горит %s: %s «%s» → %s" % (fmt_dt(due), t["id"], t.get("what"), who))
    return broken, burning


def recheck_random(tasks, n=3):
    done = [t for t in tasks.values() if t["status"] == "done" and t["done"].get("proof", "").startswith(("cmd:", "probe:", "url:"))
            and not t["done"].get("force")]
    random.shuffle(done)
    out = []
    for t in done[:n]:
        res, det, _ = execute_proof(t, t["done"]["proof"], timeout=20)
        mark = {"ok": "🟢", "fail": "🔴", "unknown": "⚪"}[res]
        out.append("%s перепроверка %s: %s — %s" % (mark, t["id"], res, det[:80]))
    return out


def cmd_open(a):
    tasks, _ = build_state(read_ledger())
    at = now()
    est = load_estimates()
    st = load_state()
    lines = []
    # 1. красная строка — система жалуется на себя
    day_ago = at - dt.timedelta(hours=24)
    recent = [e for e in read_ledger() if e.get("ev") in ("done", "note", "keep") and (parse_ts(e["ts"]) or at) > day_ago]
    total = len(tasks)
    if total and not recent:
        lines.append("🔴 за сутки в леджере ни одного done/note/keep — день не закрыт")
    ls = parse_ts(st.get("scan-sent"))
    if ls is None:
        lines.append("🔴 scan-sent не отрабатывал ни разу")
    elif (at - ls).days >= 1:
        lines.append("🔴 scan-sent не отрабатывал %d дн" % (at - ls).days)
    # 2. обещания
    broken, burning = _promise_lines(tasks, at)
    lines += broken + burning
    # 3. решено по умолчанию + 4. stalled
    lines += autodecide(tasks)
    lines += mark_stalled(tasks)
    tasks, _ = build_state(read_ledger())
    # 5. next
    active = [t for t in tasks.values() if t["status"] == "active"]
    if active:
        lines.append("в работе: " + " · ".join("%s %s" % (t["id"], t["title"][:40]) for t in active))
    q = queue(tasks, at)
    for i, t in enumerate(q[: (3 if a.short else 5)]):
        b, _ = bucket(t, at)
        lines.append("%s %s %s — %s · %s" % ("→" if i == 0 else " ", t["id"], t["title"][:60], reason(t, b, at), fmt_est(t, tasks, est)))
    asks = [t for t in tasks.values() if t["type"] == "ask" and t["status"] == "open"]
    if asks:
        lines.append("развилок открыто %d: %s" % (len(asks), " · ".join("%s (дефолт %s до %s)" % (t["id"], t["default"].upper(), fmt_dt(parse_ts(t["deadline"]))) for t in asks)))
    hands = [t for t in tasks.values() if t["type"] == "ask" and t["status"] == "open" and t.get("need") in ("access", "hands")]
    for t in hands:
        lines.append("✋ твои руки: %s — ждёт %d дн%s" % (t["title"], (at - parse_ts(t["created"])).days, (" · цена простоя: " + t["cost"]) if t.get("cost") else ""))
    # 6. перепроверка трёх случайных закрытий — только в полном режиме
    if not a.short:
        lines += recheck_random(tasks)
    if not lines:
        lines.append("🟢 обязательств нет; леджер %s" % LEDGER)
    limit = 12 if a.short else 60
    if len(lines) > limit:
        hidden = len(lines) - limit
        lines = lines[:limit - 1] + ["… скрыто %d, полностью в %s" % (hidden + 1, OPEN_MD)]
    print("\n".join(lines))
    touch_state("open")
    regen(tasks)
    return 0


def cmd_scan_sent(a):
    """Перечитать исходящие за N часов, похожее на обещание → карточка promise (без дублей по ключу)."""
    tasks, _ = build_state(read_ledger())
    at = now()
    since = at - dt.timedelta(hours=a.hours)
    seen_keys = {t.get("key") for t in tasks.values() if t["type"] == "promise" and t.get("key")}
    found, created = 0, 0
    for r in _sent_records():
        ts = parse_ts(r.get("ts"))
        if ts and ts < since:
            continue
        hit = looks_like_promise(r["text"])
        if not hit:
            continue
        found += 1
        key = "%s/%s" % (r["chat"], r["id"])
        if key in seen_keys:
            continue
        hit, gist, due, _note = promise_gist_and_due(r["text"], ts or at)
        ns = argparse.Namespace(to=r["chat"], who=None, what=gist, due=iso(due) if due else None,
                                kind="reply", key=key, src="client", msg=key, auto=True)
        cmd_promise(ns)
        seen_keys.add(key)
        created += 1
    print("scan-sent: исходящих с признаком обещания %d, новых карточек %d — каждую пометить «карточка есть» или drop --why «не обещание»" % (found, created))
    touch_state("scan-sent")
    return 0


def _promise_gist(text, hit):
    """Суть обещания: предложение, где встретился признак, до 120 знаков."""
    for sent in re.split(r"(?<=[.!?\n])\s+", text or ""):
        if hit.lower() in sent.lower().replace("ё", "е"):
            return sent.strip()[:120]
    return (text or "").strip()[:120]


def cmd_report(a):
    tasks, _ = build_state(read_ledger())
    t = find(tasks, a.id)
    at = now()
    est = load_estimates()
    m, k, src = estimate(t.get("kind", "misc"), tasks, est)
    who = t.get("who") or t.get("chat_id") or "—"
    print("📌 %s · %s\n   %s · src %s · %s · kind %s" % (t["id"], t.get("title") or t.get("what"), t["type"], t.get("src", "—"), who, t.get("kind", "—")))
    # Результат
    print("Результат:")
    if t["status"] == "done":
        d = t["done"]
        print("   🟢 закрыто %s · улика %s (%s)%s\n   факт %d мин%s · прогноз был ≈%d мин / %dk" % (
            fmt_dt(parse_ts(d["ts"])), d["proof"][:70], d["proof_result"], " · БЕЗ МАШИННОЙ ПРОВЕРКИ" if d.get("force") else "",
            d.get("minutes", 0), (" · %d ток" % d["tokens"]) if d.get("tokens") else "", m, k / 1000))
        print("   proof: %s" % d.get("proof_file"))
    elif t["status"] == "kept":
        print("   🟢 сдержано%s · %s" % (" с опозданием" if t["keep"].get("late") else "", t["keep"].get("detail")))
    elif t["status"] == "broken":
        print("   🔴 сорвано: %s" % (t["broken"].get("why") or "—"))
    elif t["status"] == "dropped":
        print("   снято: %s" % t["drop"].get("why"))
    elif t["status"] == "decided":
        print("   решено %s (%s): %s" % (t["decide"]["choice"].upper(), t["decide"]["by"], t["decide"].get("text") or ""))
    elif t["status"] == "active":
        spent = int((at - parse_ts(t["taken"])).total_seconds() // 60)
        print("   в работе %d мин из ≈%d (%s) · токенов ≈%dk%s" % (spent, m, src, k / 1000, " · 🟡 перерасход времени" if spent > m else ""))
    elif t["type"] == "ask":
        print("   ждёт слова владельца · дефолт %s до %s%s" % (t["default"].upper(), fmt_dt(parse_ts(t["deadline"])), "" if t.get("delivered") else " · ⏳ не доставлено, таймер стоит"))
    else:
        due = parse_ts(t.get("due") or t.get("deadline"))
        print("   в очереди · прогноз ≈%d мин · %dk ток (%s)%s" % (m, k / 1000, src, (" · срок " + fmt_dt(due)) if due else ""))
    if t.get("check"):
        print("   критерий: %s" % t["check"])
    if t["notes"]:
        print("   последняя запись: %s" % t["notes"][-1]["text"][:100])
    # Вопросы с рекомендациями
    print("Вопросы с рекомендациями:")
    linked = [x for x in tasks.values() if x["type"] == "ask" and x.get("task") == t["id"] and x["status"] == "open"]
    if t["type"] == "ask":
        linked = [t]
    if not linked:
        if t["type"] == "promise" and not t.get("due"):
            print("   — срок не назван: А) назвать срок сама и держать · Б) спросить человека. Рекомендую А.")
        else:
            print("   — вопросов нет")
    for x in linked:
        print("   — %s: %s\n     А: %s\n     Б: %s\n     рекомендую %s · до %s · откат: %s" % (
            x["id"], x["title"], x["a"], x["b"], x["default"].upper(), fmt_dt(parse_ts(x["deadline"])), x["undo"]))
    # План действий
    print("План действий:")
    if t["type"] == "promise" and t["status"] == "open":
        print("   1. сделать «%s» → отправить %s\n   2. task.py keep %s --msg <chat>/<message_id>" % (t.get("what"), who, t["id"]))
    elif t["type"] == "task" and t["status"] == "open":
        print("   1. task.py take %s\n   2. работа ≈%d мин\n   3. task.py done %s --proof <%s…> --tokens N" % (
            t["id"], m, t["id"], "human:|msg:" if t.get("src") == "client" else "cmd:|url:|msg:"))
    elif t["type"] == "task" and t["status"] == "active":
        print("   1. довести до критерия «%s»\n   2. task.py done %s --proof … --tokens N" % ((t.get("check") or "")[:60], t["id"]))
    elif t["type"] == "ask" and t["status"] == "open":
        print("   1. показать владельцу (А/Б, дефолт %s)\n   2. task.py note %s delivered:<chat>/<id> — часы пойдут\n   3. ответ → task.py decide %s --choice a|b --by owner" % (t["default"].upper(), t["id"], t["id"]))
    else:
        print("   — закрыто, действий нет")
    if t["type"] == "ask" and t.get("need") in ("access", "hands"):
        print("   ✋ твои руки: %s" % t["title"])
    return 0


# ---------- OPEN.md / NOW.md ----------
def regen(tasks=None):
    tasks = tasks if tasks is not None else build_state(read_ledger())[0]
    at = now()
    est = load_estimates()
    L = ["# OPEN — живая очередь обязательств", "",
         "> Генерируется из `tasks/ledger.jsonl`. **Править через `ops/task.py`**, руками — нет. Обновлено %s." % at.strftime("%d.%m.%Y %H:%M"), ""]
    broken, burning = _promise_lines(tasks, at)
    if broken or burning:
        L += ["## Обещания — сорвано и горит"] + ["- " + x for x in broken + burning] + [""]
    active = [t for t in tasks.values() if t["status"] == "active"]
    if active:
        L += ["## В работе (≤3, одна стройка)"] + ["- %s **%s** · %s · с %s · %s" % (t["id"], t["title"], t.get("kind"), fmt_dt(parse_ts(t["taken"])), fmt_est(t, tasks, est)) for t in active] + [""]
    q = queue(tasks, at)
    if q:
        L += ["## Очередь (порядок = task.py next)"]
        for t in q:
            b, _ = bucket(t, at)
            L.append("- %s %s · %s · %s · %s%s" % (t["id"], t.get("title") or t.get("what"), t.get("src", "—"), reason(t, b, at), fmt_est(t, tasks, est),
                                                (" · stalled" if t.get("stalled") else "")))
        L.append("")
    asks = [t for t in tasks.values() if t["type"] == "ask" and t["status"] == "open"]
    if asks:
        L += ["## Развилки (≤3)"]
        for t in asks:
            L.append("- %s %s — А: %s · Б: %s · дефолт %s до %s · откат: %s%s" % (
                t["id"], t["title"], t["a"], t["b"], t["default"].upper(), fmt_dt(parse_ts(t["deadline"])), t["undo"],
                "" if t.get("delivered") else " · ⏳ не доставлено, таймер стоит"))
        L.append("")
    week = at - dt.timedelta(days=7)
    closed = [t for t in tasks.values() if t["status"] in ("done", "kept", "broken", "decided", "dropped") and parse_ts(t["events"][-1]["ts"]) > week]
    if closed:
        L += ["## Закрыто за 7 дней (перечислением, с уликой)"]
        for t in sorted(closed, key=lambda x: x["events"][-1]["ts"], reverse=True):
            last = t["events"][-1]
            extra = ""
            if t["status"] == "done":
                extra = " · улика `%s` → %s%s" % (last.get("proof", "")[:50], last.get("proof_result"), " · **--force**" if last.get("force") else "")
            elif t["status"] == "kept":
                extra = " · msg %s" % last.get("msg")
            L.append("- %s %s %s · %s%s" % (fmt_dt(parse_ts(last["ts"])), t["status"], t["id"], t.get("title") or t.get("what"), extra))
        L.append("")
    forced = sum(1 for t in tasks.values() if t["status"] == "done" and t["done"].get("force"))
    done_n = sum(1 for t in tasks.values() if t["status"] == "done")
    L += ["## Прогнозы по видам (медиана фактов, иначе стартовая оценка)"]
    kinds = sorted({k for k in est if not k.startswith("_")} | {(t.get("kind") or "misc") for t in tasks.values()})
    for k in kinds:
        m, tk, src = estimate(k, tasks, est)
        L.append("- %s: ≈%d мин · %dk ток (%s)" % (k, m, tk / 1000, src))
    L += ["", "закрыто всего %d, из них без машинной проверки %d" % (done_n, forced)]
    with open(OPEN_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def cmd_regen(a):
    regen()
    print("OPEN.md обновлён: %s" % OPEN_MD)
    return 0


def cmd_nowblock(a):
    """Блок для NOW.md между <!-- tasks:begin --> и <!-- tasks:end -->; без маркеров — только печать."""
    tasks, _ = build_state(read_ledger())
    at = now()
    broken, burning = _promise_lines(tasks, at)
    q = queue(tasks, at)
    active = [t for t in tasks.values() if t["status"] == "active"]
    body = ["<!-- tasks:begin -->", "**Обязательства (`task.py open`, %s):**" % at.strftime("%d.%m %H:%M")]
    body += broken + burning
    if active:
        body.append("в работе: " + " · ".join("%s %s" % (t["id"], t["title"][:40]) for t in active))
    for t in q[:5]:
        body.append("· %s %s" % (t["id"], (t.get("title") or t.get("what"))[:70]))
    if len(q) > 5:
        body.append("… скрыто %d, полностью в tasks/OPEN.md" % (len(q) - 5))
    body.append("<!-- tasks:end -->")
    block = "\n".join(body)
    try:
        src = open(NOW_MD, encoding="utf-8").read()
    except Exception:
        print(block); return 0
    if "<!-- tasks:begin -->" in src and "<!-- tasks:end -->" in src:
        new = re.sub(r"<!-- tasks:begin -->.*?<!-- tasks:end -->", lambda m: block, src, flags=re.S)
        with open(NOW_MD, "w", encoding="utf-8") as f:
            f.write(new)
        print("NOW.md: блок обновлён")
    else:
        print(block)
        print("(в NOW.md нет маркеров <!-- tasks:begin/end --> — вставь их один раз, дальше блок обновляется сам)")
    return 0


# ---------- CLI ----------
def build_parser():
    p = argparse.ArgumentParser(prog="task.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd")

    s = sp.add_parser("new", help="обязательство (ход/стройка)")
    s.add_argument("--title", required=True)
    s.add_argument("--check", help="чем проверяется закрытие (обязательно)")
    s.add_argument("--verify", help="команда проверки, исполняется при done")
    s.add_argument("--kind", default="misc", help="reply·doc·translate·build·research·fix·design·report·misc")
    s.add_argument("--src", default="owner", choices=["owner", "client", "self", "director"])
    s.add_argument("--who"); s.add_argument("--chat", help="chat_id клиента/чата")
    s.add_argument("--anchor", help="msg:<chat>/<id> или voice:файл#N")
    s.add_argument("--due"); s.add_argument("--bleed", help="измеренная утечка числом")
    s.add_argument("--paid", action="store_true", help="взяты деньги")
    s.add_argument("--promised-due", dest="promised_due"); s.add_argument("--promised-msg", dest="promised_msg")
    s.set_defaults(fn=cmd_new)

    s = sp.add_parser("take"); s.add_argument("id"); s.set_defaults(fn=cmd_take)
    s = sp.add_parser("note"); s.add_argument("id"); s.add_argument("text"); s.set_defaults(fn=cmd_note)

    s = sp.add_parser("done", help="закрыть с уликой (без --proof — код 2)")
    s.add_argument("id"); s.add_argument("--proof"); s.add_argument("--tokens", type=int)
    s.add_argument("--force", action="store_true", help="принято со слов, машина не проверила — остаётся в строке навсегда")
    s.add_argument("--timeout", type=int, default=60)
    s.set_defaults(fn=cmd_done)

    s = sp.add_parser("drop"); s.add_argument("id"); s.add_argument("--why"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_drop)

    s = sp.add_parser("promise", help="обещание человеку — до отправки сообщения")
    s.add_argument("--to", required=True, help="chat_id"); s.add_argument("--who"); s.add_argument("--what", required=True)
    s.add_argument("--due"); s.add_argument("--kind", default="reply"); s.add_argument("--key", help="chat/message_id — идемпотентность")
    s.add_argument("--src", default="client"); s.add_argument("--msg", help="сообщение, где прозвучало (chat/id)")
    s.add_argument("--auto", action="store_true", help="заведено хуком, не рукой")
    s.set_defaults(fn=cmd_promise)

    s = sp.add_parser("keep"); s.add_argument("id"); s.add_argument("--msg", help="<chat>/<message_id>"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_keep)
    s = sp.add_parser("broken"); s.add_argument("id"); s.add_argument("--why"); s.set_defaults(fn=cmd_broken)

    s = sp.add_parser("ask", help="развилка владельцу: А/Б, дефолт, откат, срок")
    s.add_argument("--title", required=True); s.add_argument("--a"); s.add_argument("--b"); s.add_argument("--default")
    s.add_argument("--undo"); s.add_argument("--deadline"); s.add_argument("--need", default="decision", choices=["decision", "access", "hands"])
    s.add_argument("--task", help="к какому M-… относится"); s.add_argument("--cost", help="цена ожидания числом из команды")
    s.set_defaults(fn=cmd_ask)

    s = sp.add_parser("decide"); s.add_argument("id"); s.add_argument("--choice", required=True, choices=["a", "b"])
    s.add_argument("--by", default="owner", choices=["owner", "default"]); s.add_argument("--text"); s.set_defaults(fn=cmd_decide)

    s = sp.add_parser("next"); s.add_argument("--n", type=int, default=5); s.set_defaults(fn=cmd_next)
    s = sp.add_parser("open"); s.add_argument("--short", action="store_true", help="≤12 строк для SessionStart"); s.set_defaults(fn=cmd_open)
    s = sp.add_parser("scan-sent"); s.add_argument("--hours", type=int, default=24); s.set_defaults(fn=cmd_scan_sent)
    s = sp.add_parser("report"); s.add_argument("id"); s.set_defaults(fn=cmd_report)
    s = sp.add_parser("regen"); s.set_defaults(fn=cmd_regen)
    s = sp.add_parser("nowblock"); s.set_defaults(fn=cmd_nowblock)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help(); return 0
    # note может принимать delivered:<chat>/<id> — фиксирует доставку развилки (часы пошли)
    if a.cmd == "note" and a.text.startswith("delivered:"):
        tasks, _ = build_state(read_ledger())
        t = find(tasks, a.id)
        append({"ev": "delivered", "id": t["id"], "msg": a.text[len("delivered:"):]})
        print("доставка зафиксирована: %s %s — часы пошли" % (t["id"], a.text))
        return 0
    try:
        return a.fn(a) or 0
    except SystemExit as ex:
        return ex.code if isinstance(ex.code, int) else 2


if __name__ == "__main__":
    sys.exit(main())
