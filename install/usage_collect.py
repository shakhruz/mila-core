#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Журнал расхода: сколько стоил день работы агента.

Claude Code пишет транскрипт каждой сессии в ~/.claude/projects/<проект>/<id>.jsonl.
В нём у каждого ответа модели есть usage: входные токены, выходные, отдельно
запись кэша и чтение кэша. Стоимости в транскрипте НЕТ — её здесь считаем сами
по таблице цен, и в каждой строке журнала помечаем, по какой именно (basis).
Цена — единственное, что тут не факт, а допущение; всё остальное измерено.

Кэш — половина смысла этой затеи. Чтение кэша дешевле входа примерно в десять
раз, запись — дороже входа. Пока их не видно раздельно, «дорогой день» и
«длинный день» выглядят одинаково.

Пишет usage-log.jsonl (одна строка на сессию за прогон, дозапись) и печатает
сводку. Ничего не удаляет и не переписывает: журнал только растёт.

ВАЖНО про деньги: если сессия работает по подписке Claude Code, эти доллары
никто не списывает. Цифра отвечает на другой вопрос — во сколько тот же объём
обошёлся бы по прейскуранту API. Это мера того, что подписка отдаёт. Для
BYOK-ключа (клиент платит своим ключом провайдера) цифра становится настоящим
расходом, и тогда её и надо сверять с выпиской.

  usage_collect.py                 # сводка за сегодня
  usage_collect.py --days 7        # за неделю, с разбивкой по дням
  usage_collect.py --by-model      # разбивка по моделям
  usage_collect.py --json          # машинный вывод
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def local_day(ts):
    """Дата по местному времени, а не по UTC.

    Транскрипты пишутся в UTC, а «за какой день» человек спрашивает по своим
    часам. В Ташкенте разница пять часов, то есть работа с полуночи до пяти
    утра — ровно рабочие часы этого дома — попадала во вчерашний день, и
    отчёт за сегодня показывал ноль там, где ночь была самой дорогой.
    """
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date().isoformat()

CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
PROJECTS = os.path.join(CLAUDE_DIR, "projects")
LOG = os.path.join(CLAUDE_DIR, "usage-log.jsonl")
PRICES_FILE = os.path.join(CLAUDE_DIR, "prices.json")

# Цены за миллион токенов. Правь под свой договор — это допущение, а не факт,
# и оно уезжает в каждую строку журнала полем basis, чтобы старые записи не
# пришлось пересчитывать задним числом при смене тарифа.
DEFAULT_PRICES = {
    "_basis": "api-list-2026-08",
    "claude-opus-5":    {"in": 15.0, "out": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4-8":  {"in": 15.0, "out": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-5":  {"in":  3.0, "out": 15.0, "cache_write":  3.75, "cache_read": 0.30},
    "claude-fable-5":   {"in":  3.0, "out": 15.0, "cache_write":  3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"in":  0.8, "out":  4.0, "cache_write":  1.00, "cache_read": 0.08},
}


def load_prices():
    """Свой прайс перекрывает встроенный. Отсутствие файла — не ошибка."""
    prices = dict(DEFAULT_PRICES)
    try:
        with open(PRICES_FILE, encoding="utf-8") as f:
            prices.update(json.load(f))
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as e:
        print("prices.json не разобран (%s) — считаю по встроенным ценам" % e,
              file=sys.stderr)
    return prices


def price_for(prices, model):
    """Точное совпадение, иначе — по префиксу семейства, иначе None.

    None означает «модель неизвестна»: токены посчитаем, деньги — нет. Соврать
    цифрой хуже, чем показать прочерк.
    """
    if model in prices:
        return prices[model], model
    for key, val in prices.items():
        if key.startswith("_"):
            continue
        family = key.rsplit("-", 1)[0]
        if model.startswith(family):
            return val, key
    return None, None


def cost(usage, price):
    if not price:
        return None
    m = 1_000_000
    return (usage["in"] / m * price["in"]
            + usage["out"] / m * price["out"]
            + usage["cache_write"] / m * price["cache_write"]
            + usage["cache_read"] / m * price["cache_read"])


def blank():
    return {"in": 0, "out": 0, "cache_write": 0, "cache_read": 0,
            "thinking": 0, "turns": 0}


def add(dst, src):
    for k in ("in", "out", "cache_write", "cache_read", "thinking", "turns"):
        dst[k] += src[k]


def add_models(dst, src):
    """Слить корзины «модель → расход», не смешивая модели между собой."""
    for model, rec in src.items():
        add(dst[model], rec)


def cost_models(by_model, prices):
    """Стоимость корзины моделей: каждая по своей цене.

    Возвращает (сумма, была ли модель без цены). Незнакомая модель считается
    в токенах и не притворяется деньгами — иначе в отчёт попадает уверенная
    цифра, взятая из соседнего тарифа.
    """
    total, unknown = 0.0, False
    for model, rec in by_model.items():
        price, _ = price_for(prices, model)
        c = cost(rec, price)
        if c is None:
            unknown = True
        else:
            total += c
    return total, unknown


def totals_of(by_model):
    out = blank()
    for rec in by_model.values():
        add(out, rec)
    return out


def scan_file(path, since):
    """Возвращает {день: {модель: расход}}. Битые строки пропускаются молча:
    транскрипт пишется на лету, последняя строка может быть недописана."""
    out = defaultdict(lambda: defaultdict(blank))
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with f:
        for line in f:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            ts = d.get("timestamp") or ""
            day = local_day(ts)
            if not day or (since and day < since):
                continue
            model = msg.get("model") or "unknown"
            if model == "<synthetic>":
                continue  # служебные сообщения оболочки, не вызов модели
            rec = out[day][model]
            rec["in"] += u.get("input_tokens", 0) or 0
            rec["out"] += u.get("output_tokens", 0) or 0
            rec["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
            rec["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
            det = u.get("output_tokens_details") or {}
            rec["thinking"] += det.get("thinking_tokens", 0) or 0
            rec["turns"] += 1
    return out


def scan_by_chat(path, since):
    """Расход в разрезе чатов — «сколько стоил этот клиент», а не «этот день».

    Прямой связи «ход модели → чат» в транскрипте нет: одна сессия обслуживает
    двадцать чатов вперемешку. Но есть косвенная и честная: работа, которая
    закончилась ответом в чат X, делалась ради X. Копим расход ходов в корзину
    и при каждом ответе в чат высыпаем её туда.

    Что этот способ НЕ умеет: разделить работу, которая шла на два чата сразу,
    и отделить чтение чужого чата от работы по нему. Поэтому цифра здесь —
    порядок величины, а не счёт. Для вопроса «какой клиент дороже всех» этого
    достаточно, для выставления счёта — нет.
    """
    # Корзина ведётся ПО МОДЕЛЯМ. Раньше всё складывалось в одну кучу и потом
    # считалось по цене Opus — а на этой машине четверть ходов идёт дешёвыми
    # моделями. Завышение доходило до пяти раз на этих ходах, и на таких
    # цифрах принимается решение «клиент невыгоден».
    per_chat = defaultdict(lambda: defaultdict(blank))
    basket = defaultdict(blank)
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return per_chat
    with f:
        for line in f:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            ts = local_day(d.get("timestamp") or "")
            if since and ts and ts < since:
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue

            u = msg.get("usage")
            if isinstance(u, dict) and d.get("type") == "assistant":
                model = msg.get("model") or ""
                if model != "<synthetic>":
                    b = basket[model or "unknown"]
                    b["in"] += u.get("input_tokens", 0) or 0
                    b["out"] += u.get("output_tokens", 0) or 0
                    b["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
                    b["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
                    b["turns"] += 1

            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                inp = block.get("input")
                if not isinstance(inp, dict):
                    continue
                cid = inp.get("chat_id")
                if not cid:
                    continue
                add_models(per_chat[str(cid)], basket)
                basket = defaultdict(blank)
    if any(b["turns"] for b in basket.values()):
        add_models(per_chat["(без адресата)"], basket)
    return per_chat


def chat_names(state_dir):
    """Имена чатов из реестра, чтобы отчёт читался людьми, а не числами."""
    names = {}
    path = os.path.join(state_dir, "CHATS.md")
    try:
        for line in open(path, encoding="utf-8"):
            m = re.match(r"\|\s*([^|]+?)\s*\|\s*`(-?\d+)`", line)
            if m and not m.group(1).startswith("_"):
                names[m.group(2)] = m.group(1)
    except OSError:
        pass
    return names


def human(n):
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%.0fk" % (n / 1_000)
    return str(n)


def main():
    ap = argparse.ArgumentParser(description="Расход токенов по сессиям Claude Code")
    ap.add_argument("--days", type=int, default=1, help="за сколько последних дней (1 = сегодня)")
    ap.add_argument("--by-model", action="store_true", help="разбивка по моделям")
    ap.add_argument("--by-chat", action="store_true",
                    help="разбивка по чатам: сколько стоил каждый клиент")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--no-write", action="store_true", help="не дописывать журнал")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).astimezone()
    since = (today - timedelta(days=args.days - 1)).strftime("%Y-%m-%d")
    prices = load_prices()
    basis = prices.get("_basis", "unknown")

    if not os.path.isdir(PROJECTS):
        print("нет каталога проектов: %s" % PROJECTS, file=sys.stderr)
        return 1

    per_day = defaultdict(lambda: defaultdict(blank))
    files = 0
    for root, _dirs, names in os.walk(PROJECTS):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            files += 1
            for day, models in scan_file(os.path.join(root, name), since).items():
                for model, rec in models.items():
                    add(per_day[day][model], rec)

    unknown_models = set()
    days_out = []
    for day in sorted(per_day):
        models_out, day_total, day_cost, priced = [], blank(), 0.0, True
        for model in sorted(per_day[day]):
            rec = per_day[day][model]
            price, matched = price_for(prices, model)
            c = cost(rec, price)
            if c is None:
                priced = False
                unknown_models.add(model)
            else:
                day_cost += c
            add(day_total, rec)
            models_out.append({"model": model, "priced_as": matched, "cost_usd": c, **rec})
        days_out.append({"day": day, "total": day_total,
                         "cost_usd": round(day_cost, 4) if priced else None,
                         "cost_complete": priced, "models": models_out})

    if args.by_chat:
        state = os.environ.get(
            "TELEGRAM_STATE_DIR",
            os.path.join(CLAUDE_DIR, "channels", "telegram"))
        names = chat_names(state)
        per_chat = defaultdict(lambda: defaultdict(blank))
        for root, _dirs, fnames in os.walk(PROJECTS):
            for name in fnames:
                if not name.endswith(".jsonl"):
                    continue
                for cid, by_model in scan_by_chat(
                        os.path.join(root, name), since).items():
                    add_models(per_chat[cid], by_model)
        if not per_chat:
            print("За период с %s работы по чатам не найдено." % since)
            return 0
        rows = sorted(per_chat.items(),
                      key=lambda kv: -cost_models(kv[1], prices)[0])
        print("Расход по чатам · с %s · цены: %s" % (since, basis))
        print("Работа отнесена к чату, ответом в который она закончилась —")
        print("это порядок величины, а не счёт: чтение чужого чата и работа")
        print("сразу на двоих так не делятся.")
        print()
        grand = 0.0
        for cid, by_model in rows:
            c, unknown = cost_models(by_model, prices)
            grand += c
            rec = totals_of(by_model)
            title = names.get(cid, cid)
            print("  %-32s %8s  ходов %-4d  выход %s%s"
                  % (title[:32], "$%.2f" % c, rec["turns"], human(rec["out"]),
                     "  ⚠ есть модель без цены" if unknown else ""))
        print()
        print("  Сумма по чатам: $%.2f — считана по модели каждого хода, "
              "а не по одному тарифу." % grand)
        return 0

    if args.as_json:
        json.dump({"since": since, "basis": basis, "days": days_out},
                  sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0

    if not days_out:
        print("За период с %s расхода не найдено (просмотрено файлов: %d)." % (since, files))
        return 0

    print("Расход · с %s · цены: %s" % (since, basis))
    print("Это оценка «во сколько обошлось бы по прейскуранту API», а не счёт.")
    print("Работа по подписке Claude Code этими деньгами не оплачивается —")
    print("цифра показывает, что подписка отдаёт, а не что списано.")
    print()
    grand, grand_cost, grand_priced = blank(), 0.0, True
    for d in days_out:
        t = d["total"]
        add(grand, t)
        if d["cost_usd"] is None:
            grand_priced = False
        else:
            grand_cost += d["cost_usd"]
        money = "$%.2f" % d["cost_usd"] if d["cost_usd"] is not None else "$?"
        print("%s  %s  ходов %-4d  вход %-6s выход %-6s (размышление %-6s)  "
              "кэш: запись %-6s чтение %-6s"
              % (d["day"], money.rjust(7), t["turns"], human(t["in"]), human(t["out"]),
                 human(t["thinking"]), human(t["cache_write"]), human(t["cache_read"])))
        if args.by_model:
            for m in sorted(d["models"], key=lambda x: -(x["cost_usd"] or 0)):
                mm = "$%.2f" % m["cost_usd"] if m["cost_usd"] is not None else "$?"
                print("           %-18s %s  ходов %-4d  выход %s"
                      % (m["model"], mm.rjust(7), m["turns"], human(m["out"])))

    if len(days_out) > 1:
        print()
        total_money = "$%.2f" % grand_cost if grand_priced else "$%.2f и выше" % grand_cost
        print("итого %s · ходов %d · выход %s · чтение кэша %s"
              % (total_money, grand["turns"], human(grand["out"]), human(grand["cache_read"])))

    if unknown_models:
        print()
        print("цены неизвестны для: %s — токены посчитаны, деньги нет."
              % ", ".join(sorted(unknown_models)))
        print("добавь их в %s, чтобы считалось." % PRICES_FILE)

    if not args.no_write:
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                for d in days_out:
                    f.write(json.dumps({"collected_at": stamp, "basis": basis, **d},
                                       ensure_ascii=False) + "\n")
            os.chmod(LOG, 0o600)
        except OSError as e:
            print("журнал не записан: %s" % e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
