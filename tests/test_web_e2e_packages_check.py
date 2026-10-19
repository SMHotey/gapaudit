"""Сквозной контур P1: POST /api/jobs → реальный CLI → SSE → GET /api/jobs/{id}.

Шаг 3d-a. Цепочка без подмен на среднем звене::

    POST /api/jobs {"kind": "packages.check", "params": {"names": ["requests"]}}
      → реальный JobQueue → _default_runner → subprocess
        python -m cli.check_predecessors --job-json --json-out=...
      → stdout JSON-lines → SSE started/progress/done → Store: status=done

Сеть — ноль: в окружении подпроцесса (и процесса теста) стоит
``GAPAUDIT_FAKE_HTTP=1``, ответы берутся из ``tests/fixtures/http`` (AGENTS
gotcha 30). Фикстура ``conftest.app`` здесь намеренно НЕ используется: она
подменяет ``JobQueue.start``, а этот тест проверяет именно исполнение задачи.

``@pytest.mark.slow`` — один реальный ``Popen`` (быстрый цикл без подпроцессов,
AGENTS gotcha 23). Два остальных теста — защита API и __default_runner__ без
подпроцессов, поэтому идут в быстром цикле.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import web as web_module
from app.web import create_app
from tests.conftest import HOST, PORT, TOKEN, GhProbe, app_state, auth

HEARTBEAT = 0.2  # вместо реальных 15 с: поток обязан идти и без событий
DEADLINE = 60.0  # потолок на весь e2e: зависшая задача — обрыв и красный


@pytest.fixture()
def real_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Приложение с НАСТОЯЩЕЙ очередью и настоящим ``_default_runner``.

    ``runner=None`` — то есть ровно production-путь: argv собирается
    whitelist-таблицей ``app/jobs.py``. Fake HTTP виден и подпроцессу: он
    наследует окружение теста.
    """
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GAPAUDIT_FAKE_HTTP", "1")
    monkeypatch.setattr(web_module, "SSE_HEARTBEAT_INTERVAL", HEARTBEAT)
    application = create_app(
        token=TOKEN, port=PORT, runner=None, gh_check=GhProbe().check
    )
    with TestClient(application, base_url=f"http://{HOST}") as test_client:
        yield test_client


@pytest.mark.slow
def test_packages_check_end_to_end(real_client: TestClient, tmp_path: Path) -> None:
    """POST → CLI → SSE(started/progress/done) → задача в Store со статусом done."""
    posted: list[Any] = []
    events: list[str] = []

    def post_job() -> None:
        # отдельный поток: чтение SSE блокирующее, POST должен пройти «внутри» потока
        posted.append(
            real_client.post(
                "/api/jobs",
                json={"kind": "packages.check", "params": {"names": ["requests"]}},
                headers=auth(),
            )
        )

    with real_client.stream("GET", f"/api/stream?token={TOKEN}") as stream:
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
        poster.join(timeout=10.0)

    assert not poster.is_alive(), "POST не дошёл до сервера"
    assert posted and posted[0].status_code == 202, posted
    job_id = posted[0].json()["job_id"]

    assert "started" in events, events
    assert "progress" in events, events
    assert "done" in events, events
    assert "failed" not in events, events

    # Store — источник правды: задача действительно done
    row = real_client.get(f"/api/jobs/{job_id}", headers=auth()).json()
    assert row["status"] == "done", row
    assert row["progress"] == 1.0, row
    assert row["kind"] == "packages.check"

    # CLI отработал на самом деле: --json-out из _default_runner создал файл
    result_path = tmp_path / "data" / "jobs" / f"{job_id}.result.json"
    assert result_path.is_file(), result_path
    rows = json.loads(result_path.read_text(encoding="utf-8"))
    assert [item["name"] for item in rows] == ["requests"]

    # протокол прочитан очередью, а не свален в лог мусором
    log_path = tmp_path / "data" / "jobs" / f"{job_id}.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    assert '"type": "progress"' not in log_text


def test_post_evil_kind_is_rejected_before_runner(real_client: TestClient) -> None:
    """Чужой ``kind`` отбивает API (400) — до очереди и до ``_default_runner``."""
    response = real_client.post(
        "/api/jobs", json={"kind": "evil; rm -rf /", "params": {}}, headers=auth()
    )

    assert response.status_code == 400
    assert real_client.get("/api/jobs", headers=auth()).json()["jobs"] == []


def test_unknown_param_is_rejected_by_api_and_fails_at_queue(real_client: TestClient) -> None:
    """Неизвестный ключ: API отвечает 400, а очередь (в обход API) — failed.

    Два слоя защиты одной правды: ``app/web.py`` проверяет ключи поверхностно и
    отбивает запрос до создания задачи, а ``_params_to_argv`` в
    ``_default_runner`` бросает ``ValueError`` — задача становится ``failed`` с
    понятной причиной, а не «молча ничего не делает».
    """
    response = real_client.post(
        "/api/jobs",
        json={"kind": "packages.check", "params": {"names": ["x"], "unknown": 1}},
        headers=auth(),
    )
    assert response.status_code == 400
    assert "unknown" in response.json()["detail"]
    assert real_client.get("/api/jobs", headers=auth()).json()["jobs"] == []

    queue = app_state(real_client).queue
    job_id = queue.submit("packages.check", {"names": ["x"], "unknown": 1})
    deadline = time.monotonic() + 20.0
    row: dict[str, Any] = {}
    while time.monotonic() < deadline:
        row = real_client.get(f"/api/jobs/{job_id}", headers=auth()).json()
        if row["status"] in ("failed", "done", "cancelled"):
            break
        time.sleep(0.05)

    assert row["status"] == "failed", row
    assert "unknown param for packages.check: unknown" in (row["error"] or ""), row
