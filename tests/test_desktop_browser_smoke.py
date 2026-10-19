"""``desktop.py --browser``: живой подпроцесс (шаг 3c, ``@pytest.mark.slow``).

Проверяет связку «uvicorn поднялся → /api/health отвечает → токен записан →
процесс гасится» на реальном запуске ``desktop.py``. Окна в режиме ``--browser``
нет, поэтому тест идёт на любой ОС; на Windows ``terminate`` (TerminateProcess)
отдаёт код выхода 1 — это не падение с трейсбеком (само это и проверяем).

``GAPAUDIT_NO_BROWSER=1`` — чтобы проверка не открывала пользователю вкладку
браузера (переменная описана в docstring ``desktop.py``); ``-u`` — чтобы
вывод дочернего процесса не потерялся при terminate.

Ловушки, из-за которых тест написан именно так:

* ``session.token`` появляется ПОСЛЕ того, как health уже ответил (desktop.py
  сначала дожидается свой health, потом пишет токен) — поэтому файл ждём
  с опросом, а не проверяем одним мгновенным ``exists()``;
* читать ``stdout``/``stderr`` живого процесса нельзя — ``read()`` блокируется
  до EOF, и упавший тест зависнет вместо отчёта. Вывод собирается только после
  того, как процесс точно мёртв.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import desktop

ROOT = Path(__file__).resolve().parent.parent
HEALTH_TIMEOUT = 15.0
HEALTH_POLL = 0.1
TOKEN_TIMEOUT = 3.0
#: Windows: 1 от TerminateProcess; 0 — процесс успел выйти сам; -15 — SIGTERM (POSIX).
ACCEPTABLE_EXIT_CODES = (0, 1, -15)


def _read_dead(process: "subprocess.Popen[str]") -> str:
    """stdout+stderr процесса, который уже завершён (иначе read() — вечное ожидание)."""
    if process.poll() is None:
        return "<процесс ещё жив — вывод не читаем, чтобы не зависнуть>"
    chunks = []
    for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        if stream is None:
            continue
        try:
            chunks.append(f"--- {label} ---\n{stream.read() or ''}")
        except ValueError:
            chunks.append(f"--- {label} ---\n<поток закрыт>")
    return "".join(chunks)


@pytest.mark.slow
def test_desktop_browser_smoke(tmp_path: Path) -> None:
    """Запуск, health, token, terminate: процесс выходит, трейсбека нет."""
    port = desktop.free_port()
    data_dir = tmp_path / "data"
    env = {
        **os.environ,
        "GAPAUDIT_DATA_DIR": str(data_dir),
        desktop.NO_BROWSER_ENV: "1",
    }
    process = subprocess.Popen(  # noqa: S603 - argv из констант и sys.executable
        [sys.executable, "-u", str(ROOT / "desktop.py"), "--browser", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    healthy = False
    token_written = False
    forbidden_status: int | None = None
    returncode: int | None = None
    try:
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.0)
                if response.status_code == 200 and response.json().get("ok"):
                    healthy = True
                    break
            except Exception:  # noqa: BLE001 - сервер ещё поднимается
                pass
            time.sleep(HEALTH_POLL)

        if healthy:
            # шаг 5 desktop.py пишет токен сразу после своего health — ждём опросом
            token_deadline = time.monotonic() + TOKEN_TIMEOUT
            while time.monotonic() < token_deadline:
                if (data_dir / "session.token").exists():
                    token_written = True
                    break
                time.sleep(0.05)
            # бэкенд отвечает «по-настоящему»: /api/* без токена — 403
            forbidden_status = httpx.get(f"http://127.0.0.1:{port}/api/jobs").status_code

        if process.poll() is None:
            process.terminate()
            try:
                returncode = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    output = _read_dead(process)
    assert healthy, f"/api/health не ответил за {HEALTH_TIMEOUT} с\n{output}"
    assert token_written, f"session.token не появился за {TOKEN_TIMEOUT} с\n{output}"
    assert forbidden_status == 403, f"ожидался 403 без токена: {forbidden_status}\n{output}"
    assert returncode in ACCEPTABLE_EXIT_CODES, f"exit={returncode}\n{output}"
    assert "Traceback" not in output, output
    assert "ОШИБКА" not in output, output
