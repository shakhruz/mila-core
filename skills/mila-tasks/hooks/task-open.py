#!/usr/bin/env python3
"""SessionStart-хук: печатает `task.py open --short` (≤12 строк) — сорванные и горящие обещания первыми.
Печатает, не блокирует. Любая своя поломка — одна строка и выход 0."""
import os
import subprocess
import sys

TASK_PY = os.environ.get("MILA_TASK_PY") or next((p for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "task.py"), os.path.expanduser("~/milagpt/ops/task.py")) if os.path.exists(p)), os.path.expanduser("~/milagpt/ops/task.py"))
try:
    if not os.path.exists(TASK_PY):
        print("task-open: нет %s" % TASK_PY)
        sys.exit(0)
    p = subprocess.run([sys.executable, TASK_PY, "open", "--short"], capture_output=True, text=True, timeout=25)
    out = (p.stdout or "").strip()
    if out:
        print("📋 обязательства (task.py open):\n" + out)
    if p.returncode != 0 and p.stderr:
        print("task-open: код %s — %s" % (p.returncode, p.stderr.strip().splitlines()[-1][:160]))
except Exception as ex:  # хук не имеет права ронять старт сессии
    print("task-open: сломан сам — %s" % str(ex)[:160])
sys.exit(0)
