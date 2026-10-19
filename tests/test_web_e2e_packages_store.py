"""Сквозной контур P1 закрыт: POST → CLI → Store → ``GET /api/packages/{name}``.

Шаг 3d-b. Цепочка целиком, без подмен на среднем звене::

    POST /api/jobs {"kind": "packages.check", "params": {"names": ["requests"]}}
      → JobQueue → _default_runner → subprocess
        python -m cli.check_predecessors --job-json --json-out=<data>/jobs/<id>.result.json
      → событие done → lifespan-хук → Store.record_observations(run_id=job_id)
      → GET /api/packages/requests: observations непусты, run_id = id задачи

Сеть — ноль: ``GAPAUDIT_FAKE_HTTP=1`` в окружении теста (его наследует
подпроцесс), ответы — из ``tests/fixtures/http`` (AGENTS gotcha 30).

``@pytest.mark.slow`` — реальный ``Popen`` (AGENTS gotcha 23).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.web import create_app
from tests.conftest import HOST, PORT, TOKEN, GhProbe, auth

#: Потолки ожидания: задача (подпроцесс) и последующая запись в Store (пул).
JOB_TIMEOUT = 60.0
STORE_TIMEOUT = 10.0
POLL = 0.05

#: Значения из фикстуры ``tests/fixtures/http/pypistats_requests.json``.
ADOPTION_WEEK = 317314822
ADOPTION_MONTH = 1202777296


@pytest.fixture()
def real_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Приложение с настоящей очередью и настоящим ``_default_runner``."""
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GAPAUDIT_FAKE_HTTP", "1")
    application = create_app(
        token=TOKEN, port=PORT, runner=None, gh_check=GhProbe().check
    )
    with TestClient(application, base_url=f"http://{HOST}") as test_client:
        yield test_client


def wait_status(client: TestClient, job_id: str, expected: str, timeout: float) -> str:
    """Дождаться терминального статуса задачи; вернуть последний увиденный."""
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = client.get(f"/api/jobs/{job_id}", headers=auth()).json()["status"]
        if status in ("done", "failed", "cancelled"):
            return status
        time.sleep(POLL)
    return status


@pytest.mark.slow
def test_packages_check_records_observations(
    real_client: TestClient, tmp_path: Path
) -> None:
    """Задача done → observations в Store → карточка пакета их отдаёт."""
    posted = real_client.post(
        "/api/jobs",
        json={"kind": "packages.check", "params": {"names": ["requests"]}},
        headers=auth(),
    )
    assert posted.status_code == 202, posted.text
    job_id = posted.json()["job_id"]

    assert wait_status(real_client, job_id, "done", JOB_TIMEOUT) == "done"

    # CLI отработал: файл, который он записал по --json-out
    result_path = tmp_path / "data" / "jobs" / f"{job_id}.result.json"
    assert result_path.is_file(), result_path
    rows = json.loads(result_path.read_text(encoding="utf-8"))
    assert [row["name"] for row in rows] == ["requests"]

    # хук lifespan записал наблюдения (асинхронно, в пуле)
    deadline = time.monotonic() + STORE_TIMEOUT
    body: dict = {}
    while time.monotonic() < deadline:
        response = real_client.get("/api/packages/requests", headers=auth())
        # до первой записи пакета нет — 404 это нормальный шаг ожидания
        if response.status_code == 200 and response.json()["observations"]:
            body = response.json()
            break
        time.sleep(POLL)
    assert body, "наблюдения не появились в Store"

    assert body["package"] is not None
    observations = body["observations"]
    assert len(observations) >= 1
    first = observations[0]
    assert first["name"] == "requests"
    assert first["run_id"] == job_id, first
    assert first["adoption_week"] == ADOPTION_WEEK
    assert first["adoption_month"] == ADOPTION_MONTH
    assert first["verdict"] == "ЖИВО"
    assert first["flag_value"] == 0  # у requests нет MCP-признака
    assert first["measured_at"]

    # история того же имени видит ту же запись
    history = real_client.get("/api/packages/requests/history", headers=auth()).json()
    assert [row["run_id"] for row in history["observations"]] == [job_id]
