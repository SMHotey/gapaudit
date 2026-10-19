"""Общие фикстуры веб-тестов (шаг 3b): приложение, клиент, фейки.

Здесь же лежат константы контура (порт/токен) — их импортируют модули
``tests/test_web_*.py``.

Два решения, ради которых фикстуры живут в conftest, а не в каждом файле:

* ``GAPAUDIT_DATA_DIR`` указывает на ``tmp_path``: lifespan (``app/web.py``)
  создаёт каталог, открывает ``Store`` и очередь ровно там, где их ждёт тест —
  так проверяется путь из desktop.py (env → ontpost → один каталог);
* ``JobQueue.start`` подменён на запись вызова: веб-тесты проверяют слой HTTP,
  а не исполнение задач. Без этого воркеры поднимались бы при каждом ``with
  TestClient`` и гоняли фейковый runner, а статус только что отправленной
  задачи перестал бы быть предсказуемым. Покрытие воркеров — в
  ``tests/test_jobs.py`` (AGENTS gotcha 23: быстрый цикл без Popen).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.jobs import Job, JobQueue
from app.web import TOKEN_COOKIE, create_app

#: Контур тестового приложения (порт совпадает с Host из gotcha 3).
PORT = 8799
HOST = f"127.0.0.1:{PORT}"
TOKEN = "test-token-123"


@pytest.fixture(scope="session", autouse=True)
def _env_isolation():
    """Изолирует тесты от утечки GAPAUDIT_* env из шелла
    (gotcha 33). На старте сохраняет значения, удаляет; на выходе
    восстанавливает."""
    import os
    keys = ("GAPAUDIT_FAKE_HTTP", "GAPAUDIT_DATA_DIR",
            "GAPAUDIT_KEYWORDS", "GAPAUDIT_CACHE_TTL_H",
            "GAPAUDIT_MAX_JOBS", "GAPAUDIT_NO_BROWSER")
    saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
    yield
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(saved)


def auth(token: str = TOKEN) -> dict[str, str]:
    """Заголовок с токеном — самое частое действие в веб-тестах."""
    return {"X-GapAudit-Token": token}


def cookie_header(token: str = TOKEN) -> dict[str, str]:
    """Токен в cookie строкой (``cookies=`` у httpx deprecated — gotcha в тестах)."""
    return {"Cookie": f"{TOKEN_COOKIE}={token}"}


class FakeProc:
    """``subprocess.Popen``-заглушка: протокол отдан сразу, процесса нет.

    Нужна, чтобы runner в фикстуре оставался валидным для ``JobQueue``, но
    сюита не поднимала ни одного реального подпроцесса.
    """

    def __init__(self, lines: tuple[str, ...] = ()) -> None:
        self.pid = 4242
        self.stdout: Any = iter(lines)
        self.stderr: Any = iter(())
        self._exit: int | None = None

    def poll(self) -> int | None:
        return self._exit

    def wait(self) -> int:
        self._exit = 0
        return 0

    def terminate(self) -> None:
        self._exit = -15

    def kill(self) -> None:
        self._exit = -9


def fake_runner(job: Job, data_dir: Path) -> FakeProc:
    """Runner без процесса: полезная нагрузка — прогресс из параметров задачи."""
    progress = json.dumps(
        {"type": "progress", "progress": 0.5, "message": "фейк"},
        ensure_ascii=False,
    )
    return FakeProc((progress + "\n", '{"type": "done"}\n'))


class GhProbe:
    """Фейковая проверка ``gh auth status``: счётчик вызовов + управляемый результат."""

    def __init__(self) -> None:
        self.calls = 0
        self.result = True
        self.error: Exception | None = None

    def check(self) -> bool:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture()
def gh_probe() -> GhProbe:
    return GhProbe()


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh_probe: GhProbe) -> Iterator[Any]:
    """Приложение с каталогом данных в tmp и воркерами, которые не поднимаются."""
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    started: list[JobQueue] = []
    monkeypatch.setattr(JobQueue, "start", lambda self: started.append(self))
    application = create_app(
        token=TOKEN, port=PORT, runner=fake_runner, gh_check=gh_probe.check
    )
    application.state.jobs_started = started
    yield application


def app_state(client: TestClient) -> Any:
    """``app.state`` живого клиента.

    ``TestClient.app`` типизирован как ASGI-протокол, а нам нужен FastAPI со
    своим ``state`` — поэтому здесь одно приведение, а не в каждом тесте.
    """
    return cast(Any, client.app).state


@pytest.fixture()
def client(app: Any) -> Iterator[TestClient]:
    """Клиент, внутри которого lifespan поднял Store/очередь (нужен ``with``).

    ``base_url`` задаёт ``Host: 127.0.0.1:8799`` — иначе httpx шлёт
    ``Host: testserver``, и миддлварь честно отвечает 403 (AGENTS gotcha 3).
    """
    with TestClient(app, base_url=f"http://{HOST}") as test_client:
        yield test_client
