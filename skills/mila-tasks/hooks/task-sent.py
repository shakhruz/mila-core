#!/usr/bin/env python3
"""PostToolUse-хук на mcp__plugin_mila-telegram_telegram__reply. ПИШУЩИЙ, не блокирующий.

1. Каждое отправленное сообщение → tasks/sent.jsonl (chat_id, message_id, текст) — это журнал
   исходящих для `keep --msg` и `scan-sent` (reply плагина в channels/telegram/sent/ не пишет).
2. Если в тексте есть признак обещания («пришлю», «сделаю», «в течение», «к вечеру», «сегодня»,
   «завтра», «до …», «готово будет», «через …») — заводит promise с chat_id и вычисленным due;
   срок не разобрался → due=null и метка «уточнить срок». Ложные срабатывания допустимы, пропуски — нет.
Ключ идемпотентности — chat/message_id: повторный запуск строку не дублирует.
Выход всегда 0; свои ошибки — в tasks/hooks.log.
"""
import datetime as dt
import json
import os
import re
import sys

TASK_PY = os.environ.get("MILA_TASK_PY") or next((p for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "task.py"), os.path.expanduser("~/milagpt/ops/task.py")) if os.path.exists(p)), os.path.expanduser("~/milagpt/ops/task.py"))


def log(msg):
    try:
        base = os.environ.get("MILA_TASKS_DIR") or os.path.expanduser("~/milagpt/tasks")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "hooks.log"), "a", encoding="utf-8") as f:
            f.write("%s task-sent: %s\n" % (dt.datetime.now().isoformat(timespec="seconds"), msg))
    except Exception:
        pass


def response_text(resp):
    """tool_response бывает строкой, списком блоков или dict с content — собираем текст."""
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return " ".join(response_text(x) for x in resp)
    if isinstance(resp, dict):
        if "text" in resp and isinstance(resp["text"], str):
            return resp["text"]
        if "content" in resp:
            return response_text(resp["content"])
        return json.dumps(resp, ensure_ascii=False)
    return str(resp)


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    tool = payload.get("tool_name") or ""
    if not tool.endswith("__reply"):
        return 0
    args = payload.get("tool_input") or {}
    chat_id = str(args.get("chat_id") or "").strip()
    text = args.get("text") or ""
    rtext = response_text(payload.get("tool_response"))
    # `sent (id: 123)` · `sent 2 parts (ids: 123, 124)`
    m = re.search(r"ids?:\s*([\d,\s]+)", rtext)
    ids = re.findall(r"\d+", m.group(1)) if m else []
    if "failed" in rtext.lower() and not ids:
        log("reply failed, chat %s — не записываю" % chat_id)
        return 0
    if not chat_id or not ids:
        log("нет chat_id/ids в ответе: %s" % rtext[:120])
        return 0
    sys.path.insert(0, os.path.dirname(TASK_PY))
    import task  # noqa: E402
    task.ensure_dirs()
    rec = {"ts": task.iso(task.now()), "chat_id": chat_id, "ids": ids, "text": text,
           "session": payload.get("session_id"), "files": args.get("files") or []}
    with open(task.SENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    hit, gist, due, note = task.promise_gist_and_due(text)
    if not hit:
        return 0
    key = "%s/%s" % (chat_id, ids[0])
    import argparse
    ns = argparse.Namespace(to=chat_id, who=None, what=(gist or "")[:120] + ((" · " + note) if note else ""),
                            due=task.iso(due) if due else None, kind="reply", key=key,
                            src="client", msg=key, auto=True)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        task.cmd_promise(ns)
    log("promise by «%s» → %s" % (hit, buf.getvalue().strip()[:160]))
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        log("сломан сам — %s" % str(ex)[:200])
    sys.exit(0)
