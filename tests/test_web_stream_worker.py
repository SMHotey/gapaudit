"""Интеграционный тест «воркер → SSE» (шаг 3c, ``@pytest.mark.slow``).

Цепочка целиком, без подмен на каждом звене::

    POST /api/jobs → реальный JobQueue → реальный subprocess (fake_job.py)
                   → GET /api/stream → created/started/progress/done

Фикстура ``conftest.app`` здесь намеренно НЕ используется: она подменяет
``JobQueue.start`` (веб-тесты проверяют HTTP-слой, а не исполнение). Своя
``client_real_queue`` оставляет очередь настоящей.

Сеть — ноль (``TestClient`` ходит по ASGI), подпроцессы — один локальный
Python-скрипт из ``tests/fixtures``.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import web as web_module
from app.web import create_app
from tests.conftest import HOST, PORT, TOKEN, GhProbe, auth

FIXTURE_DIR = Path(__file__).parent / "fixtures"
HEARTBEAT = 0.2  # вместо реальных 15 с: поток обязан идти и без событий
DEADLINE = 20.0  # потолок на весь тест: при зависшей задаче — обрыв и красный


def real_runner(job, data_dir: Path) -> "subprocess.Popen[str]":
    """Реальный ``Popen``: ``fake_job.py`` с полным JSON-lines протоколом.

    Параметры задачи в тесте не используются: POST /api/jobs принимает только
    ``kind``-схему (``names``/``keyword``), а длительность прогона нужна самому
    тесту — 3 шага по 0.2 с, чтобы события успели пройти через поток.
    """
    return subprocess.Popen(
        [sys.executable, str(FIXTURE_DIR / "fake_job.py"), "--steps", "3", "--sleep", "0.2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture()
def client_real_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Приложение с настоящей очередью: воркеры поднимаются, Popen — настоящий."""
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(web_module, "SSE_HEARTBEAT_INTERVAL", HEARTBEAT)
    application = create_app(
        token=TOKEN, port=PORT, runner=real_runner, gh_check=GhProbe().check
    )
    with TestClient(application, base_url=f"http://{HOST}") as test_client:
        yield test_client


@pytest.mark.slow
def test_worker_to_sse_stream(client_real_queue: TestClient) -> None:
    """Задача проходит весь путь и доходит до SSE-клиента событиями."""
    events: list[str] = []
    posted: list[Any] = []

    def post_job() -> None:
        # отдельный поток: чтение потока событий блокирующее (см. test_web_stream)
        posted.append(
            client_real_queue.post(
                "/api/jobs",
                json={"kind": "packages.check", "params": {"names": ["x"]}},
                headers=auth(),
            )
        )

    with client_real_queue.stream("GET", "/api/stream", headers=auth()) as stream:
        assert stream.status_code == 200
        poster = threading.Thread(target=post_job, daemon=True)
        poster.start()
        deadline = time.monotonic() + DEADLINE
        for line in stream.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: ") :])
            if "done" in events or "failed" in events:
                break
            if time.monotonic() > deadline:
                break
        poster.join(timeout=5.0)

    assert not poster.is_alive(), "POST не дошёл до сервера"
    assert posted and posted[0].status_code == 202

    # цепочка: создано → воркер взял → подпроцесс отчитался → завершено
    assert "created" in events, events
    assert "started" in events, events
    assert "progress" in events, events
    assert "done" in events, events

    # и статус в Store (источнике правды) тоже стал done
    job_id = posted[0].json()["job_id"]
    row: dict[str, Any] = {}
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        row = client_real_queue.get(f"/api/jobs/{job_id}", headers=auth()).json()
        if row["status"] == "done":
            break
        time.sleep(0.05)
    assert row["status"] == "done", row
    assert row["progress"] == 1.0, row
