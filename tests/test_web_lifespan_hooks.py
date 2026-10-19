"""Хук записи результатов в lifespan (шаги 3d-b и 3b): ``done`` → Store.

Что проверяется:

* ``done`` для ``kind="packages.check"`` при существующем
  ``<data_dir>/jobs/<id>.result.json`` — строки уходят в
  ``Store.record_observations`` с ``run_id=job_id``;
* ``done`` для ``monitor.diff`` — ``{"events": [...]}`` уходит в
  ``Store.record_events`` (шаг 3b), наблюдений не появляется;
* ``done`` для ``catalog.build`` — ``{"queries","stats","items"}`` уходит в
  ``Store.record_catalog`` + ``record_catalog_items``;
* ``progress`` и отсутствующий result.json записи не дают (отсутствующий —
  ещё и warning в лог);
* порядок выключения: запись, поставленная в пул непосредственно перед выходом
  из lifespan, успевает лечь в БД до ``store.close()``.

Хук — подписчик ``JobQueue.subscribe``, поэтому события подаются тем же путём,
которым их рассылает очередь (``queue._emit``), а очередь здесь настоящая.

Сеть — ноль, подпроцессы — ноль (conftest: воркеры не поднимаются, runner
фейковый). ``slow`` здесь не нужен.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.jobs import JobQueue
from app.store import Store
from app.web import create_app
from tests.conftest import HOST, PORT, TOKEN, app_state, fake_runner

#: Строка результата CLI (``cli/check_predecessors --json-out``) в форме,
#: которую ждёт ``Store.record_observations``.
ROW = {
    "name": "requests",
    "src": "pypi",
    "ver": "2.34.2",
    "date": "2026-05-14",
    "month": 1202777296,
    "week": 317314822,
    "flag": False,
    "verdict": "ЖИВО",
}


def wait_for(predicate: Callable[[], bool], *, timeout: float = 2.0) -> bool:
    """Дождаться ``predicate`` (запись идёт в потоке пула — не мгновенно)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


#: Валидные params на каждый kind хука (то, что примет POST /api/jobs).
PARAMS: dict[str, dict[str, Any]] = {
    "packages.check": {"names": ["requests"]},
    "monitor.diff": {"diff_from": "raw/p.json"},
    "catalog.build": {"phrases": ["test impact analysis"]},
}


def submit(client: TestClient, kind: str = "packages.check") -> tuple[str, Path]:
    """Задача в Store + путь её result.json (файл создаёт вызывающий)."""
    state = app_state(client)
    job_id = state.queue.submit(kind, PARAMS[kind])
    return job_id, state.queue.jobs_dir / f"{job_id}.result.json"


def write_payload(path: Path, payload: Any) -> None:
    """Файл, который в проде пишет CLI по ``--json-out``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_result(path: Path) -> None:
    """Файл наблюдений ``packages.check`` (список строк)."""
    write_payload(path, [ROW])


#: События сравнения — форма ``core.diffing.diff_observations`` и таблицы events.
EVENTS_PAYLOAD: dict[str, Any] = {
    "events": [
        {"kind": "release", "name": "requests", "detail": "2.32.0"},
        {"kind": "flag_appeared", "name": "flask", "detail": "появился MCP"},
    ],
    "observations": [ROW],
}

#: Каталог — форма ``core.catalog.build_catalog`` (её же пишет CLI).
CATALOG_PAYLOAD: dict[str, Any] = {
    "queries": ["test impact analysis"],
    "stats": {"total": 2, "by_source": {"github": 2}},
    "items": [
        {
            "source": "github",
            "id": "owner/one",
            "desc": "TIA tool",
            "stars": 120,
            "lang": "Python",
            "pushed": "2026-10-01",
            "sources": ["github"],
        },
        {
            "source": "github",
            "id": "owner/two",
            "desc": "flaky detector",
            "stars": 40,
            "lang": "Go",
            "pushed": "2025-01-01",
            "sources": ["github", "npm"],
        },
    ],
}


class RecordSpy:
    """Обёртка ``Store.record_observations``: считает вызовы, зовёт настоящий."""

    def __init__(self, store: Store) -> None:
        self.calls: list[str] = []
        self._real = store.record_observations

    def __call__(self, rows: list[dict], *, run_id: str, **kwargs: Any) -> list[int]:
        self.calls.append(run_id)
        return self._real(rows, run_id=run_id, **kwargs)


def test_done_packages_check_records_observations(client: TestClient) -> None:
    """``done`` + result.json → наблюдения в БД, ``run_id`` = id задачи."""
    state = app_state(client)
    job_id, result_path = submit(client)
    write_result(result_path)

    state.queue._emit(job_id, "done", {"status": "done"})

    assert wait_for(lambda: state.store.count_observations() > 0)
    history = state.store.get_observations_history("requests")
    assert len(history) == 1
    row = history[0]
    assert row["run_id"] == job_id
    assert row["adoption_week"] == ROW["week"]
    assert row["flag_value"] == 0
    assert row["verdict"] == ROW["verdict"]
    # пакет заведён upsert'ом — карточка /api/packages/{name} сразу живёт
    assert state.store.get_package("requests") is not None


def test_done_for_other_kind_is_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``monitor.diff`` завершился — наблюдений не пишем: у него свои события.

    Проверка держит разделение двух записей: файл для monitor.diff — это
    события, и путь ``packages.check`` к нему применяться не должен.
    """
    state = app_state(client)
    job_id, result_path = submit(client, kind="monitor.diff")
    write_result(result_path)  # файл есть: отсекает именно kind
    spy = RecordSpy(state.store)
    monkeypatch.setattr(state.store, "record_observations", spy)

    state.queue._emit(job_id, "done", {"status": "done"})

    time.sleep(0.1)  # дать пулу шанс ошибиться
    assert spy.calls == []


def test_progress_event_is_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``progress`` — не терминальное событие: записи нет, хотя файл уже есть."""
    state = app_state(client)
    job_id, result_path = submit(client)
    write_result(result_path)
    spy = RecordSpy(state.store)
    monkeypatch.setattr(state.store, "record_observations", spy)

    state.queue._emit(job_id, "progress", {"progress": 0.5})

    time.sleep(0.1)
    assert spy.calls == []


def test_missing_result_file_warns_and_writes_nothing(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """result.json нет — warning в лог, наблюдений ноль (тихий пропуск запрещён)."""
    state = app_state(client)
    job_id, result_path = submit(client)
    assert not result_path.exists()

    with caplog.at_level(logging.WARNING, logger="app.web"):
        state.queue._emit(job_id, "done", {"status": "done"})
        assert wait_for(
            lambda: any("result.json" in record.getMessage() for record in caplog.records)
        )

    assert state.store.count_observations() == 0


def test_lifespan_registers_hook_and_executor(client: TestClient) -> None:
    """lifespan подписал хук на очередь и создал пул записей (шаг 3d-b)."""
    state = app_state(client)

    assert isinstance(state.post_executor, ThreadPoolExecutor)
    # SSE в этот момент не подключён — в подписчиках ровно хук
    assert len(state.queue._subscribers) == 1


def test_emit_for_unknown_job_is_ignored(client: TestClient) -> None:
    """``done`` по несуществующему id — тихий выход, не исключение в ``_emit``."""
    state = app_state(client)

    state.queue._emit("f" * 32, "done", {"status": "done"})

    time.sleep(0.1)
    assert state.store.count_observations() == 0


def test_broken_result_json_logs_error_and_writes_nothing(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Битый result.json: ошибка в лог, наблюдений нет, исключение не улетает."""
    state = app_state(client)
    job_id, result_path = submit(client)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("{не json", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="app.web"):
        state.queue._emit(job_id, "done", {"status": "done"})
        assert wait_for(
            lambda: any(
                "не читается результат" in record.getMessage()
                for record in caplog.records
            )
        )

    assert state.store.count_observations() == 0


def test_hook_unsubscribed_and_pool_closed_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Выход из lifespan: хук отписан, пул закрыт (порядок шага 3d-b)."""
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(JobQueue, "start", lambda self: None)
    application = create_app(
        token=TOKEN, port=PORT, runner=fake_runner, gh_check=lambda: True
    )

    with TestClient(application, base_url=f"http://{HOST}"):
        pass

    state = application.state
    assert state.queue._subscribers == []
    with pytest.raises(RuntimeError):
        state.post_executor.submit(lambda: None)  # пул уже shutdown


def test_pending_record_survives_lifespan_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Запись, отданная в пул перед выходом, ложится в БД ДО ``store.close()``.

    ``record_observations`` намеренно замедлен на 0.2 с: без
    ``post_executor.shutdown(wait=True)`` до ``store.close()`` запись пришла бы
    в закрытую БД и история осталась бы пустой.
    """
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    # воркеры не поднимаем: событие ``done`` подаётся вручную, фейковый runner
    # не должен запускаться в потоке (его FakeProc не полноценный Popen)
    monkeypatch.setattr(JobQueue, "start", lambda self: None)
    application = create_app(
        token=TOKEN, port=PORT, runner=fake_runner, gh_check=lambda: True
    )
    db_path = tmp_path / "data" / "gapaudit.db"

    with TestClient(application, base_url=f"http://{HOST}") as client:
        state = app_state(client)
        real_record = state.store.record_observations

        def slow_record(rows: list[dict], *, run_id: str, **kwargs: Any) -> list[int]:
            time.sleep(0.2)
            return real_record(rows, run_id=run_id, **kwargs)

        monkeypatch.setattr(state.store, "record_observations", slow_record)
        job_id, result_path = submit(client)
        write_result(result_path)
        state.queue._emit(job_id, "done", {"status": "done"})
        # выходим из контекста сразу: запись ещё в пуле (спит 0.2 с)

    # Store закрыт, поэтому смотрим файл БД заново
    with Store(db_path) as reopened:
        history = reopened.get_observations_history("requests")
    assert len(history) == 1
    assert history[0]["run_id"] == job_id


# --- monitor.diff: события (шаг 3b) -----------------------------------------


def test_done_monitor_diff_records_events(client: TestClient) -> None:
    """``done`` для monitor.diff → события в ``events``, ``run_id`` = id задачи."""
    state = app_state(client)
    job_id, result_path = submit(client, kind="monitor.diff")
    write_payload(result_path, EVENTS_PAYLOAD)

    state.queue._emit(job_id, "done", {"status": "done"})

    assert wait_for(lambda: state.store.count_events() == 2)
    events = state.store.get_events()
    assert {event["kind"] for event in events} == {"release", "flag_appeared"}
    assert {event["name"] for event in events} == {"requests", "flask"}
    assert {event["run_id"] for event in events} == {job_id}
    # события — не наблюдения: история замеров не должна пополняться
    assert state.store.count_observations() == 0


def test_done_monitor_diff_without_result_warns(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """result.json нет у monitor.diff — warning, событий ноль."""
    state = app_state(client)
    job_id, result_path = submit(client, kind="monitor.diff")
    assert not result_path.exists()

    with caplog.at_level(logging.WARNING, logger="app.web"):
        state.queue._emit(job_id, "done", {"status": "done"})
        assert wait_for(
            lambda: any("result.json" in record.getMessage() for record in caplog.records)
        )

    assert state.store.count_events() == 0


def test_done_monitor_diff_skips_rows_without_kind(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Строки без ``kind``/``name`` не пишутся: одна такая отменила бы батч.

    ``events.kind`` в схеме NOT NULL — INSERT упал бы целиком, и «Мониторинг»
    остался бы пустым из-за одной мусорной строки в файле.
    """
    state = app_state(client)
    job_id, result_path = submit(client, kind="monitor.diff")
    write_payload(
        result_path,
        {"events": [{"kind": "release", "name": "requests", "detail": "2.32.0"},
                    {"name": "без-kind"}, "мусор"]},
    )

    with caplog.at_level(logging.WARNING, logger="app.web"):
        state.queue._emit(job_id, "done", {"status": "done"})
        assert wait_for(
            lambda: any("kind/name" in record.getMessage() for record in caplog.records)
        )

    assert state.store.count_events() == 1


# --- catalog.build: каталог и элементы (шаг 3b) -----------------------------


def test_done_catalog_build_records_catalog_and_items(client: TestClient) -> None:
    """``done`` для catalog.build → строка каталога + все его элементы."""
    state = app_state(client)
    job_id, result_path = submit(client, kind="catalog.build")
    write_payload(result_path, CATALOG_PAYLOAD)

    state.queue._emit(job_id, "done", {"status": "done"})

    assert wait_for(lambda: state.store.count_catalog_items(job_id) == 2)
    catalog = state.store.get_catalog(job_id)
    assert catalog is not None
    assert catalog["queries"] == ["test impact analysis"]
    assert catalog["stats"]["total"] == 2
    # путь к результату задачи виден в каталоге: это файл, который прочитал хук
    assert str(catalog["path"]).endswith(f"{job_id}.result.json")

    items = state.store.get_catalog_items(job_id)
    assert [item["name"] for item in items] == ["owner/one", "owner/two"]
    assert items[0]["source"] == "github"
    # источники склеены: одна запись живёт и на GitHub, и в npm
    assert items[1]["source"] == "github,npm"


def test_done_catalog_build_without_result_warns(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """result.json нет у catalog.build — warning, каталога нет."""
    state = app_state(client)
    job_id, result_path = submit(client, kind="catalog.build")
    assert not result_path.exists()

    with caplog.at_level(logging.WARNING, logger="app.web"):
        state.queue._emit(job_id, "done", {"status": "done"})
        assert wait_for(
            lambda: any("result.json" in record.getMessage() for record in caplog.records)
        )

    assert state.store.get_catalog(job_id) is None


def test_done_catalog_build_with_wrong_payload_logs_error(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Вместо объекта каталога — список: ошибка в лог, мусор в БД не попадает."""
    state = app_state(client)
    job_id, result_path = submit(client, kind="catalog.build")
    write_payload(result_path, [ROW])

    with caplog.at_level(logging.ERROR, logger="app.web"):
        state.queue._emit(job_id, "done", {"status": "done"})
        assert wait_for(
            lambda: any(
                "объект каталога" in record.getMessage() for record in caplog.records
            )
        )

    assert state.store.get_catalog(job_id) is None
    assert state.store.count_catalog_items(job_id) == 0
