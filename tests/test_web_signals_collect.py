"""``POST /api/signals/collect`` и ``jobs`` в ``GET /api/signals/{package}`` (P5a).

Что проверяется:

* эндпоинт 202 + задача ``signals.collect`` в Store, валидация 400
  (пустой package, неизвестный вид — fail fast до создания задачи);
* хук lifespan: ``done`` + result.json ``{"package", "signals"}`` →
  ``Store.record_signal_observation`` (value=total, detail=блок);
* ``GET /api/signals/{package}``: три состояния jobs («собран» / «ключ не
  настроен» / «ещё не собирали») видны по ``jobs`` + ``signals_status`` и
  по ``signals_available``/``signals_unavailable``;
* «согласие слоёв» (gotcha 54): ``app/web.SIGNAL_COLLECT_KINDS`` ==
  ``cli.signals.SIGNAL_KINDS`` — иначе API ответил бы 202 задаче,
  которая умирает argparse'ом в подпроцессе.

Сеть — ноль, подпроцессы — ноль (conftest: воркеры не поднимаются, runner
фейковый; воркер с реальным cli.signals закрыт бы slow-тестом, здесь хук
проверяется подачей события ``done`` напрямую — паттерн test_web_lifespan_hooks).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.web import SIGNAL_COLLECT_KINDS
from cli.signals import SIGNAL_KINDS
from tests.conftest import app_state, auth

#: Наблюдение jobs, как его пишет хук lifespan из result.json cli.signals.
JOBS_DETAIL: dict[str, Any] = {
    "total": 212,
    "by_country": [
        {"country": "de", "count": 200, "label": "Germany"},
        {"country": "it", "count": 12, "label": "Italy"},
    ],
    "configured": True,
    "error": None,
}


def observation(name: str = "requests", week: int = 100) -> dict[str, Any]:
    """Строка вида ``core.packages.check()`` — столько нужно ``Store``."""
    return {
        "name": name,
        "src": "pypi",
        "ver": "1.0.0",
        "date": "2026-10-01",
        "summary": f"описание {name}",
        "home": f"https://example.invalid/{name}",
        "flag": 0,
        "month": week * 4,
        "week": week,
        "verdict": "живой",
    }


def seed_package(client: TestClient, name: str = "requests") -> None:
    """Завести пакет с одним замером — чтобы /api/signals не отвечал 404."""
    app_state(client).store.record_observations(
        [observation(name)], run_id="seed"
    )


def wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    """Дождаться ``predicate`` (запись сигнала идёт в потоке пула)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def collect(client: TestClient, payload: dict[str, Any]):
    """POST /api/signals/collect с токеном."""
    return client.post("/api/signals/collect", json=payload, headers=auth())


def signals(client: TestClient, name: str = "requests") -> dict[str, Any]:
    response = client.get(f"/api/signals/{name}", headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


def finish_with_result(
    client: TestClient, payload: Any, kind: str = "signals.collect",
    params: dict[str, Any] | None = None,
) -> str:
    """Поставить задачу, написать result.json, подать событие done (как хук)."""
    state = app_state(client)
    job_id = state.queue.submit(
        kind, params or {"package": "requests", "kinds": ["jobs"]}
    )
    path = state.queue.jobs_dir / f"{job_id}.result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    state.queue._emit(job_id, "done", {"status": "done"})
    return job_id


# --- POST /api/signals/collect ------------------------------------------------


def test_collect_returns_202_and_creates_job(client: TestClient) -> None:
    """202 {job_id, status}; задача kind=signals.collect лежит в Store."""
    response = collect(client, {"package": "requests", "kinds": ["jobs"]})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    job = app_state(client).queue.get(body["job_id"])
    assert job is not None
    assert job.kind == "signals.collect"
    assert job.params == {"package": "requests", "kinds": ["jobs"]}


def test_collect_without_kinds_defaults_to_jobs(client: TestClient) -> None:
    """kinds необязателен: CLI соберёт всё, что умеет (P5a — jobs)."""
    response = collect(client, {"package": "requests"})

    assert response.status_code == 202, response.text
    job = app_state(client).queue.get(response.json()["job_id"])
    assert job is not None
    assert job.params == {"package": "requests"}


def test_collect_without_package_is_400(client: TestClient) -> None:
    """Пустой package — 400 (та же _param_errors, что у POST /api/jobs)."""
    response = collect(client, {"kinds": ["jobs"]})

    assert response.status_code == 400
    assert "package" in response.json()["detail"]


def test_collect_with_unknown_kind_is_400(client: TestClient) -> None:
    """kinds=["evil"] — 400 до создания задачи: в подпроцессе её ждёт argparse."""
    response = collect(client, {"package": "requests", "kinds": ["evil"]})

    assert response.status_code == 400
    assert "неизвестный вид сигнала" in response.json()["detail"]
    assert app_state(client).queue.list() == []


def test_collect_with_wrong_kinds_type_is_400(client: TestClient) -> None:
    """kinds строкой, а не списком — 400 по таблице типов (KIND_PARAM_TYPES)."""
    response = collect(client, {"package": "requests", "kinds": "jobs"})

    assert response.status_code == 400


def test_web_and_cli_agree_on_signal_kinds() -> None:
    """«Согласие слоёв» (gotcha 54): наборы видов в web и cli совпадают."""
    assert SIGNAL_COLLECT_KINDS == frozenset(SIGNAL_KINDS)


# --- хук lifespan: done -> signal_observations --------------------------------


def test_done_records_signal_observation(client: TestClient) -> None:
    """``done`` + result.json → наблюдение jobs: value=total, detail=блок."""
    state = app_state(client)
    job_id = finish_with_result(
        client, {"package": "requests", "signals": {"jobs": JOBS_DETAIL}}
    )

    assert wait_for(
        lambda: state.store.get_signal_observations("requests", signal_kind="jobs")
    )
    rows = state.store.get_signal_observations("requests", signal_kind="jobs")
    assert len(rows) == 1
    row = rows[0]
    assert row["value"] == 212.0
    assert row["detail"]["by_country"] == JOBS_DETAIL["by_country"]


def test_done_for_other_kind_records_nothing(
    client: TestClient, monkeypatch
) -> None:
    """``done`` для packages.check с «сигнальным» файлом — не пишет сигналов."""
    from tests.test_web_lifespan_hooks import ROW

    state = app_state(client)
    job_id, result_path = _submit(client, "packages.check", {"names": ["requests"]})
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps([ROW]), encoding="utf-8")
    calls: list[str] = []
    real = state.store.record_signal_observation

    def spy(**kwargs: Any) -> int:
        calls.append(str(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(state.store, "record_signal_observation", spy)
    state.queue._emit(job_id, "done", {"status": "done"})

    time.sleep(0.1)
    assert calls == []


def _submit(
    client: TestClient, kind: str, params: dict[str, Any]
) -> tuple[str, Path]:
    state = app_state(client)
    job_id = state.queue.submit(kind, params)
    return job_id, state.queue.jobs_dir / f"{job_id}.result.json"


# --- GET /api/signals/{package}: три состояния jobs ---------------------------


def test_signals_endpoint_shows_jobs_after_collection(client: TestClient) -> None:
    """Наблюдение configured=True → jobs=detail, статус ok, имя в available."""
    seed_package(client)
    finish_with_result(
        client, {"package": "requests", "signals": {"jobs": JOBS_DETAIL}}
    )
    wait_for(lambda: signals(client)["jobs"] is not None)

    payload = signals(client)

    assert payload["jobs"]["total"] == 212
    assert payload["jobs"]["by_country"][0]["label"] == "Germany"
    assert payload["signals_status"]["jobs"] == "ok"
    assert "jobs" in payload["signals_available"]
    assert "jobs" not in payload["signals_unavailable"]


def test_signals_endpoint_without_observation(client: TestClient) -> None:
    """Наблюдений нет → jobs=None, статус not_collected, имя в unavailable."""
    seed_package(client)

    payload = signals(client)

    assert payload["jobs"] is None
    assert payload["signals_status"]["jobs"] == "not_collected"
    assert "jobs" in payload["signals_unavailable"]


def test_signals_endpoint_not_configured_observation(client: TestClient) -> None:
    """Наблюдение configured=False → jobs=None, статус not_configured.

    Ключ не настроен — не «нет данных вообще»: UI показывает ссылку
    «Настройки → API-ключи», а не кнопку «Собрать» (§4.3 шага).
    """
    seed_package(client)
    state = app_state(client)
    state.store.record_signal_observation(
        package="requests",
        signal_kind="jobs",
        value=0.0,
        detail={"total": 0, "by_country": [], "configured": False, "error": None},
    )

    payload = signals(client)

    assert payload["jobs"] is None
    assert payload["signals_status"]["jobs"] == "not_configured"
    assert "jobs" in payload["signals_unavailable"]
