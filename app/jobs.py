"""Очередь задач GapAudit: подпроцессы, статусы, отмена, логи (P1, DEVPLAN §6).

Слой ``app/`` (карта проекта — DEVPLAN §3): модуль знает про Store и про
подпроцесс, но ничего не знает про FastAPI — для ``app/web.py`` (шаг 3b)
достаточно :meth:`JobQueue.subscribe` (SSE подпишется на события).

Контракт (DEVPLAN §6):

* статусы ``queued → running → done | failed | cancelled``; ``interrupted``
  — задача работала, когда приложение упало: её переводит
  :meth:`JobQueue.recover_interrupted` на старте (шаг 3c), продолжает
  :meth:`JobQueue.resume_interrupted`;
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
            (whitelist ``kind`` → ``python -m cli.<модуль>`` + params-флаги,
            шаг 3d-a; тесты 3a передают свой runner).
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

    def recover_interrupted(self) -> list[str]:
        """Восстановить задачи, работавшие в упавшем приложении (шаг 3c).

        Все jobs со ``status='running'`` и непустым ``started_at`` — задачи,
        которые работали, когда приложение упало: воркеры умерли вместе с
        процессом, подпроцессы — почти наверняка тоже. Они переводятся в
        ``interrupted`` (а не ``failed``: это не ошибка задачи, это остановка
        приложения), и пользователь решает, продолжать ли их.

        ``running`` без ``started_at`` не трогаем: такого быть не должно, а
        молча менять чужие данные мы не вправе.

        Возвращает список восстановленных ``job_id``. Вызывается из
        lifespan (``app/web.py``) **до** :meth:`start` — из ``__init__``
        не вызывается: конструктор не должен менять хранилище.
        """
        recovered: list[str] = []
        with self._lock:
            for row in self._store.list_jobs(status="running"):
                if not row.get("started_at"):
                    continue
                job_id = str(row["id"])
                self._store.update_job(job_id, status="interrupted")
                recovered.append(job_id)
        return recovered

    def resume_interrupted(self, job_id: str) -> bool:
        """Продолжить ``interrupted``-задачу; True — задача поставлена в очередь.

        * задача не найдена или не в ``interrupted`` → ``False`` (продолжать
          нечего и/или нечего продолжать);
        * ``log_path`` пуст или файла нет → ``False`` (лог прошлого запуска —
          свидетельство, что запуск вообще был, а пустой путь означает
          задачу, стоявшую в очереди: её и статус был бы ``queued``);
        * дальше: сброс полей прошлого запуска (``started_at``/``finished_at``/
          ``error``/``progress``/``message``) → ``queued``, следы прошлого
          запуска в памяти очереди стираются, файл ``<id>.cancel`` удаляется
          (отмена, не дошедшая до конца перед падением, не должна убить
          возобновлённую задачу), задача кладётся в рабочий deque.

        API-эндпоинт ``POST /api/jobs/{id}/resume`` -> ``app.web.resume_job``
        (шаг 3d-b). Возвращает 200 если resumed, 404 если job нет,
        409 если не interrupted или лог отсутствует.
        """
        row = self._store.get_job(job_id)
        if row is None or row["status"] != "interrupted":
            return False
        log_path = str(row.get("log_path") or "")
        if not log_path or not Path(log_path).exists():
            return False
        with self._lock:
            # следы прошлого запуска в памяти: финализация/отмена не должны
            # блокировать новый старт (иначе _run_job увидит _finalized и
            # тихо откатит задачу в cancelled).
            self._finalized.discard(job_id)
            self._cancel_flags.discard(job_id)
            self._progress_emits.pop(job_id, None)
            self._store.update_job(
                job_id,
                status="queued",
                error=None,
                started_at=None,
                finished_at=None,
                progress=0.0,
                message="",
            )
        try:
            (self.jobs_dir / f"{job_id}.cancel").unlink(missing_ok=True)
        except OSError:
            pass  # файла и не было — это норма, а блокировать возобновление нельзя
        self._enqueue(job_id)
        return True

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

    def _enqueue(self, job_id: str) -> None:
        """Положить id в рабочий deque и разбудить воркеров (под ``_cond``)."""
        with self._cond:
            if job_id not in self._queue:
                self._queue.append(job_id)
                self._cond.notify()

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


#: Корень проекта: ``python -m cli.*`` требует его в ``sys.path`` подпроцесса.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: kind задачи -> модуль CLI. Единственное место, где ``kind`` превращается в
#: имя модуля: значение из запроса только сравнивается с ключами таблицы
#: (см. :func:`_default_runner`).
_KIND_TO_MODULE: dict[str, str] = {
    "packages.check": "cli.check_predecessors",
    "catalog.build": "cli.catalog_build",
    "registry.scan": "cli.catalog_scan",
    "github.verify": "cli.verify",
    "monitor.diff": "cli.check_predecessors",
    "signals.collect": "cli.signals",
}

#: Whitelist ключей ``params`` на каждый kind — ЕДИНСТВЕННЫЙ источник правды о
#: том, какая задача валидна. По этой таблице ``app/web.py`` строит 400 на
#: ``POST /api/jobs`` (fail fast), а :func:`_params_to_argv` — argv подпроцесса.
KIND_PARAM_KEYS: dict[str, frozenset[str]] = {
    "packages.check": frozenset({"names", "keyword"}),
    "catalog.build": frozenset({"phrases", "min_stars", "pages", "sources"}),
    "registry.scan": frozenset({"base_url", "limit", "max_pages"}),
    "github.verify": frozenset({"targets", "keyword"}),
    "monitor.diff": frozenset({"diff_from"}),
    "signals.collect": frozenset({"package", "kinds"}),
}

#: Обязательные ключи: без них задача бессмысленна. Проверяется и в API (400),
#: и в runner'е (``ValueError``) — одной функцией :func:`missing_required`.
#: Пустой список/строка для обязательного ключа = «не передано»
#: (:func:`missing_required`). Если когда-нибудь понадобится ``names: []`` как
#: валидное пустое — потребуется ``KIND_ALLOW_EMPTY_KEYS``.
KIND_REQUIRED_KEYS: dict[str, frozenset[str]] = {
    "packages.check": frozenset({"names"}),
    "catalog.build": frozenset({"phrases"}),
    "registry.scan": frozenset(),
    "github.verify": frozenset({"targets"}),
    "monitor.diff": frozenset({"diff_from"}),
    "signals.collect": frozenset({"package"}),
}

#: Типы значений ``params`` — для той же валидации в API (неизвестный ключ
#: отбивает :data:`KIND_PARAM_KEYS`, тип — эта таблица). ``list`` — любой
#: JSON-массив, элементы проверяет сам CLI.
KIND_PARAM_TYPES: dict[str, dict[str, type | tuple[type, ...]]] = {
    "packages.check": {"names": list, "keyword": list},
    "catalog.build": {
        "phrases": list,
        "min_stars": int,
        "pages": int,
        "sources": list,
    },
    "registry.scan": {"base_url": str, "limit": int, "max_pages": int},
    "github.verify": {"targets": list, "keyword": str},
    "monitor.diff": {"diff_from": str},
    "signals.collect": {"package": str, "kinds": list},
}

#: Источники каталога по умолчанию: отсутствие в ``params["sources"]`` даёт
#: ``--no-npm``/``--no-registry`` (CLI включает всё, кроме явно выключенного).
_DEFAULT_SOURCES: tuple[str, ...] = ("github", "npm", "registry")


def missing_required(kind: str, params: dict) -> list[str]:
    """Какие обязательные ключи отсутствуют или пусты — список имён (пусто — всё есть).

    Пустое значение считается отсутствующим: ``names: []`` или ``diff_from: ""``
    задачу не запускают, поэтому и API, и runner должны ответить одинаково.
    """
    missing: list[str] = []
    for key in sorted(KIND_REQUIRED_KEYS.get(kind, frozenset())):
        value = params.get(key)
        if value is None:
            missing.append(key)
        elif isinstance(value, str) and not value.strip():
            missing.append(key)
        elif isinstance(value, (list, tuple, dict, set)) and not value:
            missing.append(key)
    return missing


def _str_list(value: Any) -> list[str]:
    """Список строк из значения ``params`` (``None``/мусор — пустой список)."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _params_to_argv(kind: str, params: dict) -> list[str]:
    """Whitelist params -> argv. Любой неизвестный ключ — ``ValueError``.

    Таблица соответствия — :data:`KIND_PARAM_KEYS` (ключи), :data:`KIND_REQUIRED_KEYS`
    (обязательные) и :data:`KIND_PARAM_TYPES` (типы для API):

    * ``packages.check``: ``names`` (обязателен, позиционные), ``keyword``
      (список -> ``--keyword=``; пусто — CLI возьмёт ``Settings.keywords``);
    * ``catalog.build``: ``phrases`` (обязателен, позиционные), ``min_stars``,
      ``pages``, ``sources`` (``npm``/``registry`` вне списка -> ``--no-*``);
    * ``registry.scan``: ``base_url``, ``limit``, ``max_pages``;
    * ``github.verify``: ``targets`` (обязателен, позиционные), ``keyword``;
    * ``monitor.diff``: ``diff_from`` (обязателен -> ``--diff=``);
    * ``signals.collect``: ``package`` (обязателен) и ``kinds`` (список;
      пусто — без ``--kind``, все виды возьмёт сам CLI: ``SIGNAL_KINDS``,
      в P5d — ``jobs``, ``patents``, ``funding`` и ``search_interest``).
      Подкоманда ``collect`` идёт ПЕРЕД флагами — так устроен argparse
      ``cli.signals``. Белый список видов здесь не дублируется: его проверяет
      ``POST /api/signals/collect`` по ``SIGNAL_COLLECT_KINDS`` (gotcha 54).

    Ошибки — только ``ValueError`` и только до ``subprocess``: задача,
    которая заведомо не запустится, не должна создавать процесс. Обязательные
    ключи проверяет :func:`missing_required` — та же функция, что в API.
    """
    if kind not in _KIND_TO_MODULE:
        raise ValueError(f"неизвестный kind: {kind!r}")
    schema = KIND_PARAM_KEYS[kind]
    for key in params:
        if key not in schema:
            raise ValueError(f"unknown param for {kind}: {key}")
    missing = missing_required(kind, params)
    if missing:
        raise ValueError(f"missing param for {kind}: {missing[0]}")

    def positional(key: str) -> list[str]:
        """Позиционные аргументы CLI из обязательного списка ``params``."""
        return _str_list(params.get(key))

    if kind == "packages.check":
        argv = positional("names")
        argv += [f"--keyword={word}" for word in _str_list(params.get("keyword"))]
        return argv

    if kind == "catalog.build":
        argv = positional("phrases")
        if "min_stars" in params:
            argv.append(f"--min-stars={int(params['min_stars'])}")
        if "pages" in params:
            argv.append(f"--pages={int(params['pages'])}")
        sources = _str_list(params.get("sources")) or list(_DEFAULT_SOURCES)
        if "npm" not in sources:
            argv.append("--no-npm")
        if "registry" not in sources:
            argv.append("--no-registry")
        return argv

    if kind == "registry.scan":
        argv = []
        if "base_url" in params:
            argv.append(f"--base-url={params['base_url']}")
        if "limit" in params:
            argv.append(f"--limit={int(params['limit'])}")
        if "max_pages" in params:
            argv.append(f"--max-pages={int(params['max_pages'])}")
        return argv

    if kind == "github.verify":
        argv = positional("targets")
        if "keyword" in params:
            argv.append(f"--keyword={params['keyword']}")
        return argv

    if kind == "signals.collect":
        # Подкоманда до флагов: python -m cli.signals collect --package X ...
        argv = ["collect", "--package", str(params["package"])]
        # Пустой kinds — вообще без --kind: набор «всё, что умеет CLI»
        # решает cli.signals.SIGNAL_KINDS. Здесь список видов не дублируем
        # (в P5a жил жёсткий default ["jobs"] — после P5b он стал бы третьим
        # местом правды рядом с SIGNAL_KINDS и SIGNAL_COLLECT_KINDS).
        for signal_kind in _str_list(params.get("kinds")):
            argv += ["--kind", signal_kind]
        return argv

    # monitor.diff — единственный оставшийся kind: сравнение с прошлым прогоном.
    return [f"--diff={params['diff_from']}"]


def _default_runner(job: Job, data_dir: Path) -> subprocess.Popen:
    """Запуск реального CLI-подпроцесса по whitelist-таблице (шаг 3d-a).

    ``argv = [python, -m, _KIND_TO_MODULE[kind], --job-json,
    --json-out=<data_dir>/jobs/<id>.result.json, *params_argv]``.

    ``kind`` НИКОГДА не подставляется в имя модуля: он лишь ключ таблицы
    :data:`_KIND_TO_MODULE`. Неизвестный ``kind`` — ``ValueError`` до
    ``subprocess.Popen`` (иначе ``kind="monitor.diff"`` давал бы
    ``python -m cli.monitor.diff``, а чужой ``kind`` с точками/пробелами — RCE).

    ``cwd`` — корень проекта: ``-m cli.*`` требует его в ``sys.path``,
    независимо от того, откуда запущено приложение.
    """
    module = _KIND_TO_MODULE.get(job.kind)
    if module is None:
        raise ValueError(f"неизвестный kind: {job.kind!r}")
    params_argv = _params_to_argv(job.kind, job.params)
    result_path = Path(data_dir) / "jobs" / f"{job.id}.result.json"
    argv = [
        sys.executable,
        "-m",
        module,
        "--job-json",
        f"--json-out={result_path}",
        *params_argv,
    ]
    return subprocess.Popen(  # noqa: S603 - argv собирается из констант и id
        argv,
        cwd=str(_PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
