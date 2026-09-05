#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Приёмка страницы машиной: то, что глаз пропускает, а замер — нет.

Правило, которое проверяется одной командой, доживает до третьей страницы;
правило «делай аккуратно» не доживает до второй. Здесь собраны только те
требования, которые можно посчитать. Вкус остаётся человеку.

Каждая проверка выросла из конкретной поломки:

  переполнение  — страница оффера уехала на 130 пикселей вправо на телефоне,
                  а на скриншоте выглядела прекрасно: headless рендерит как
                  десктоп. Врёт скриншот, не врёт число.
  строки в h1   — русский заголовок длиннее английского на 15-25%, и герой,
                  который на макете занимал две строки, в переводе занимает
                  четыре. Считаем строки, а не символы.
  кириллица     — шрифт, подключённый без cyrillic-подмножества, молча
                  подменяется системным: заголовок теряет гарнитуру, и это
                  видно только тому, кто знает, как он должен выглядеть.
  тема          — цвет, объявленный только внутри @media (prefers-color-scheme)
                  или [data-theme], не применяется в состоянии «системная» —
                  страница показывает текст одной темы на фоне другой.
  фокус         — :focus-visible не был ни на одной из трёх страниц, которые
                  мы выпустили за день. Клавиатурой по ним пройти нельзя.
  движение      — prefers-reduced-motion там же: ноль из трёх.

Запуск:  design_check.py <url или путь к файлу> [--json]
Требуется playwright (pip install playwright && playwright install chromium).
"""
import argparse
import json
import os
import re
import sys

VIEWPORTS = [(320, 640, "узкий телефон"), (375, 667, "телефон"),
             (414, 896, "крупный телефон"), (1280, 800, "десктоп")]

JS_AUDIT = r"""
() => {
  const out = {};
  const de = document.documentElement;
  out.overflow = de.scrollWidth - de.clientWidth;

  // Заголовок первого экрана: считаем СТРОКИ, а не символы — перевод меняет
  // именно число строк, и на четвёртой строке герой перестаёт быть героем.
  const h1 = document.querySelector('h1');
  if (h1) {
    const lh = parseFloat(getComputedStyle(h1).lineHeight) || 0;
    out.h1_lines = lh ? Math.round(h1.scrollHeight / lh) : null;
    out.h1_text = (h1.textContent || '').trim().slice(0, 90);
  }

  // Вложенность рамок: третий уровень — это коробка в коробке в коробке.
  const boxed = el => {
    const s = getComputedStyle(el);
    return (s.borderStyle !== 'none' && parseFloat(s.borderWidth) > 0)
        || parseFloat(s.borderRadius) > 6;
  };
  let worst = 0, worstSel = '';
  for (const el of document.querySelectorAll('*')) {
    if (!boxed(el)) continue;
    let depth = 0, p = el;
    while (p && p !== document.body) { if (boxed(p)) depth++; p = p.parentElement; }
    if (depth > worst) { worst = depth; worstSel = el.className || el.tagName; }
  }
  out.box_depth = worst;
  out.box_where = String(worstSel).slice(0, 60);

  // Цифры в колонках без tabular-nums скачут по ширине при каждом изменении.
  // Считаем только ячейки, где цифры ЕСТЬ: заголовок колонки со словом
  // «Очередь» в табличном начертании не нуждается, и требовать его — значит
  // приучать обходить собственный гейт.
  const digitCells = [...document.querySelectorAll('td, th, .num, .kpi b, .money')]
    .filter(el => /\d/.test(el.textContent || ''));
  out.numeric_cells = digitCells.length;
  const bad = digitCells.filter(
    el => !getComputedStyle(el).fontVariantNumeric.includes('tabular'));
  out.numeric_without_tabular = bad.length;
  out.numeric_bad_sample = bad.slice(0, 3).map(
    el => (el.textContent || '').trim().slice(0, 28));

  // Интерактивное без видимого фокуса — страница, непроходимая с клавиатуры.
  out.interactive = document.querySelectorAll(
    'a[href], button, input, select, textarea, [tabindex]').length;

  // Шрифты, реально применённые к тексту.
  const fams = new Set();
  for (const el of document.querySelectorAll('h1,h2,h3,p,td,li,span')) {
    const f = getComputedStyle(el).fontFamily;
    if (f) fams.add(f.split(',')[0].replace(/["']/g, '').trim());
  }
  out.fonts = [...fams].slice(0, 12);

  // Фон body: прозрачный означает, что страница берёт фон хозяина и в другой
  // теме читается наизнанку.
  out.body_bg = getComputedStyle(document.body).backgroundColor;
  out.body_color = getComputedStyle(document.body).color;

  // Пустые секции: заголовок есть, содержимого нет — на месте данных дыра.
  let empty = 0;
  for (const h of document.querySelectorAll('h2, h3')) {
    const sec = h.parentElement;
    if (sec && (sec.textContent || '').trim().length <= (h.textContent || '').trim().length + 5) empty++;
  }
  out.empty_sections = empty;
  return out;
}
"""


def check_source(html):
    """Проверки по исходнику — то, чего не видно из готового DOM."""
    notes = []
    if "focus-visible" not in html:
        notes.append(("фокус", "нет :focus-visible — по странице нельзя пройти клавиатурой"))
    if "prefers-reduced-motion" not in html and re.search(r"@keyframes|transition:", html):
        notes.append(("движение", "есть анимация, но нет prefers-reduced-motion"))

    # Шрифт без кириллицы подменяется молча — но проверять надо ОТВЕТ сервиса,
    # а не строку запроса. Google отдаёт все подмножества сразу и не требует
    # subset= в URL; первая версия этой проверки ругалась на IBM Plex, у
    # которого кириллица есть. Ложная тревога в гейте обесценивает гейт.
    # И ходить надо с настоящим User-Agent: на «python-urllib» сервис отдаёт
    # урезанный ответ без cyrillic-секций, и проверка снова соврёт.
    for m in re.finditer(r"fonts\.googleapis\.com/css2\?([^\"']+)", html):
        q = m.group(1).replace("&amp;", "&")
        for fam in re.findall(r"family=([A-Za-z0-9+]+)", q):
            name = fam.replace("+", " ")
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://fonts.googleapis.com/css2?family=%s&display=swap" % fam,
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                                           "Chrome/128.0.0.0 Safari/537.36"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    css = r.read().decode("utf-8", "replace")
            except Exception as e:
                notes.append(("шрифт", "%s — не удалось проверить глифы (%s)"
                              % (name, type(e).__name__)))
                continue
            if "/* cyrillic" not in css:
                notes.append(("шрифт", "%s без кириллицы — русский текст молча "
                                       "подменится системной гарнитурой" % name))

    # Цвет, определённый ТОЛЬКО под медиа-запросом или [data-theme], не работает
    # в состоянии «системная тема» — самый частый способ выпустить нечитаемую
    # страницу, не заметив этого на своей машине.
    root_block = re.search(r":root\s*\{([^}]*)\}", html)
    root_vars = set(re.findall(r"(--[\w-]+)\s*:", root_block.group(1))) if root_block else set()
    themed = set()
    for m in re.finditer(r"(@media[^{]*prefers-color-scheme[^{]*\{|:root\[data-theme[^{]*\{)"
                         r"([\s\S]{0,2000}?)\}\s*\}?", html):
        themed |= set(re.findall(r"(--[\w-]+)\s*:", m.group(2)))
    orphan = themed - root_vars
    if orphan:
        notes.append(("тема", "переменные объявлены только в тёмной ветке: %s — "
                              "в системной теме их не существует"
                      % ", ".join(sorted(orphan)[:6])))
    if "<title" not in html.lower():
        notes.append(("заголовок", "нет <title> — в галерее и вкладке страница будет безымянной"))
    return notes


def main():
    ap = argparse.ArgumentParser(description="Механическая приёмка страницы")
    ap.add_argument("target", help="URL или путь к html")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    target = args.target
    html = ""
    if os.path.exists(target):
        html = open(target, encoding="utf-8", errors="replace").read()
        url = "file://" + os.path.abspath(target)
    else:
        url = target

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нужен playwright: pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2

    results, problems = {}, []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for w, h, label in VIEWPORTS:
            page = browser.new_page(viewport={"width": w, "height": h})
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(400)
            data = page.evaluate(JS_AUDIT)
            results[label] = data
            if not html:
                html = page.content()
            if data["overflow"] > 0:
                problems.append(("переполнение", "%s (%dpx): страница шире экрана на %d px"
                                 % (label, w, data["overflow"])))
            page.close()
        browser.close()

    desktop = results.get("десктоп", {})
    phone = results.get("телефон", {})

    if phone.get("h1_lines") and phone["h1_lines"] > 3:
        problems.append(("герой", "заголовок занимает %d строки на телефоне — потолок три"
                         % phone["h1_lines"]))
    if desktop.get("box_depth", 0) >= 3:
        problems.append(("коробки", "рамка внутри рамки внутри рамки (%s) — оставь одну на секцию"
                         % desktop.get("box_where", "")))
    nw = desktop.get("numeric_without_tabular", 0)
    if nw and desktop.get("numeric_cells", 0):
        problems.append(("цифры", "%d из %d ячеек с числами без tabular-nums (%s) — колонки поедут"
                         % (nw, desktop["numeric_cells"],
                            ", ".join(desktop.get("numeric_bad_sample") or []))))
    bg = desktop.get("body_bg", "")
    if bg in ("rgba(0, 0, 0, 0)", "transparent"):
        problems.append(("фон", "у body прозрачный фон — страница возьмёт фон хозяина "
                                "и в другой теме прочтётся наизнанку"))
    if desktop.get("empty_sections"):
        problems.append(("пустоты", "%d заголовков без содержимого — либо данные, "
                                    "либо честная строка «нет данных»"
                         % desktop["empty_sections"]))
    problems.extend(check_source(html))

    if args.as_json:
        json.dump({"url": url, "problems": [{"kind": k, "text": t} for k, t in problems],
                   "measurements": results}, sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 1 if problems else 0

    print("Приёмка: %s" % url)
    print()
    for label, d in results.items():
        print("  %-16s ширина ок · переполнение %d · h1 строк %s · шрифты: %s"
              % (label, d["overflow"], d.get("h1_lines", "—"),
                 ", ".join(d.get("fonts", [])[:3])))
    print()
    if not problems:
        print("🟢 замечаний нет.")
        return 0
    print("Замечаний: %d" % len(problems))
    for kind, text in problems:
        print("  · %-14s %s" % (kind, text))
    return 1


if __name__ == "__main__":
    sys.exit(main())
