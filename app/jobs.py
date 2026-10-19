"""Очередь задач GapAudit: подпроцессы, статусы, отмена, логи (P1, DEVPLAN §6).

Слой ``app/`` (карта проекта — DEVPLAN §3): модуль знает про Store и про
подпроцесс, но ничего не знает про FastAPI — для ``app/web.py`` (шаг 3b)
достаточно :meth:`JobQueue.subscribe` (SSE подпишется на события).

Контракт (DEVPLAN §6):

* статусы ``queued → running → done | failed | cancelled`` (``interrupted``
  и восстановление — шаг 3c);
* параллелизм — ``Settings.max_parallel_jobs`` воркеров;
* отмена — сначала пустой файл ``<data_dir>/jobs/<id>.cancel`` (его читает
  сам подпроцесс), через ``cancel_grace_sec`` — ``terminate`` процесса;
  отменённая задача остаётся ``cancelled``, даже если процесс вышел с кодом 0;
* лог каждой задачи — отдельный файл ``<data_dir>/jobs/<id>.log``: в него
  пишется stderr подпроцесса и нераспознанные строки stdout; сами
  JSON-lines протокола в лог не дублируются (они живут в
  ``jobs.progress``/``jobs.message``).

Протокол подпроцесса — JSON-lines в stdout, одна сущность на строку::

    {"type": "hello", "pid": 12345}
    {"type": "progress", "progress": 0.42, "message": "стр. 34/152"}
    {"type": "result", "json_path": "raw/result.json"}
    {"type": "error", "error": "..."}
    {"type": "done"}

Строка, которая не парсится в JSON-объект протокола, пишется в лог как
stderr-строка и не роняет задачу.

Истина статусов — Store (``store.update_job`` + ``store.get_job``); память
очереди — рабочая очередь (``deque`` id), живые процессы, флаги отмены и
подписчики. Любой терминальный переход проходит через :meth:`JobQueue._finalize`
и выполняется ровно один раз на задачу — независимо от того, кто первый:
воркер или отмена. Всё, что трогает статусы, берёт ``self._lock``, поэтому
``store`` (помимо собственного замка, gotcha 21) защищён и от воркеров, и от
отмены, и от будущего ``app/web.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

if TYPE_CHECKING:  # только для аннотаций: циклический импорт на старте не нужен
    from app.store import Store
    from core.settings import Settings

__all__ = [
    "EventCallback",
    "Job",
    "JobQueue",
    "JobStatus",
    "TERMINAL_STATUSES",
    "job_from_row",
]

JobStatus = Literal[
    "queued", "running", "done", "failed", "cancelled", "interrupted"
]

#: Задача уже не изменится — новые события от неё не ждём.
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "interrupted"})

#: Троттлинг события ``progress``: UI не переварит 100 сообщений в секунду.
PROGRESS_EMIT_INTERVAL = 0.1

EventCallback = Callable[[str, str, dict], None]
# (job_id, event_type, data); event_type: created | started | progress |
# done | failed | cancelled


@dataclass
class Job:
    """Задача целиком — проекция строки ``jobs`` из Store."""

    id: str
    kind: str
    params: dict
    status: JobStatus
    progress: float = 0.0
    message: str = ""
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    log_path: str = ""
    error: str | None = None
    pid: int | None = None


def job_from_row(row: dict) -> Job:
    """Строка ``Store.get_job()``/``Store.list_jobs()`` → :class:`Job`."""
    return Job(
        id=str(row["id"]),
        kind=str(row["kind"]),
        params=row.get("params") or {},
        status=row["status"],
        progress=float(row.get("progress") or 0.0),
        message=row.get("message") or "",
        created_at=row.get("created_at") or "",
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        log_path=row.get("log_path") or "",
        error=row.get("error"),
        pid=row.get("pid"),
    )


class JobQueue:
    """Очередь задач: submit/cancel/list/get + воркеры и отмена подпроцессов.

    Один экземпляр на приложение. :meth:`start` поднимает
    ``settings.max_parallel_jobs`` daemon-потоков, :meth:`stop` гасит их,
    отменяя активные задачи. Store должен быть открыт (``with Store(...)``)
    — очередь не владеет его жизненным циклом.
    """

    def __init__(
        self,
        store: "Store",
        *,
        data_dir: str | Path,
        settings: "Settings",
        runner: "Callable[[Job, Path], subprocess.Popen] | None" = None,
        cancel_grace_sec: float = 10.0,
        poll_interval: float = 0.2,
    ):
        """
        store — открытый ``app.store.Store`` (``__enter`` вызван).
        data_dir — в нём живут ``jobs/<id>.log`` и ``jobs/<id>.cancel``.
        runner — как запускать подпроцесс; по умолчанию ``_default_runner``
            (``python -m cli.<kind>`` — параметризация в шаге 3d, в 3a тесты
            передают свой runner).
        cancel_grace_sec — сколько ждать, пока подпроцесс сам проверит
            файл-отмену, прежде чем ``terminate``.
        poll_interval — как часто проверять, завершился ли процесс.
        """
        self._store = store
        self._data_dir = Path(data_dir)
        self._settings = settings
        self._runner = (
            runner if runner is not None else _default_runner
        )
        self._cancel_grace_sec = float(cancel_grace_sec)
        self._poll_interval = max(0.01, float(poll_interval))

        # рабочая очередь (только id) и её условие — отдельно от _lock:
        # воркеры ждут в self._cond.wait(), а submit/cancel дёргают notify.
        self._queue: deque[str] = deque()
        self._cond = threading.Condition()
        # всё, что связано со статусами/процессами/подписчиками:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._active: dict[str, subprocess.Popen] = {}
        self._cancel_flags: set[str] = set()
        self._finalized: set[str] = set()
        self._progress_emits: dict[str, float] = {}
        self._subscribers: list[EventCallback] = []
        # лог пишется из воркера (stdout-мусор) и из stderr-потока:
        self._log_lock = threading.Lock()

    # --- свойства ----------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """Каталог данных очереди (логи и файлы отмены лежат в ``jobs/``)."""
        return self._data_dir

    @property
    def jobs_dir(self) -> Path:
        """Каталог логов и файлов отмены: ``<data_dir>/jobs``."""
        return self._data_dir / "jobs"

    # --- жизненный цикл ----------------------------------------------------

    def start(self) -> None:
        """Запустить воркеров: ``N = settings.max_parallel_jobs``, daemon.

        Повторный ``start()`` — no-op (не плодит потоки).
        """
        if self._workers:
            return
        self._stop.clear()
        count = max(1, int(self._settings.max_parallel_jobs))
        for index in range(count):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"gapaudit-job-worker-{index}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Остановить очередь.

        Graceful: сначала фиксируем отмену активных задач (файл-отмена для
        подпроцессов), затем ждём до ``timeout``, пока воркеры доработают
        текущую итерацию. Активные, что ещё работают к этому моменту, —
        ``cancelled`` с detail ``shutdown``; оставшиеся живыми процессы —
        ``terminate``, и финальное ожидание выхода воркеров.
        """
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

        with self._lock:
            active_ids = list(self._active)
        for job_id in active_ids:
            self._request_cancel(job_id)

        deadline = time.monotonic() + max(0.0, timeout)
        for worker in self._workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))

        with self._lock:
            running_ids = [
                job_id
                for job_id, proc in self._active.items()
                if proc.poll() is None
            ]
        for job_id in running_ids:
            self._finalize(
                job_id, "cancelled", message="shutdown", detail="shutdown"
            )
        for job_id in running_ids:
            with self._lock:
                proc = self._active.get(job_id)
            if proc is not None and proc.poll() is None:
                proc.terminate()

        for worker in self._workers:
            worker.join(timeout=2.0)
        self._workers.clear()

    # --- API ---------------------------------------------------------------

    def submit(self, kind: str, params: dict, *, job_id: str | None = None) -> str:
        """Создать задачу в Store, положить в очередь, вернуть её id.

        ``job_id`` — uuid4-hex (32 hex-символа), если не передан. Повторный
        ``submit`` с занятым id — ``ValueError``: id задачи — ключ истории,
        тихая перезапись потеряла бы старую задачу.
        """
        new_id = job_id or uuid.uuid4().hex
        log_path = self.jobs_dir / f"{new_id}.log"
        created_at = _utcnow_iso()
        with self._lock:
            if self._store.get_job(new_id) is not None:
                raise ValueError(f"задача с id {new_id} уже существует")
            self._store.create_job(
                {
                    "id": new_id,
                    "kind": kind,
                    "params": dict(params),
                    "status": "queued",
                    "progress": 0.0,
                    "message": "",
                    "created_at": created_at,
                    "log_path": str(log_path),
                }
            )
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        with self._cond:
            self._queue.append(new_id)
            self._cond.notify()
        self._emit(new_id, "created", {"kind": kind, "status": "queued"})
        return new_id

    def cancel(self, job_id: str) -> bool:
        """Отменить задачу: True — принято, False — не найдена/завершена.

        ``queued`` — сразу ``cancelled`` (процесс не запускается). ``running``
        — файл ``<id>.cancel``, ожидание до ``cancel_grace_sec``, затем
        ``terminate``; статус — ``cancelled`` (даже если процесс вышел с 0).
        """
        row = self._store.get_job(job_id)
        if row is None:
            return False
        if row["status"] in TERMINAL_STATUSES:
            return False

        with self._cond:
            queued = job_id in self._queue
            if queued:
                self._queue.remove(job_id)
        if queued:
            self._finalize(
                job_id,
                "cancelled",
                message="отменена до старта",
                detail="cancelled before start",
            )
            return self._status_of(job_id) == "cancelled"

        # running или воркер только собирается запускать подпроцесс:
        self._request_cancel(job_id)
        deadline = time.monotonic() + self._cancel_grace_sec
        while time.monotonic() < deadline:
            with self._lock:
                proc = self._active.get(job_id)
            if proc is None or proc.poll() is not None:
                break
            time.sleep(self._poll_interval)
        with self._lock:
            proc = self._active.get(job_id)
        if proc is not None and proc.poll() is None:
            self._terminate(proc)
        self._finalize(
            job_id, "cancelled", message="отменена пользователем", detail="cancelled"
        )
        return self._status_of(job_id) == "cancelled"

    def get(self, job_id: str) -> Job | None:
        """Задача из Store (источник правды) или None."""
        row = self._store.get_job(job_id)
        return job_from_row(row) if row is not None else None

    def list(self, *, status: str | None = None, limit: int = 100) -> list[Job]:
        """Задачи по ``created_at DESC``; ``status`` — точный фильтр."""
        rows = self._store.list_jobs(status=status, limit=limit)
        return [job_from_row(row) for row in rows]

    def subscribe(self, callback: EventCallback) -> None:
        """Подписаться на события (шаг 3b повесит сюда SSE)."""
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: EventCallback) -> None:
        """Отписаться; повторная отписка — no-op."""
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    # --- воркеры -----------------------------------------------------------

    def _worker_loop(self) -> None:
        """Один воркер: ждать очередь → взять id → выполнить задачу."""
        while True:
            with self._cond:
                while not self._stop.is_set() and not self._queue:
                    self._cond.wait(timeout=0.1)
                if self._stop.is_set():
                    return  # после stop новые задачи не берём
                if not self._queue:
                    continue
                job_id = self._queue.popleft()
            self._run_job(job_id)

    def _run_job(self, job_id: str) -> None:
        """Полный жизненный цикл одной задачи в воркере."""
        row = self._store.get_job(job_id)
        if row is None or row["status"] != "queued":
            return  # отменена, пока стояла в очереди
        job = job_from_row(row)

        # отмена до старта (гонка с cancel() после deque-проверки):
        with self._lock:
            cancelled_early = job_id in self._cancel_flags
        if cancelled_early:
            self._finalize(
                job_id,
                "cancelled",
                message="отменена до старта",
                detail="cancelled before start",
            )
            return

        # переход queued → running атомарен с проверкой отмены/финализации:
        with self._lock:
            blocked = job_id in self._finalized or job_id in self._cancel_flags
            if not blocked:
                self._store.update_job(
                    job_id,
                    status="running",
                    started_at=_utcnow_iso(),
                    message="запущена",
                )
        if blocked:
            self._finalize(
                job_id,
                "cancelled",
                message="отменена до старта",
                detail="cancelled before start",
            )
            return
        self._emit(job_id, "started", {"status": "running"})

        # запуск: проверка флага и Popen под одним замком — cancel() не может
        # увидеть «процесса нет» в момент, когда он уже создаётся.
        try:
            with self._lock:
                if job_id in self._cancel_flags:
                    proc: subprocess.Popen | None = None
                else:
                    proc = self._runner(job, self._data_dir)
                    self._active[job_id] = proc
        except Exception as error:  # noqa: BLE001 - любой сбой запуска = failed
            self._finalize(
                job_id, "failed", error=f"не удалось запустить подпроцесс: {error}"
            )
            return
        if proc is None:
            self._finalize(
                job_id,
                "cancelled",
                message="отменена до старта",
                detail="cancelled before start",
            )
            return
        self._store.update_job(job_id, pid=proc.pid)

        state = _RunState()
        log_path = Path(job.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # один раз на задачу, append: лог прошлого запуска не затирается.
        log = open(log_path, "a", encoding="utf-8", errors="replace", newline="\n")
        stderr_thread = threading.Thread(
            target=self._pump_stderr,
            args=(proc, log),
            name=f"gapaudit-job-stderr-{job_id[:8]}",
            daemon=True,
        )
        stderr_thread.start()
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    self._handle_stdout_line(job_id, line, log, state)
            proc.wait()
        finally:
            stderr_thread.join(timeout=5.0)
            with self._log_lock:
                log.close()
            with self._lock:
                self._active.pop(job_id, None)
                cancelled = job_id in self._cancel_flags
            self._progress_emits.pop(job_id, None)

        # итоговый статус (приоритет: отмена > error > exit code > done):
        if cancelled:
            self._finalize(
                job_id, "cancelled", message="отменена", detail="cancelled"
            )
        elif state.error is not None:
            self._finalize(job_id, "failed", error=state.error)
        elif proc.returncode not in (0, None):
            self._finalize(job_id, "failed", error=f"exit code {proc.returncode}")
        else:
            # код 0: done, даже если {"type": "done"} не приходил — протокол
            # не обязан быть идеальным, ненулевой код разбирается выше.
            self._finalize(job_id, "done", progress=1.0)

    # --- протокол подпроцесса ----------------------------------------------

    def _handle_stdout_line(
        self, job_id: str, raw_line: str, log: Any, state: "_RunState"
    ) -> None:
        """Разобрать строку stdout: JSON-lines протокола или мусор в лог."""
        line = raw_line.rstrip("\n")
        if not line.strip():
            return
        try:
            message: Any = json.loads(line)
        except ValueError:
            message = None
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            # не JSON-объект протокола — пишем в лог как stderr-строку:
            with self._log_lock:
                log.write(line + "\n")
                log.flush()
            return

        kind = message["type"]
        if kind == "hello":
            pid = message.get("pid")
            if isinstance(pid, int):
                self._store.update_job(job_id, pid=pid)
        elif kind == "progress":
            self._handle_progress(job_id, message, log)
        elif kind == "result":
            # json_path и пр. — для шага 3d; в 3a только сохраняем без вывода
            state.result = message
        elif kind == "error":
            error = message.get("error")
            state.error = str(error) if error else "ошибка подпроцесса"
        elif kind == "done":
            state.done = True
        else:
            with self._log_lock:
                log.write(line + "\n")
                log.flush()

    def _handle_progress(self, job_id: str, message: dict, log: Any) -> None:
        """Обновить progress/message в Store и эмитнуть событие (с троттлингом)."""
        try:
            progress = float(message.get("progress", 0.0))
        except (TypeError, ValueError):
            with self._log_lock:
                log.write(f"битое progress-сообщение: {message!r}\n")
                log.flush()
            return
        text = message.get("message")
        fields: dict[str, Any] = {"progress": progress}
        if isinstance(text, str):
            fields["message"] = text

        now = time.monotonic()
        with self._lock:
            if job_id in self._finalized:
                # задача уже завершена (отмена/stop успели раньше) — её
                # message/progress не трогаем: поздний progress не должен
                # затирать итоговое сообщение финализации.
                return
            self._store.update_job(job_id, **fields)
            last = self._progress_emits.get(job_id)
            throttled = last is not None and now - last < PROGRESS_EMIT_INTERVAL
            if not throttled:
                self._progress_emits[job_id] = now
        if not throttled:
            payload: dict[str, Any] = {"progress": progress}
            if isinstance(text, str):
                payload["message"] = text
            self._emit(job_id, "progress", payload)

    def _pump_stderr(self, proc: subprocess.Popen, log: Any) -> None:
        """Строка stderr → лог-файл; отдельный поток, чтобы процесс не встал."""
        if proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                with self._log_lock:
                    log.write(line)
                    log.flush()
        except (OSError, ValueError):
            return  # лог уже закрыт или процесс убит — не роняем воркер

    # --- отмена и финализация ------------------------------------------------

    def _request_cancel(self, job_id: str) -> None:
        """Флаг отмены в памяти + пустой файл ``<id>.cancel`` для подпроцесса."""
        with self._lock:
            self._cancel_flags.add(job_id)
        cancel_path = self.jobs_dir / f"{job_id}.cancel"
        try:
            cancel_path.parent.mkdir(parents=True, exist_ok=True)
            cancel_path.touch()
        except OSError:
            # файл — подсказка подпроцессу; без него отмена дойдёт до terminate
            pass

    def _finalize(
        self,
        job_id: str,
        status: JobStatus,
        *,
        error: str | None = None,
        message: str | None = None,
        progress: float | None = None,
        detail: str | None = None,
    ) -> bool:
        """Терминальный статус ровно один раз на задачу; True — это были мы.

        Проверка и запись — под ``self._lock``: воркер и cancel() могут
        завершать одну задачу одновременно, событие и статус должны
        прийти ровно один раз. emit — уже вне замка (подписчик вправе
        вызвать ``get``/``list``/``unsubscribe``, а lock не реентерабельный).
        """
        with self._lock:
            if job_id in self._finalized:
                return False
            self._finalized.add(job_id)
            fields: dict[str, Any] = {
                "status": status,
                "finished_at": _utcnow_iso(),
            }
            if error is not None:
                fields["error"] = error
            if message is not None:
                fields["message"] = message
            if progress is not None:
                fields["progress"] = progress
            self._store.update_job(job_id, **fields)

        payload: dict[str, Any] = {"status": status}
        if error is not None:
            payload["error"] = error
        if detail is not None:
            payload["detail"] = detail
        self._emit(job_id, status, payload)
        return True

    def _status_of(self, job_id: str) -> str | None:
        row = self._store.get_job(job_id)
        return str(row["status"]) if row is not None else None

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """terminate с эскалацией до kill, если процесс не вышел."""
        proc.terminate()
        deadline = time.monotonic() + 2.0
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            proc.kill()

    def _emit(self, job_id: str, event_type: str, data: dict) -> None:
        """Разослать событие подписчикам (ошибки подписчика не роняют очередь)."""
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(job_id, event_type, data)
            except Exception:  # noqa: BLE001 - воркер не должен умирать из-за UI
                continue


class _RunState:
    """Что подпроцесс сообщил о себе за один запуск (локально для воркера)."""

    def __init__(self) -> None:
        self.done = False
        self.error: str | None = None
        self.result: dict | None = None


def _utcnow_iso() -> str:
    """Текущий момент ISO-8601 UTC — та же функция, что в ``app.store``.

    Импорт внутри функции, чтобы ``app.jobs`` не тянул sqlite3-модуль на
    уровень собственных импортов (он всё равно нужен только через Store).
    """
    from app.store import utcnow_iso

    return utcnow_iso()


def _default_runner(job: Job, data_dir: Path) -> subprocess.Popen:
    """Запуск реального CLI-подпроцесса (скелет; параметризация — шаг 3d).

    TODO(3d): ``job.params`` превратить в аргументы ``--key value`` (значение-
    список повторяет ``--key`` для каждого элемента) и подключить
    ``--job-json``/``--json-out`` по-настоящему. В 3a этим runner'ом
    ничего не запускается — тесты передают свой.
    """
    argv = [
        sys.executable,
        "-m",
        f"cli.{job.kind}",
        "--job-json",
        "--json-out",
        str(data_dir / "jobs" / f"{job.id}.json"),
    ]
    return subprocess.Popen(  # noqa: S603 - argv собирается из констант и id
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
