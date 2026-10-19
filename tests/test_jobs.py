"""Тесты очереди задач (шаг 3a): фейковый подпроцесс, отмена, события.

Настоящие ``cli/`` не участвуют (интеграция — шаг 3d): каждый тест
запускает ``tests/fixtures/fake_job.py`` или крошечный скрипт из
``tmp_path``. Сеть — ноль; подпроцессы — только локальные Python-скрипты.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

import pytest

from app.jobs import Job, JobQueue
from app.store import Store
from core.settings import Settings

FIXTURE_DIR = Path(__file__).parent / "fixtures"
HEX32 = re.compile(r"^[0-9a-f]{32}$")

PopenFactory = Callable[[Job, Path], "subprocess.Popen[str]"]


def wait_for_status(
    queue: JobQueue,
    job_id: str,
    wanted: str | set[str],
    *,
    timeout: float = 5.0,
    poll: float = 0.05,
) -> Job:
    """Ждать, пока задача не примет один из статусов; иначе уронить тест."""
    targets = {wanted} if isinstance(wanted, str) else set(wanted)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = queue.get(job_id)
        if job is not None and job.status in targets:
            return job
        time.sleep(poll)
    job = queue.get(job_id)
    raise AssertionError(
        f"задача {job_id} не достигла {sorted(targets)}; сейчас: {job}"
    )


def fake_runner(counter: list[str]) -> PopenFactory:
    """Runner на ``fake_job.py``: ``job.params`` → аргументы подпроцесса."""

    def run(job: Job, data_dir: Path) -> "subprocess.Popen[str]":
        counter.append(job.id)
        args = [sys.executable, str(FIXTURE_DIR / "fake_job.py")]
        params = job.params
        if "steps" in params:
            args += ["--steps", str(params["steps"])]
        if "sleep" in params:
            args += ["--sleep", str(params["sleep"])]
        if "fail_at" in params:
            args += ["--fail-at", str(params["fail_at"])]
        if params.get("respect_cancel"):
            args += [
                "--respect-cancel",
                "--cancel-file",
                str(data_dir / "jobs" / f"{job.id}.cancel"),
            ]
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )

    return run


def script_runner(script: Path) -> PopenFactory:
    """Runner на произвольном скрипте из ``tmp_path`` (мусор, чужие коды)."""

    def run(job: Job, data_dir: Path) -> "subprocess.Popen[str]":
        return subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )

    return run


@pytest.fixture
def make_queue(tmp_path) -> Iterator[Callable[..., JobQueue]]:
    """Фабрика очередей на общей файловой БД; всё гасится на выходе."""
    store_cm = Store(tmp_path / "jobs.db")
    store = store_cm.__enter__()
    created: list[JobQueue] = []

    def factory(runner: PopenFactory, **overrides) -> JobQueue:
        max_parallel = overrides.pop("max_parallel_jobs", 2)
        overrides.setdefault("poll_interval", 0.05)
        settings = Settings(max_parallel_jobs=max_parallel)
        queue = JobQueue(
            store,
            data_dir=tmp_path / "data",
            settings=settings,
            runner=runner,
            **overrides,
        )
        created.append(queue)
        return queue

    yield factory
    for queue in created:
        queue.stop(timeout=5.0)
    store_cm.__exit__(None, None, None)


# --- 1. submit/get -----------------------------------------------------------


def test_submit_get_queued_and_id_hex(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter))

    job_id = queue.submit("packages.check", {"names": ["requests"]})

    assert HEX32.match(job_id), job_id
    job = queue.get(job_id)
    assert job is not None
    assert job.status == "queued"
    assert job.kind == "packages.check"
    assert job.params == {"names": ["requests"]}
    assert job.progress == 0.0
    assert job.log_path.endswith(f"{job_id}.log")
    assert queue.get("nope") is None


# --- 2. полный путь queued → done --------------------------------------------


def test_run_to_done_progress_pid_and_log(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter))
    queue.start()

    job_id = queue.submit("packages.check", {"steps": 3, "sleep": 0.05})
    job = wait_for_status(queue, job_id, "done")

    assert job.progress == 1.0
    assert job.pid is not None and job.pid > 0
    assert job.started_at is not None
    assert job.finished_at is not None
    assert job.error is None
    log_path = Path(job.log_path)
    assert log_path.is_file()
    assert "fake_job: started" in log_path.read_text(encoding="utf-8")
    assert counter == [job_id]


# --- 3. провал подпроцесса ---------------------------------------------------


def test_failed_with_error_from_protocol(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter))
    queue.start()

    job_id = queue.submit(
        "packages.check", {"steps": 3, "sleep": 0.02, "fail_at": 2}
    )
    job = wait_for_status(queue, job_id, "failed")

    assert job.error is not None and "boom" in job.error


# --- 4. отмена до старта -----------------------------------------------------


def test_cancel_queued_never_starts_process(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter))

    job_id = queue.submit("packages.check", {"steps": 3})
    assert queue.cancel(job_id) is True

    job = queue.get(job_id)
    assert job is not None and job.status == "cancelled"

    queue.start()
    time.sleep(0.3)  # воркеры стартовали — но отменённую не берут
    assert counter == []  # подпроцесс не запускался
    assert queue.cancel(job_id) is False  # уже завершена


# --- 5. отмена running: подпроцесс уважает файл-отмену -----------------------


def test_cancel_running_with_respect_cancel_is_fast(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter), cancel_grace_sec=5.0)
    queue.start()

    job_id = queue.submit(
        "packages.check",
        {"steps": 40, "sleep": 0.1, "respect_cancel": True},
    )
    wait_for_status(queue, job_id, "running")

    started = time.monotonic()
    assert queue.cancel(job_id) is True
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, elapsed

    job = queue.get(job_id)
    assert job is not None and job.status == "cancelled"


# --- 6. отмена running: подпроцесс игнорирует файл → terminate ---------------


def test_cancel_running_escalates_to_terminate(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter), cancel_grace_sec=0.5)
    queue.start()

    job_id = queue.submit("packages.check", {"steps": 40, "sleep": 0.1})
    wait_for_status(queue, job_id, "running")

    started = time.monotonic()
    assert queue.cancel(job_id) is True
    elapsed = time.monotonic() - started
    assert 0.4 <= elapsed < 3.0, elapsed  # прожила grace, потом terminate

    job = queue.get(job_id)
    assert job is not None and job.status == "cancelled"
    time.sleep(0.3)  # воркер увидел мёртвый процесс — статус не перетирает
    assert queue.get(job_id).status == "cancelled"


# --- 7. ограничение параллелизма ---------------------------------------------


def test_max_parallel_jobs_two_of_three(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter), max_parallel_jobs=2)
    queue.start()

    ids = [
        queue.submit("packages.check", {"steps": 8, "sleep": 0.1})
        for _ in range(3)
    ]

    deadline = time.monotonic() + 5.0
    seen_two_running = False
    while time.monotonic() < deadline:
        running = queue.list(status="running")
        queued = queue.list(status="queued")
        if len(running) == 2 and len(queued) == 1:
            seen_two_running = True
            break
        time.sleep(0.02)
    assert seen_two_running, [job.status for job in queue.list()]

    for job_id in ids:
        wait_for_status(queue, job_id, "done", timeout=10.0)


# --- 8. события в порядке жизни задачи ---------------------------------------


def test_subscribe_event_order(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter))
    events: list[str] = []
    lock = threading.Lock()

    def callback(job_id: str, event_type: str, data: dict) -> None:
        with lock:
            events.append(event_type)

    queue.subscribe(callback)
    queue.start()
    job_id = queue.submit("packages.check", {"steps": 3, "sleep": 0.05})
    wait_for_status(queue, job_id, "done")
    time.sleep(0.1)

    order = [
        events.index(name) if name in events else -1
        for name in ("created", "started", "progress", "done")
    ]
    assert all(index >= 0 for index in order), events
    assert order == sorted(order), events
    assert events.count("created") == 1
    assert events.count("done") == 1
    assert events.count("cancelled") == 0


# --- 9. unsubscribe ----------------------------------------------------------


def test_unsubscribe_stops_events(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter))
    events: list[str] = []

    def callback(job_id: str, event_type: str, data: dict) -> None:
        events.append(event_type)

    queue.subscribe(callback)
    queue.unsubscribe(callback)
    queue.start()
    job_id = queue.submit("packages.check", {"steps": 1, "sleep": 0.01})
    wait_for_status(queue, job_id, "done")
    time.sleep(0.1)
    assert events == []


# --- 10. list(status=...) -----------------------------------------------------


def test_list_filters_done_only(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter))
    queue.start()

    ok = queue.submit("packages.check", {"steps": 1, "sleep": 0.01})
    bad = queue.submit(
        "packages.check", {"steps": 1, "sleep": 0.01, "fail_at": 1}
    )
    wait_for_status(queue, ok, "done")
    wait_for_status(queue, bad, "failed")

    assert [job.id for job in queue.list(status="done")] == [ok]
    assert [job.id for job in queue.list(status="failed")] == [bad]
    assert len(queue.list()) == 2
    assert len(queue.list(limit=1)) == 1


# --- 11. дубликат job_id ------------------------------------------------------


def test_duplicate_job_id_raises(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter))

    job_id = queue.submit("packages.check", {"steps": 1})
    with pytest.raises(ValueError, match="уже существует"):
        queue.submit("packages.check", {"steps": 1}, job_id=job_id)


# --- 12. stop -----------------------------------------------------------------


def test_stop_cancels_active_and_workers_exit(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter), max_parallel_jobs=2)
    queue.start()

    ids = [
        queue.submit("packages.check", {"steps": 30, "sleep": 0.1})
        for _ in range(2)
    ]
    for job_id in ids:
        wait_for_status(queue, job_id, "running")

    queue.stop(timeout=1.0)

    for job_id in ids:
        job = queue.get(job_id)
        assert job is not None and job.status == "cancelled", job
        assert job.message == "shutdown", job
    workers = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("gapaudit-job-worker")
    ]
    assert workers == []


# --- 13. троттлинг progress ---------------------------------------------------


def test_progress_events_are_throttled(make_queue) -> None:
    counter: list[str] = []
    queue = make_queue(fake_runner(counter))
    progress_events: list[dict] = []

    def callback(job_id: str, event_type: str, data: dict) -> None:
        if event_type == "progress":
            progress_events.append(data)

    queue.subscribe(callback)
    queue.start()
    job_id = queue.submit("packages.check", {"steps": 100, "sleep": 0.001})
    wait_for_status(queue, job_id, "done", timeout=10.0)

    assert len(progress_events) <= 15, len(progress_events)


# --- 14. мусорный stdout -------------------------------------------------------


def test_garbage_stdout_goes_to_log_without_crash(make_queue, tmp_path) -> None:
    script = tmp_path / "garbage_job.py"
    script.write_text(
        "import json\n"
        "print('hello world')\n"
        "print(json.dumps({'type': 'done'}))\n",
        encoding="utf-8",
        newline="\n",
    )
    counter: list[str] = []
    queue = make_queue(script_runner(script))
    queue.start()

    job_id = queue.submit("packages.check", {})
    job = wait_for_status(queue, job_id, "done")

    log_text = Path(job.log_path).read_text(encoding="utf-8")
    assert "hello world" in log_text  # непарсящаяся строка ушла в лог


# --- 15. ненулевой код без {"type": "error"} ------------------------------------


def test_nonzero_exit_without_error_is_failed(make_queue, tmp_path) -> None:
    script = tmp_path / "exit7_job.py"
    script.write_text("raise SystemExit(7)\n", encoding="utf-8", newline="\n")
    counter: list[str] = []
    queue = make_queue(script_runner(script))
    queue.start()

    job_id = queue.submit("packages.check", {})
    job = wait_for_status(queue, job_id, "failed")

    assert job.error is not None and "exit code 7" in job.error
