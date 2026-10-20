"""``POST /api/niche/find`` и ``GET /api/niche/result/{job_id}`` (шаг P5f-1).

Экран «Поиск ниши» — это два эндпоинта поверх задачи ``niche.find``:
постановка (202) и чтение артефакта задачи. Тесты держат три вещи, которые
ломаются тихо:

* валидацию тела до постановки задачи (400, а не ``failed`` через секунду):
  обязательный ``query``, границы ``pages``/``limit``, наборы ``sources`` и
  ``signal_kinds``;
* коды ответа чтения: 404 — задачи нет, 202 — ещё идёт (с ``status`` и
  ``progress``), 200 — готовый ``result.json``, 500 — задача ``done``, а файла
  нет (баг задачи, а не «ничего не нашли»);
* согласие слоёв: наборы ``sources``/``signal_kinds`` в ``app/web.py`` и
  ``cli/niche.py`` обязаны совпадать (иначе API примет задачу, которую
  подпроцесс убьёт argparse'ом — gotcha 54).

Сеть — ноль, подпроцессы — ноль: задача создаётся прямо в Store, ``result.json``
пишется в каталог данных теста, как его писал бы CLI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.store import utcnow_iso
from cli import niche as niche_cli
from app import web as web_module
from tests.conftest import app_state, auth

JOB_KIND = "niche.find"


def create_job(
    client: TestClient,
    job_id: str,
    *,
    status: str = "queued",
    progress: float = 0.0,
    message: str = "",
    error: str | None = None,
) -> None:
    """Положить задачу в Store так, как это делает ``JobQueue.submit``."""
    store = app_state(client).store
    store.create_job(
        {
            "id": job_id,
            "kind": JOB_KIND,
            "params": {"query": "test impact analysis"},
            "status": status,
            "progress": progress,
            "message": message,
            "created_at": utcnow_iso(),
            "log_path": str(Path(app_state(client).data_dir) / "jobs" / f"{job_id}.log"),
        }
    )
    if status != "queued" or progress or message or error:
        fields: dict[str, Any] = {"status": status, "progress": progress}
        if message:
            fields["message"] = message
        if error:
            fields["error"] = error
        store.update_job(job_id, **fields)


def write_result(client: TestClient, job_id: str, payload: dict[str, Any]) -> Path:
    """Записать ``result.json`` задачи — так его пишет ``cli/niche.py``."""
    path = Path(app_state(client).data_dir) / "jobs" / f"{job_id}.result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


RESULT = {
    "query": "test impact analysis",
    "items": [
        {
            "name": "pytest-testmon",
            "gap_score": 87.0,
            "momentum_30d": None,
            "flag": False,
            "adoption_week": 21000,
            "verdict": "ЖИВО",
            "funding_total": None,
            "patents_impulse_pct": None,
            "jobs_total": None,
            "search_interest_trend": None,
            "sources": ["github", "npm"],
            "components_available": 3,
        }
    ],
    "stats": {"total": 1, "found": 1, "with_gap": 1, "signals_used": []},
}


# --- POST /api/niche/find ------------------------------------------------------


def test_niche_find_returns_202_and_creates_job(client: TestClient) -> None:
    """Валидное тело — 202 с job_id; задача в Store с kind niche.find."""
    response = client.post(
        "/api/niche/find",
        json={
            "query": "test impact analysis",
            "min_stars": 20,
            "pages": 1,
            "sources": ["github", "npm"],
            "signal_kinds": ["jobs", "patents"],
            "limit": 10,
        },
        headers=auth(),
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    job_id = body["job_id"]
    assert len(job_id) == 32

    job = app_state(client).store.get_job(job_id)
    assert job is not None
    assert job["kind"] == JOB_KIND
    assert job["status"] == "queued"
    assert job["params"] == {
        "query": "test impact analysis",
        "min_stars": 20,
        "pages": 1,
        "sources": ["github", "npm"],
        "signal_kinds": ["jobs", "patents"],
        "limit": 10,
    }


def test_niche_find_minimal_body_uses_defaults(client: TestClient) -> None:
    """Только ``query`` — задача создаётся: остальные поля берут дефолты CLI."""
    response = client.post(
        "/api/niche/find", json={"query": "vector database"}, headers=auth()
    )

    assert response.status_code == 202
    params = app_state(client).store.get_job(response.json()["job_id"])["params"]
    assert params == {"query": "vector database"}


def test_niche_find_without_query_is_400(client: TestClient) -> None:
    """Пустой/отсутствующий query — 400 до постановки задачи (fail fast)."""
    for body in ({}, {"query": ""}, {"query": "   "}, {"min_stars": 20}):
        response = client.post("/api/niche/find", json=body, headers=auth())
        assert response.status_code == 400, body
        assert "query" in response.json()["detail"]


def test_niche_find_unknown_source_is_400(client: TestClient) -> None:
    """``sources=["evil"]`` — 400: набор источников знает только этот эндпоинт."""
    response = client.post(
        "/api/niche/find",
        json={"query": "x", "sources": ["github", "evil"]},
        headers=auth(),
    )

    assert response.status_code == 400
    assert "sources" in response.json()["detail"]


def test_niche_find_unknown_signal_kind_is_400(client: TestClient) -> None:
    """``signal_kinds=["evil"]`` — 400, а не задача, умирающая в argparse."""
    response = client.post(
        "/api/niche/find",
        json={"query": "x", "signal_kinds": ["jobs", "evil"]},
        headers=auth(),
    )

    assert response.status_code == 400
    assert "signal_kinds" in response.json()["detail"]


def test_niche_find_out_of_range_limits_are_400(client: TestClient) -> None:
    """Границы: pages 1..10, limit 1..500, min_stars >= 0 — 400 с именем поля."""
    for body, field in (
        ({"query": "x", "pages": 0}, "pages"),
        ({"query": "x", "pages": 11}, "pages"),
        ({"query": "x", "limit": 0}, "limit"),
        ({"query": "x", "limit": 501}, "limit"),
        ({"query": "x", "min_stars": -1}, "min_stars"),
    ):
        response = client.post("/api/niche/find", json=body, headers=auth())
        assert response.status_code == 400, body
        assert field in response.json()["detail"]


def test_niche_find_wrong_type_is_400(client: TestClient) -> None:
    """Типы проверяет таблица ``app/jobs.py``: ``pages: "2"`` — 400."""
    response = client.post(
        "/api/niche/find", json={"query": "x", "pages": "2"}, headers=auth()
    )

    assert response.status_code == 400
    assert "pages" in response.json()["detail"]


# --- GET /api/niche/result/{job_id} -------------------------------------------


def test_niche_result_unknown_job_is_404(client: TestClient) -> None:
    """Задачи нет — 404 (а не пустой результат)."""
    response = client.get("/api/niche/result/unknown", headers=auth())

    assert response.status_code == 404


def test_niche_result_running_job_is_202_with_progress(client: TestClient) -> None:
    """Задача идёт — 202 со статусом и прогрессом: экран поллит этот же адрес."""
    create_job(client, "r" * 32, status="running", progress=0.42,
               message="packages.check")

    response = client.get(f"/api/niche/result/{'r' * 32}", headers=auth())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert body["progress"] == 0.42
    assert body["message"] == "packages.check"
    assert body["error"] is None


def test_niche_result_failed_job_is_202_with_error(client: TestClient) -> None:
    """``failed`` — тоже 202, но с ``error``: экран покажет причину и остановится.

    Код 202 здесь сохранён сознательно (контракт шага: «status != done →
    202»): отличать «идёт» от «упало» должен клиент по полю ``status``, а не
    по коду ответа. ``error`` — чтобы причина не потерялась.
    """
    create_job(client, "f" * 32, status="failed", progress=0.5,
               message="упала", error="exit code 2")

    response = client.get(f"/api/niche/result/{'f' * 32}", headers=auth())

    assert response.status_code == 202
    assert response.json() == {
        "status": "failed",
        "progress": 0.5,
        "message": "упала",
        "error": "exit code 2",
    }


def test_niche_result_done_job_returns_result_json(client: TestClient) -> None:
    """``done`` + файл — 200 с содержимым result.json как есть."""
    create_job(client, "d" * 32, status="done", progress=1.0, message="готово")
    write_result(client, "d" * 32, RESULT)

    response = client.get(f"/api/niche/result/{'d' * 32}", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert body == RESULT
    assert body["items"][0]["gap_score"] == 87.0
    assert body["stats"]["with_gap"] == 1
    assert "status" not in body


def test_niche_result_done_without_file_is_500(client: TestClient) -> None:
    """``done`` без result.json — 500: это баг задачи, а не пустой результат."""
    create_job(client, "m" * 32, status="done", progress=1.0)

    response = client.get(f"/api/niche/result/{'m' * 32}", headers=auth())

    assert response.status_code == 500
    assert "result.json" in response.json()["detail"]


def test_niche_result_broken_json_is_500(client: TestClient) -> None:
    """Битый файл — 500 с внятным текстом, а не 500 от необработанного исключения."""
    create_job(client, "b" * 32, status="done", progress=1.0)
    path = Path(app_state(client).data_dir) / "jobs" / f"{'b' * 32}.result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{не json", encoding="utf-8")

    response = client.get(f"/api/niche/result/{'b' * 32}", headers=auth())

    assert response.status_code == 500
    assert "не читается" in response.json()["detail"]


# --- согласие слоёв -----------------------------------------------------------


def test_niche_kind_is_registered_in_jobs_tables() -> None:
    """``niche.find`` зарегистрирован во всех четырёх таблицах ``app/jobs.py``.

    Четыре места (gotcha 54): модуль, whitelist ключей, обязательные ключи и
    типы. Пропуск любого означает задачу, которую API примет, а runner
    отправит в ``python -m cli.niche`` без валидации.
    """
    from app.jobs import (
        KIND_PARAM_KEYS,
        KIND_PARAM_TYPES,
        KIND_REQUIRED_KEYS,
        _KIND_TO_MODULE,
    )

    assert _KIND_TO_MODULE[JOB_KIND] == "cli.niche"
    assert KIND_PARAM_KEYS[JOB_KIND] == frozenset(
        {"query", "min_stars", "pages", "sources", "signal_kinds", "limit"}
    )
    assert KIND_REQUIRED_KEYS[JOB_KIND] == frozenset({"query"})
    assert set(KIND_PARAM_TYPES[JOB_KIND]) == set(KIND_PARAM_KEYS[JOB_KIND])


def test_niche_sets_agree_between_web_and_cli() -> None:
    """Наборы ``sources``/``signal_kinds`` у API и CLI совпадают (gotcha 54)."""
    assert set(web_module.NICHE_SOURCES) == set(niche_cli.SOURCES)
    assert set(web_module.NICHE_SIGNAL_KINDS) == set(niche_cli.NICHE_SIGNAL_KINDS)
    assert web_module.NICHE_SIGNAL_KINDS == web_module.SIGNAL_COLLECT_KINDS
