#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка Telegram-сообщения (MarkdownV2) с копи-блоками.

Использование из кода/CLI:
    python3 tg_md2.py obychny_tekst.txt          # экранировать весь текст
или как модуль:
    from tg_md2 import esc, code_block, build
    text = build(esc("Готово, копируйте целиком:"), code_block("Добрый день! ..."), esc("Отправляйте как есть."))

Правила MarkdownV2:
- вне код-блоков экранируются: _ * [ ] ( ) ~ ` > # + - = | { } . !
- внутри ``` ``` экранируются только ` и \\
Отправка: mcp reply с format="markdownv2".
"""
import sys

_SPECIALS = r'_*[]()~`>#+-=|{}.!'


def esc(s: str) -> str:
    """Экранировать обычный текст для MarkdownV2."""
    return "".join("\\" + c if c in _SPECIALS else c for c in s)


def code_block(s: str) -> str:
    """Копи-блок: моноширинный, копируется одним тапом."""
    body = s.replace("\\", "\\\\").replace("`", "\\`")
    return "```\n" + body + "\n```"


def build(*parts: str) -> str:
    """Склеить части пустой строкой. Части-код-блоки передавать уже через code_block()."""
    return "\n\n".join(p for p in parts if p)


if __name__ == "__main__":
    data = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
    print(esc(data))
