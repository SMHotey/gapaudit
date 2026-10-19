"""Восстановление прерванных задач (шаг 3c): ``recover``/``resume``.

Контракт (шаг 3c, DEVPLAN §6/§7):

* ``recover_interrupted`` — только при старте, из lifespan, до ``queue.start()``:
  ``running`` с ``started_at`` → ``interrupted``; ``running`` без ``started_at``,
  ``queued``, ``done`` — не трогаются;
* ``resume_interrupted`` — ``interrupted`` + существующий ``log_path`` →
  ``queued`` и в очередь; всё остальное → ``False``;
* из ``__init__`` ничего не восстанавливается — конструктор не меняет хранилище.

Сети нет; единственный ``Popen`` — в slow-тесте «падение и возобновление».
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.jobs import Job, JobQueue
from app.store import Store
from core.settings import Settings

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    """Открытая файловая БД; закрывается на выходе из теста."""
    store_cm = Store(tmp_path / "recover.db")
    opened = store_cm.__enter__()
    yield opened
    store_cm.__exit__(None, None, None)


def make_queue(store: Store, tmp_path: Path, **overrides) -> JobQueue:
    """Очередь без воркеров (для ``recover``/``resume``) или с ними (slow)."""
    runner = overrides.pop("runner", None)
    settings = Settings(max_parallel_jobs=overrides.pop("max_parallel_jobs", 1))
    return JobQueue(
        store,
        data_dir=tmp_path / "data",
        settings=settings,
        runner=runner,
        **overrides,
    )


def seed_job(
    store: Store,
    tmp_path: Path,
    *,
    status: str,
    started_at: str | None = None,
    log: bool = True,
    job_id: str = "a" * 32,
) -> str:
    """Записать строку ``jobs`` заданного вида (+ файл лога, если нужен)."""
    log_path = tmp_path / "data" / "jobs" / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log:
        log_path.write_text("fake_job: started\n", encoding="utf-8", newline="\n")
    store.create_job(
        {
            "id": job_id,
            "kind": "packages.check",
            "params": {"names": ["x"]},
            "status": status,
            "progress": 0.0,
            "message": "",
            "created_at": "2026-10-19T00:00:00+00:00",
            "log_path": str(log_path),
        }
    )
    if started_at is not None:
        # create_job started_at не принимает (оставляет NULL) — это поле
        # заполняет update_job, как в жизни делает воркер
        store.update_job(job_id, started_at=started_at)
    return job_id


def job_row(store: Store, job_id: str) -> dict:
    """Строка задачи из Store (тест обязан знать, что задача есть)."""
    row = store.get_job(job_id)
    assert row is not None, f"задача {job_id} не найдена в Store"
    return row


# --- recover_interrupted ------------------------------------------------------


def test_recover_without_running_returns_empty(store: Store, tmp_path: Path) -> None:
    queue = make_queue(store, tmp_path)

    assert queue.recover_interrupted() == []


def test_recover_moves_running_with_started_at_to_interrupted(
    store: Store, tmp_path: Path
) -> None:
    job_id = seed_job(
        store, tmp_path, status="running", started_at="2026-10-19T00:00:01+00:00"
    )
    queue = make_queue(store, tmp_path)

    assert queue.recover_interrupted() == [job_id]
    assert job_row(store, job_id)["status"] == "interrupted"


def test_recover_leaves_running_without_started_at_alone(
    store: Store, tmp_path: Path
) -> None:
    """``running`` без ``started_at`` — нештатное состояние, не наше решение."""
    job_id = seed_job(store, tmp_path, status="running", started_at=None)
    queue = make_queue(store, tmp_path)

    assert queue.recover_interrupted() == []
    assert job_row(store, job_id)["status"] == "running"


def test_recover_leaves_queued_job_alone(store: Store, tmp_path: Path) -> None:
    job_id = seed_job(store, tmp_path, status="queued")
    queue = make_queue(store, tmp_path)

    assert queue.recover_interrupted() == []
    assert job_row(store, job_id)["status"] == "queued"


def test_recover_leaves_done_job_alone(store: Store, tmp_path: Path) -> None:
    job_id = seed_job(store, tmp_path, status="done")
    queue = make_queue(store, tmp_path)

    assert queue.recover_interrupted() == []
    assert job_row(store, job_id)["status"] == "done"


def test_recover_is_not_called_from_constructor(store: Store, tmp_path: Path) -> None:
    """Конструктор не меняет хранилище — восстановление только из lifespan."""
    job_id = seed_job(
        store, tmp_path, status="running", started_at="2026-10-19T00:00:01+00:00"
    )

    make_queue(store, tmp_path)

    assert job_row(store, job_id)["status"] == "running"


# --- resume_interrupted -------------------------------------------------------


def test_resume_interrupted_job_enqueues_and_resets_fields(
    store: Store, tmp_path: Path
) -> None:
    job_id = seed_job(
        store, tmp_path, status="interrupted", started_at="2026-10-19T00:00:01+00:00"
    )
    store.update_job(
        job_id, finished_at="2026-10-19T00:05:00+00:00", progress=0.7, message="упала"
    )
    queue = make_queue(store, tmp_path)
    cancel_path = queue.jobs_dir / f"{job_id}.cancel"
    cancel_path.parent.mkdir(parents=True, exist_ok=True)
    cancel_path.touch()

    assert queue.resume_interrupted(job_id) is True

    row = store.get_job(job_id)
    assert row["status"] == "queued"
    assert row["started_at"] is None
    assert row["finished_at"] is None
    assert row["error"] is None
    assert row["progress"] == 0.0
    assert row["message"] == ""
    assert job_id in queue._queue  # deque заполнен — воркеры разбудятся
    assert not cancel_path.exists()  # след отмены прошлого запуска убран


def test_resume_queued_job_is_refused(store: Store, tmp_path: Path) -> None:
    job_id = seed_job(store, tmp_path, status="queued")
    queue = make_queue(store, tmp_path)

    assert queue.resume_interrupted(job_id) is False
    assert job_row(store, job_id)["status"] == "queued"


def test_resume_unknown_job_is_refused(store: Store, tmp_path: Path) -> None:
    queue = make_queue(store, tmp_path)

    assert queue.resume_interrupted("f" * 32) is False


def test_resume_without_log_file_is_refused(store: Store, tmp_path: Path) -> None:
    job_id = seed_job(
        store,
        tmp_path,
        status="interrupted",
        started_at="2026-10-19T00:00:01+00:00",
        log=False,
    )
    queue = make_queue(store, tmp_path)

    assert queue.resume_interrupted(job_id) is False
    assert job_row(store, job_id)["status"] == "interrupted"


# --- slow: настоящий подпроцесс, падение, восстановление ----------------------


def _long_runner(job: Job, data_dir: Path) -> "subprocess.Popen[str]":
    """``fake_job.py`` с долгой паузой: задача живёт, пока её не погасят."""
    return subprocess.Popen(
        [
            sys.executable,
            str(FIXTURE_DIR / "fake_job.py"),
            "--steps",
            "3",
            "--sleep",
            "30",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )


def _wait_log(path: Path, timeout: float = 5.0) -> None:
    """Дождаться, пока воркер откроет лог-файл задачи (нужен ``resume``)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"лог {path} не появился за {timeout} с")


@pytest.mark.slow
def test_crash_recovery_roundtrip_with_real_worker(
    store: Store, tmp_path: Path
) -> None:
    """Реальный воркер → «падение» → recover → resume (без остановки воркера).

    Падение эмулируется вторым экземпляром ``JobQueue`` на той же БД — так же,
    как это произойдёт после taskkill: первый процесс не участвует в
    восстановлении и не пишет статусы, пока его задача не завершится.
    """
    running = make_queue(
        store, tmp_path, runner=_long_runner, cancel_grace_sec=0.5, poll_interval=0.05
    )
    rebooted: JobQueue | None = None
    running.start()
    try:
        job_id = running.submit("packages.check", {"names": ["x"]})
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            job = running.get(job_id)
            if job is not None and job.status == "running":
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"задача не стала running: {running.get(job_id)}")
        started = running.get(job_id)
        assert started is not None
        _wait_log(Path(started.log_path))

        rebooted = make_queue(store, tmp_path, runner=_long_runner)
        assert rebooted.recover_interrupted() == [job_id]
        recovered = rebooted.get(job_id)
        assert recovered is not None and recovered.status == "interrupted"
        assert rebooted.resume_interrupted(job_id) is True
        resumed = rebooted.get(job_id)
        assert resumed is not None and resumed.status == "queued"
    finally:
        running.stop(timeout=5.0)
        if rebooted is not None:
            rebooted.stop(timeout=5.0)
