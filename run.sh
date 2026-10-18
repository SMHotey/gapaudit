#!/usr/bin/env bash
# Запуск GapAudit одной командой: ./run.sh [аргументы desktop.py]
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    echo "[run.sh] окружение .venv не найдено, ставлю его через uv..."
    uv venv --python 3.11
    uv pip install -e ".[dev]"
    if [ -x ".venv/Scripts/python.exe" ]; then
        PY=".venv/Scripts/python.exe"
    else
        PY=".venv/bin/python"
    fi
fi

exec "$PY" desktop.py "$@"
