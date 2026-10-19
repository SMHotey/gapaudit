"""``/api/jobs`` (шаг 3b): создание, список, карточка, отмена, лог.

Очередь в этих тестах — настоящая ``JobQueue`` из lifespan, но воркеры не
поднимаются (conftest), поэтому статус только что отправленной задачи
предсказуем: ``queued``. Поведение воркеров покрыто ``tests/test_jobs.py``.

Сеть — ноль; подпроцессы — ноль.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.jobs import JobQueue
from app.store import Store
from app.web import LOG_TAIL_BYTES
from tests.conftest import app_state, auth

HEX32 = re.compile(r"^[0-9a-f]{32}$")

VALID_JOB = {"kind": "monitor.diff", "params": {"diff_from": "raw/predecessors.json"}}


# --- lifespan: Store и очередь подключены к FastAPI --------------------------


def test_lifespan_opens_store_and_starts_queue(client: TestClient) -> None:
    state = app_state(client)

    assert isinstance(state.store, Store)
    assert state.store.ping() is True
    assert isinstance(state.queue, JobQueue)
    # lifespan вызвал queue.start() ровно один раз (conftest фиксирует вызов)
    assert state.jobs_started == [state.queue]
    assert state.data_dir.endswith("data")


# --- POST /api/jobs ----------------------------------------------------------


def test_post_valid_job_returns_202(client: TestClient) -> None:
    response = client.post("/api/jobs", json=VALID_JOB, headers=auth())

    assert response.status_code == 202
    payload = response.json()
    assert HEX32.match(payload["job_id"]), payload
    assert payload["status"] == "queued"

    jobs = client.get("/api/jobs", headers=auth()).json()["jobs"]
    assert [job["id"] for job in jobs] == [payload["job_id"]]


def test_post_unknown_kind_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"kind": "nuke.launch", "params": {}}, headers=auth()
    )

    assert response.status_code == 400
    assert "nuke.launch" in response.json()["detail"]


def test_post_params_not_dict_is_422(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"kind": "monitor.diff", "params": [1, 2]}, headers=auth()
    )

    assert response.status_code == 422


def test_post_without_kind_is_422(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"params": {}}, headers=auth())

    assert response.status_code == 422


def test_post_unknown_param_key_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={"kind": "monitor.diff", "params": {"diff_from": "raw/prev.json", "depth": 3}},
        headers=auth(),
    )

    assert response.status_code == 400
    assert "depth" in response.json()["detail"]


def test_post_unknown_param_days_is_400(client: TestClient) -> None:
    """Fail fast: ``days`` — не параметр задачи (сравниваются два списка), 400 сразу."""
    response = client.post(
        "/api/jobs", json={"kind": "monitor.diff", "params": {"days": 7}}, headers=auth()
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "days" in detail and "обязательный" in detail


def test_post_monitor_diff_without_diff_from_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"kind": "monitor.diff", "params": {}}, headers=auth()
    )

    assert response.status_code == 400
    assert "diff_from" in response.json()["detail"]


def test_post_packages_check_without_names_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"kind": "packages.check", "params": {"keyword": ["mcp"]}},
        headers=auth(),
    )

    assert response.status_code == 400
    assert "names" in response.json()["detail"]


def test_post_registry_scan_with_classes_is_400(client: TestClient) -> None:
    """``classes`` — настройка метода (core.scan.DEFAULT_CLASSES), не параметр задачи."""
    response = client.post(
        "/api/jobs",
        json={"kind": "registry.scan", "params": {"classes": ["devtools"]}},
        headers=auth(),
    )

    assert response.status_code == 400
    assert "classes" in response.json()["detail"]


def test_post_param_wrong_type_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/jobs",
        json={"kind": "monitor.diff", "params": {"diff_from": 7}},
        headers=auth(),
    )

    assert response.status_code == 400
    assert "diff_from" in response.json()["detail"]

    # bool — не int для наших целей
    boolean = client.post(
        "/api/jobs",
        json={"kind": "registry.scan", "params": {"limit": True}},
        headers=auth(),
    )
    assert boolean.status_code == 400


# --- GET /api/jobs -----------------------------------------------------------


def test_list_shows_public_fields_only(client: TestClient) -> None:
    client.post("/api/jobs", json=VALID_JOB, headers=auth())

    jobs = client.get("/api/jobs", headers=auth()).json()["jobs"]

    assert len(jobs) == 1
    assert set(jobs[0]) == {
        "id",
        "kind",
        "status",
        "progress",
        "message",
        "created_at",
        "started_at",
        "finished_at",
        "error",
    }
    assert "log_path" not in jobs[0] and "pid" not in jobs[0]


def test_list_filters_by_status_and_limit(client: TestClient) -> None:
    first = client.post("/api/jobs", json=VALID_JOB, headers=auth()).json()["job_id"]
    second = client.post("/api/jobs", json=VALID_JOB, headers=auth()).json()["job_id"]

    everything = client.get("/api/jobs", headers=auth()).json()["jobs"]
    queued = client.get("/api/jobs?status=queued", headers=auth()).json()["jobs"]
    none = client.get("/api/jobs?status=failed", headers=auth()).json()["jobs"]
    limited = client.get("/api/jobs?limit=1", headers=auth()).json()["jobs"]

    assert {job["id"] for job in everything} == {first, second}
    assert {job["id"] for job in queued} == {first, second}
    assert none == []
    assert len(limited) == 1


# --- GET /api/jobs/{id} ------------------------------------------------------


def test_get_job_with_params_and_404(client: TestClient) -> None:
    job_id = client.post("/api/jobs", json=VALID_JOB, headers=auth()).json()["job_id"]

    found = client.get(f"/api/jobs/{job_id}", headers=auth())
    missing = client.get("/api/jobs/" + "0" * 32, headers=auth())

    assert found.status_code == 200
    payload = found.json()
    assert payload["id"] == job_id
    assert payload["params"] == {"diff_from": "raw/predecessors.json"}
    assert "log_path" not in payload and "pid" not in payload
    assert missing.status_code == 404


# --- POST /api/jobs/{id}/cancel ----------------------------------------------


def test_cancel_queued_job(client: TestClient) -> None:
    job_id = client.post("/api/jobs", json=VALID_JOB, headers=auth()).json()["job_id"]

    response = client.post(f"/api/jobs/{job_id}/cancel", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"cancelled": True}
    assert client.get(f"/api/jobs/{job_id}", headers=auth()).json()["status"] == "cancelled"


def test_cancel_finished_job_returns_false(client: TestClient) -> None:
    store = app_state(client).store
    done_id = "d" * 32
    store.create_job(
        {
            "id": done_id,
            "kind": "monitor.diff",
            "params": {"diff_from": "raw/prev.json"},
            "status": "queued",
            "created_at": "2026-10-19T00:00:00Z",
            "log_path": "",
        }
    )
    store.update_job(done_id, status="done", finished_at="2026-10-19T00:01:00Z")

    response = client.post(f"/api/jobs/{done_id}/cancel", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"cancelled": False}


def test_cancel_unknown_job_is_404(client: TestClient) -> None:
    response = client.post(f"/api/jobs/{'f' * 32}/cancel", headers=auth())

    assert response.status_code == 404


# --- GET /api/jobs/{id}/log --------------------------------------------------


def test_log_returns_tail_of_10000_bytes(client: TestClient) -> None:
    job_id = client.post("/api/jobs", json=VALID_JOB, headers=auth()).json()["job_id"]
    # log_path в публичной проекции задачи намеренно нет — путь к файлу
    # остаётся внутри приложения, клиенту он не нужен.
    job = app_state(client).queue.get(job_id)
    assert job is not None and job.log_path
    log_file = Path(job.log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    content = ("x" * 15_000 + "КОНЕЦ").encode("utf-8")
    log_file.write_bytes(content)

    response = client.get(f"/api/jobs/{job_id}/log", headers=auth())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.content
    assert len(body) == LOG_TAIL_BYTES
    assert body.endswith("КОНЕЦ".encode("utf-8"))
    assert body == content[-LOG_TAIL_BYTES:]


def test_log_for_unknown_job_is_404(client: TestClient) -> None:
    response = client.get(f"/api/jobs/{'e' * 32}/log", headers=auth())

    assert response.status_code == 404


def test_log_without_file_is_empty_200(client: TestClient) -> None:
    job_id = client.post("/api/jobs", json=VALID_JOB, headers=auth()).json()["job_id"]

    response = client.get(f"/api/jobs/{job_id}/log", headers=auth())

    assert response.status_code == 200
    assert response.content == b""
