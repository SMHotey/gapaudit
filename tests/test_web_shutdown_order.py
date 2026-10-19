"""Порядок выключения lifespan (шаг 3d-b): запись в Store не проигрывает закрытию.

Сценарий ровно тот, что ломает неправильный порядок: задача завершается, хук
отдаёт наблюдения в ``post_executor``, и приложение тут же закрывается. Если
``store.close()`` случается раньше ``post_executor.shutdown(wait=True)``, запись
уходит в закрытую БД и история остаётся пустой.

Два гейта:

* по порядку вызовов (``record_observations`` раньше ``close``) — детерминированный:
  запись намеренно замедлена, поэтому из lifespan выходим с записью в пуле;
* по файлу БД: после выхода из ``TestClient`` наблюдения уже на диске.

``@pytest.mark.slow`` — настоящий ``Popen`` (``_default_runner``), AGENTS gotcha 23.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.store import Store
from app.web import create_app
from tests.conftest import HOST, PORT, TOKEN, GhProbe, auth

JOB_TIMEOUT = 60.0
POLL = 0.05

#: Пауза внутри записи: гарантирует, что на момент выхода из lifespan запись
#: ещё в пуле (без неё тест проходил бы и при перевернутом порядке).
RECORD_DELAY = 0.2


@pytest.fixture()
def real_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Приложение с настоящей очередью и настоящим runner; каталог — в tmp."""
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GAPAUDIT_FAKE_HTTP", "1")
    application = create_app(
        token=TOKEN, port=PORT, runner=None, gh_check=GhProbe().check
    )
    return application


def wait_done(client: TestClient, job_id: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = client.get(f"/api/jobs/{job_id}", headers=auth()).json()["status"]
        if status in ("done", "failed", "cancelled"):
            return status
        time.sleep(POLL)
    return status


def submit_and_wait_done(client: TestClient) -> str:
    """Отправить ``packages.check`` и дождаться ``done`` (реальный подпроцесс)."""
    posted = client.post(
        "/api/jobs",
        json={"kind": "packages.check", "params": {"names": ["requests"]}},
        headers=auth(),
    )
    assert posted.status_code == 202, posted.text
    job_id = posted.json()["job_id"]
    assert wait_done(client, job_id, JOB_TIMEOUT) == "done"
    return job_id


@pytest.mark.slow
def test_observations_written_before_store_closed(real_app, tmp_path: Path) -> None:
    """После выхода из ``TestClient`` наблюдения уже на диске (Store закрыт)."""
    db_path = tmp_path / "data" / "gapaudit.db"
    job_id = ""

    with TestClient(real_app, base_url=f"http://{HOST}") as client:
        job_id = submit_and_wait_done(client)
        # выходим немедленно: ни наблюдений, ни post_executor не ждём

    assert db_path.is_file(), db_path
    # Store приложения закрыт — читаем тот же файл заново
    with Store(db_path) as reopened:
        history = reopened.get_observations_history("requests")
    assert history, "наблюдения не записались: post_executor не дождались до store.close()"
    assert history[0]["run_id"] == job_id


@pytest.mark.slow
def test_record_happens_before_store_close(
    real_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порядок вызовов: ``record_observations`` → ``close``, а не наоборот.

    Гейт, который падает при перевернутом порядке выключения: запись замедлена,
    поэтому выходить из lifespan приходится с записью в пуле.
    """
    db_path = tmp_path / "data" / "gapaudit.db"
    events: list[str] = []
    real_close = Store.close
    real_record = Store.record_observations

    def close(self) -> None:
        events.append("close")
        real_close(self)

    def record(self, rows: list[dict], *, run_id: str, **kwargs: Any) -> list[int]:
        events.append("record")
        time.sleep(RECORD_DELAY)
        return real_record(self, rows, run_id=run_id, **kwargs)

    monkeypatch.setattr(Store, "close", close)
    monkeypatch.setattr(Store, "record_observations", record)

    with TestClient(real_app, base_url=f"http://{HOST}") as client:
        job_id = submit_and_wait_done(client)

    # первые два события — внутри lifespan; закрытие переоткрытого Store ниже
    # добавит ещё один "close"
    assert events[:2] == ["record", "close"], events

    with Store(db_path) as reopened:
        history = reopened.get_observations_history("requests")
    assert history and history[0]["run_id"] == job_id
