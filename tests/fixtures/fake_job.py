#!/usr/bin/env python3
"""Фейковая задача-подпроцесс для тестов ``app/jobs.py`` (шаг 3a).

Печатает JSON-lines протокола в stdout (hello → progress → done/error),
приветствие — в stderr. Настоящие ``cli/`` в тестах не участвуют:
интеграция с ними — шаг 3d.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def emit(payload: dict) -> None:
    """Строка протокола в stdout (flush — иначе буфер переживёт exit)."""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="фейковая задача тестов 3a")
    parser.add_argument("--steps", type=int, default=3, help="шагов прогресса")
    parser.add_argument("--sleep", type=float, default=0.05, help="пауза между шагами, с")
    parser.add_argument("--fail-at", type=int, default=None, help="на шаге K: error+exit 1")
    parser.add_argument(
        "--respect-cancel",
        action="store_true",
        help="проверять файл отмены перед каждым шагом",
    )
    parser.add_argument("--cancel-file", default="", help="путь к <id>.cancel")
    parser.add_argument(
        "--pid",
        action="store_true",
        help="печатать hello с pid (печатается всегда; флаг для совместимости)",
    )
    args = parser.parse_args(argv)

    print("fake_job: started", file=sys.stderr, flush=True)
    emit({"type": "hello", "pid": os.getpid()})

    cancel_path = Path(args.cancel_file) if args.cancel_file else None
    steps = max(0, args.steps)
    for step in range(1, steps + 1):
        if args.respect_cancel and cancel_path is not None and cancel_path.exists():
            emit({"type": "error", "error": "cancelled by user"})
            return 3
        if args.fail_at is not None and step == args.fail_at:
            emit({"type": "error", "error": "boom"})
            return 1
        progress = step / steps if steps else 1.0
        emit({"type": "progress", "progress": progress, "message": f"step {step}"})
        time.sleep(max(0.0, args.sleep))

    emit({"type": "done"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
