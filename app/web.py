"""FastAPI-приложение GapAudit: эндпоинты задач, SSE, пакеты (шаг 3b).

Слой ``app/`` (DEVPLAN §3): модуль знает про ``Store`` и ``JobQueue``, но не
знает про pywebview — окно смотрит только на HTTP.

Что здесь есть на этом шаге:

* **миддлварь** (шаг 0.1, ужесточён): ``Host``/``Origin`` только своей машины;
  токен сессии на всё ``/api/*``, кроме ``/api/health``. Матрица мест, откуда
  берётся токен (см. :func:`_check_token`):

  =============================  ==========================
  Запрос                          Допустимые места токена
  =============================  ==========================
  ``GET /api/*``                  header, cookie, ``?token=``
  ``POST/PUT/DELETE /api/*``      header, cookie
  ``GET /api/health``             без токена
  ``GET /``, ``/static/*``        без токена
  =============================  ==========================

  Query для мутаций запрещен намеренно: ``?token=`` понадобился только
  EventSource (SSE), он умеет читать GET-стрим и не умеет ставить заголовки;
  POST из ``<form>``/``curl`` с токеном в строке запроса — признак того, что
  токен утечёт в логи и в ``Referer``.

* **lifespan** (§8): открывает ``Store``, поднимает ``JobQueue``, кладёт их в
  ``app.state``; на выключении гасит очередь и закрывает БД.

* **эндпоинты** (§7): задачи ``/api/jobs*`` (создание, список, карточка,
  отмена, хвост лога, ``POST /resume`` — заглушка 501), SSE ``/api/stream``,
  пакеты ``/api/packages*``, расширенный ``/api/health``.
* **восстановление после падения** (шаг 3c): lifespan зовёт
  ``JobQueue.recover_interrupted()`` до ``queue.start()`` — задачи,
  работавшие в упавшем приложении, становятся ``interrupted``.

Правило обработчиков (§7): никакой сетевой работы — только чтение SQLite и
очереди; всё долгое уходит в задачу. События ``started`` и ``status`` не
перемешиваются (DEVPLAN §6): SSE отдаёт тип события строкой ``event:``, а
статус — полем ``status`` внутри данных; поток не «чинит» порядок событий
(AGENTS gotcha 24).

Экземпляр ``app`` уровня модуля существует для ``uvicorn app.web:app`` и для
тестов без окна: токен/порт берутся из ``GAPAUDIT_TOKEN``/``GAPAUDIT_PORT``,
настройки — из окружения (``GAPAUDIT_DATA_DIR`` и др., см.
:func:`core.settings.from_env`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.jobs import (
    KIND_PARAM_KEYS,
    KIND_PARAM_TYPES,
    KIND_REQUIRED_KEYS,
    EventCallback,
    Job,
    JobQueue,
    missing_required,
)
from app.store import Store
from core.settings import Settings, from_env, resolve_data_dir

__version__ = "0.1.0"

#: Логгер этого модуля: в ``lifespan`` он сообщает, какие задачи восстановлены
#: после падения приложения (шаг 3c). ``print`` в ``web.py`` запрещён —
#: вывод бэкенда принадлежит uvicorn'у, а мы пишем через logging.
logger = logging.getLogger(__name__)

__all__ = [
    "GhOkCache",
    "KIND_PARAM_KEYS",
    "KIND_PARAM_TYPES",
    "KIND_REQUIRED_KEYS",
    "LOG_TAIL_BYTES",
    "LocalGuardMiddleware",
    "SSE_HEARTBEAT_INTERVAL",
    "TOKEN_COOKIE",
    "TOKEN_HEADER",
    "TOKEN_QUERY",
    "VERSION",
    "app",
    "create_app",
]

VERSION = __version__
TOKEN_HEADER = "X-GapAudit-Token"
TOKEN_COOKIE = "gapaudit_token"
TOKEN_QUERY = "token"

TOKEN_ENV = "GAPAUDIT_TOKEN"
PORT_ENV = "GAPAUDIT_PORT"

HEALTH_PATH = "/api/health"
STREAM_PATH = "/api/stream"

#: Хвост лога задачи, отдаваемый ``GET /api/jobs/{id}/log``.
LOG_TAIL_BYTES = 10_000

#: Пауза SSE-heartbeat. Тесты опускают константу в 0.1 с вместо реальных 15.
SSE_HEARTBEAT_INTERVAL = 15.0

#: TTL кэша ``gh auth status`` в /api/health: проверка — подпроцесс, на каждый
#: запрос её гонять нельзя (§7: в обработчиках только чтение из SQLite/кэша).
GH_OK_TTL_SECONDS = 60.0

ROOT = Path(__file__).resolve().parent.parent
VIEWS_DIR = ROOT / "web" / "views"
STATIC_DIR = ROOT / "web" / "static"

_ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Валидация ``params`` строится по таблицам из ``app/jobs.py`` — единый источник
#: правды: ``KIND_PARAM_KEYS`` (какие ключи допустимы), ``KIND_REQUIRED_KEYS``
#: (обязательные) и ``KIND_PARAM_TYPES`` (типы значений). Локального списка
#: kind в этом модуле нет намеренно (шаг 3d-b): два описания «валидной задачи»
#: расходились — API отвечал 202, а задача падала с ``missing param``.


# --- проверки окружения (миддлварь) ------------------------------------------


def _host_allowed(host: str, port: int | None) -> bool:
    """``Host`` должен быть своей машиной. Порт проверяем, если он нам известен.

    Это защита от DNS-rebinding: чужая страница в браузере может обратиться к
    ``127.0.0.1``, но её ``Host`` будет именем её домена — такой запрос отбиваем.
    """
    host = (host or "").strip().lower()
    if not host:
        return False
    hostname, _, host_port = host.partition(":")
    if hostname not in _ALLOWED_HOSTNAMES:
        return False
    if port is None:
        return True
    return host_port == str(port)


def _origin_allowed(origin: str, port: int | None) -> bool:
    if not origin:
        return False
    prefix, _, rest = origin.lower().partition("://")
    if prefix not in ("http", "https"):
        return False
    return _host_allowed(rest, port)


def _check_token(request: Request, token: str) -> str | None:
    """``None`` — токен принят; строка — причина для 403 (см. матрицу в docstring).

    Функция возвращает причину, а не бросает ``HTTPException``: она вызывается
    из ``BaseHTTPMiddleware``, чьи исключения уходят мимо обработчиков
    FastAPI и превратились бы в 500 вместо 403.
    """
    supplied: list[str | None] = [
        request.headers.get(TOKEN_HEADER),
        request.cookies.get(TOKEN_COOKIE),
    ]
    if request.method not in _MUTATING_METHODS:
        supplied.append(request.query_params.get(TOKEN_QUERY))
    if any(value and secrets.compare_digest(value, token) for value in supplied):
        return None
    return "нужен токен сессии"


class LocalGuardMiddleware(BaseHTTPMiddleware):
    """Пускать только запросы с этой машины и с валидным токеном на ``/api/*``."""

    def __init__(self, app, token: str, port: int | None = None) -> None:
        super().__init__(app)
        self.token = token
        self.port = port

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not _host_allowed(request.headers.get("host", ""), self.port):
            return JSONResponse(
                {"ok": False, "detail": "forbidden: недопустимый Host"}, status_code=403
            )
        origin = request.headers.get("origin")
        if origin and not _origin_allowed(origin, self.port):
            return JSONResponse(
                {"ok": False, "detail": "forbidden: недопустимый Origin"}, status_code=403
            )
        path = request.url.path
        if path.startswith("/api/") and path != HEALTH_PATH:
            problem = _check_token(request, self.token)
            if problem is not None:
                return JSONResponse(
                    {"ok": False, "detail": f"forbidden: {problem}"}, status_code=403
                )
        return await call_next(request)


# --- здоровье ----------------------------------------------------------------


class GhOkCache:
    """``gh auth status`` с кэшем: один запрос не чаще, чем раз в ``ttl`` секунд.

    ``check`` — как проверять (по умолчанию :func:`_default_gh_check`); ``clock``
    подменяется в тестах, чтобы не ждать минуту. Исключение проверяющего
    колбэка — не ошибка health: ``gh_ok`` становится ``None`` («неизвестно») и
    тоже кэшируется.
    """

    def __init__(
        self,
        check: Callable[[], bool] | None = None,
        *,
        ttl: float = GH_OK_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check = check
        self._ttl = float(ttl)
        #: публично — тесты сдвигают время, а не спят
        self.clock = clock
        self._value: bool | None = None
        self._checked_at: float | None = None
        self._lock = threading.Lock()

    def value(self) -> bool | None:
        """Текущее значение, из кэша; при устаревании — новый вызов ``gh``."""
        now = self.clock()
        with self._lock:
            if self._checked_at is not None and now - self._checked_at < self._ttl:
                return self._value
        checker = self._check if self._check is not None else _default_gh_check
        try:
            fresh: bool | None = bool(checker())
        except Exception:  # noqa: BLE001 - health не должен падать из-за gh
            fresh = None
        with self._lock:
            self._value = fresh
            self._checked_at = now
        return fresh


def _default_gh_check() -> bool:
    """Авторизован ли ``gh`` (подпроцесс ``gh auth status``)."""
    from core.github import GhClient  # ленивый импорт: subprocess на старте не нужен

    authorized, _text = GhClient().auth_status()
    return authorized


# --- контракты запросов ------------------------------------------------------


class JobSubmit(BaseModel):
    """Тело ``POST /api/jobs``: ``kind`` + словарь ``params``."""

    kind: str
    params: dict[str, Any] = {}


def _type_ok(value: Any, expected: type | tuple[type, ...]) -> bool:
    """Поверхностная проверка типа: ``bool`` — не ``int`` для наших целей."""
    if expected is int and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def _param_errors(kind: str, params: dict[str, Any]) -> list[str]:
    """Валидация ``params`` по таблицам ``app/jobs.py``: ключи, типы, обязательные.

    Fail fast: задача, которая заведомо упадёт в ``_params_to_argv``, не должна
    получить 202 и умереть в ``failed`` через секунду. Порядок сообщений —
    стабильный (ключи в порядке запроса, затем недостающие по алфавиту).
    """
    allowed = KIND_PARAM_KEYS[kind]
    types = KIND_PARAM_TYPES[kind]
    errors: list[str] = []
    for key, value in params.items():
        if key not in allowed:
            errors.append(f"неизвестный параметр {key!r}")
            continue
        expected = types.get(key)
        if expected is not None and not _type_ok(value, expected):
            errors.append(f"параметр {key!r} должен быть {expected!r}")
    for key in missing_required(kind, params):
        errors.append(f"обязательный параметр {key!r} отсутствует или пуст")
    return errors


def _job_public(job: Job) -> dict[str, Any]:
    """Публичная проекция задачи: без ``log_path`` и ``pid`` (внешний каталог)."""
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
    }


def _store(request: Request) -> Store:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="хранилище не открыто (lifespan)")
    return store


def _queue(request: Request) -> JobQueue:
    queue = getattr(request.app.state, "queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="очередь не запущена (lifespan)")
    return queue


def _tail(path: Path, size: int = LOG_TAIL_BYTES) -> bytes:
    """Последние ``size`` байт файла; файла нет — пусто."""
    try:
        total = path.stat().st_size
        with path.open("rb") as handle:
            if total > size:
                handle.seek(total - size)
            return handle.read()
    except OSError:
        return b""


# --- сборка приложения -------------------------------------------------------


def create_app(
    token: str | None = None,
    port: int | None = None,
    *,
    settings: Settings | None = None,
    runner: Callable[[Job, Path], Any] | None = None,
    gh_check: Callable[[], bool] | None = None,
) -> FastAPI:
    """Собрать приложение: токен/порт контура, настройки, runner задач.

    ``runner`` — как запускать подпроцесс задачи (по умолчанию
    ``JobQueue``-стандартный ``_default_runner``; тесты передают фейк).
    ``gh_check`` — чем проверять ``gh`` в health (тесты передают фейк, чтобы
    в сюите не заводился subprocess).
    """
    token = token or os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(32)
    if port is None:
        raw_port = os.environ.get(PORT_ENV)
        port = int(raw_port) if raw_port and raw_port.isdigit() else None
    resolved_settings = settings if settings is not None else from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        data_dir = resolve_data_dir(resolved_settings)
        data_dir.mkdir(parents=True, exist_ok=True)
        store = Store(data_dir / "gapaudit.db")
        store.__enter__()
        queue = JobQueue(
            store, data_dir=data_dir, settings=resolved_settings, runner=runner
        )
        # Задачи, работавшие в упавшем приложении, — interrupted: это видно
        # в GET /api/jobs, а в лог уходит список id (шаг 3c). До queue.start():
        # восстановление не должно гоняться параллельно с воркерами.
        recovered = queue.recover_interrupted()
        if recovered:
            logger.info(
                "восстановлено прерванных задач: %d (%s)",
                len(recovered),
                ", ".join(recovered),
            )
        queue.start()
        application.state.store = store
        application.state.queue = queue
        application.state.data_dir = str(data_dir)
        try:
            yield
        finally:
            queue.stop(timeout=5.0)
            store.close()

    application = FastAPI(title="GapAudit", version=VERSION, lifespan=lifespan)
    application.state.token = token
    application.state.port = port
    application.state.settings = resolved_settings
    application.state.gh_status = GhOkCache(check=gh_check)
    application.add_middleware(LocalGuardMiddleware, token=token, port=port)

    templates = Jinja2Templates(directory=str(VIEWS_DIR))
    if STATIC_DIR.is_dir():
        # Локальная статика вместо CDN (правило из плана: никаких внешних CDN).
        application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @application.get(HEALTH_PATH)
    async def health(request: Request) -> dict[str, Any]:
        """Готовность бэкенда: по этому ответу окно решает, что можно открываться.

        Без токена (его ещё нет, когда окно только стартует), но внутри
        контура Host/Origin. ``gh_ok`` — из кэша, не подпроцесс на каждый
        пинг; ``None`` — проверить не удалось.
        """
        state = request.app.state
        store = getattr(state, "store", None)
        return {
            "ok": True,
            "version": VERSION,
            "gh_ok": state.gh_status.value(),
            "db_ok": bool(store.ping()) if store is not None else False,
            "data_dir": str(getattr(state, "data_dir", "")),
        }

    # --- задачи ---------------------------------------------------------------

    @application.post("/api/jobs", status_code=202)
    async def submit_job(payload: JobSubmit, request: Request) -> dict[str, str]:
        """Создать задачу: ``{"kind", "params"}`` → ``202 {"job_id", "status"}``."""
        if payload.kind not in KIND_PARAM_KEYS:
            raise HTTPException(
                status_code=400, detail=f"неизвестный kind: {payload.kind!r}"
            )
        errors = _param_errors(payload.kind, payload.params)
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        job_id = _queue(request).submit(payload.kind, payload.params)
        return {"job_id": job_id, "status": "queued"}

    @application.get("/api/jobs")
    async def list_jobs(
        request: Request,
        status: str | None = None,
        limit: int = Query(100, ge=1, le=10_000),
    ) -> dict[str, Any]:
        """Список задач ``created_at DESC``; ``status`` — точный фильтр."""
        jobs = _queue(request).list(status=status, limit=limit)
        return {"jobs": [_job_public(job) for job in jobs]}

    @application.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> dict[str, Any]:
        """Карточка задачи: те же поля, что в списке, плюс ``params``."""
        job = _queue(request).get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"задача {job_id} не найдена")
        payload = _job_public(job)
        payload["params"] = job.params
        return payload

    @application.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request) -> dict[str, bool]:
        """Отмена: ``{"cancelled": bool}``; неизвестная задача — 404.

        ``False`` — задача уже в терминальном статусе (отменять нечего).
        """
        queue = _queue(request)
        if queue.get(job_id) is None:
            raise HTTPException(status_code=404, detail=f"задача {job_id} не найдена")
        return {"cancelled": queue.cancel(job_id)}

    @application.post("/api/jobs/{job_id}/resume")
    async def resume_job(job_id: str, request: Request) -> dict[str, Any]:
        """Заглушка шага 3c: продолжение прерванной задачи ещё не работает.

        ``501`` — честный ответ для кнопки «Продолжить» из ``index.html``:
        сам метод :meth:`JobQueue.resume_interrupted` уже есть, а вызывать
        его из HTTP будем в шаге 3d, там же, где реальные ``cli/*`` через
        ``--job-json`` (до тех пор продолжать нечего: реальных прерванных
        задач приложение ещё не умеет выполнять).
        """
        raise HTTPException(
            status_code=501,
            detail="продолжение задачи появится в шаге 3d (реальные cli-задачи)",
        )

    @application.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
    async def job_log(job_id: str, request: Request) -> str:
        """Хвост лога (последние 10 000 байт) как ``text/plain``.

        Задача есть, а файла лога ещё нет (стояла в очереди) — пустой ответ
        ``200``: 404 зарезервирован за «задача не найдена».
        """
        job = _queue(request).get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"задача {job_id} не найдена")
        if not job.log_path:
            return ""
        return _tail(Path(job.log_path)).decode("utf-8", errors="replace")

    # --- SSE ------------------------------------------------------------------

    @application.get(STREAM_PATH)
    async def stream(request: Request) -> StreamingResponse:
        """SSE-канал событий очереди (DEVPLAN §7).

        ``EventCallback`` вызывается в воркер-потоке, поэтому переход в
        asyncio-цикл — через ``loop.call_soon_threadsafe`` и очередь. Ответ
        без буферизации (``X-Accel-Buffering: no``), heartbeat — комментарием
        ``: keep-alive``, чтобы прокси и браузер не закрыли молчаливый канал.

        Порядок событий не нормализуется: ``started`` после ``done`` — это два
        разных события, «чинить» их здесь нельзя (AGENTS gotcha 24).
        """
        queue = _queue(request)
        loop = asyncio.get_running_loop()
        inbox: asyncio.Queue[tuple[str, str, dict]] = asyncio.Queue()

        def on_event(job_id: str, event_type: str, data: dict) -> None:
            # воркер-поток → цикл: иначе нетронутый loop из чужого потока
            loop.call_soon_threadsafe(inbox.put_nowait, (job_id, event_type, data))

        # подписка до отправки заголовков: клиент не пропустит события,
        # пришедшие сразу после подключения.
        queue.subscribe(on_event)
        try:
            body = _event_stream(queue, on_event, inbox)
            return StreamingResponse(
                body,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )
        except BaseException:
            queue.unsubscribe(on_event)
            raise

    # --- пакеты ---------------------------------------------------------------

    @application.get("/api/packages")
    async def list_packages(
        request: Request,
        limit: int = Query(100, ge=1, le=10_000),
        only_with_observations: bool = False,
    ) -> dict[str, Any]:
        """Список пакетов из ``packages`` (read-only, без изменений Store)."""
        packages = _store(request).list_packages(
            limit=limit, only_with_observations=only_with_observations
        )
        return {"packages": packages}

    @application.get("/api/packages/{name}")
    async def get_package(name: str, request: Request) -> dict[str, Any]:
        """Карточка: строка пакета, последнее наблюдение, его события.

        Фильтр событий по имени делается здесь, а не в Store: у
        ``Store.get_events`` нет параметра ``name``, и менять Store ради
        одного фильтра (шаг 9) не нужно.
        """
        store = _store(request)
        package = store.get_package(name)
        if package is None:
            raise HTTPException(status_code=404, detail=f"пакет {name} не найден")
        events = [
            event
            for event in store.get_events()
            if str(event.get("name")) == name
        ]
        return {
            "package": package,
            "observations": store.get_latest_observations([name]),
            "events": events,
        }

    @application.get("/api/packages/{name}/history")
    async def package_history(
        name: str,
        request: Request,
        limit: int = Query(50, ge=1, le=10_000),
    ) -> dict[str, Any]:
        """История наблюдений имени, новые сверху (для мини-графика)."""
        store = _store(request)
        if store.get_package(name) is None:
            raise HTTPException(status_code=404, detail=f"пакет {name} не найден")
        return {
            "name": name,
            "observations": store.get_observations_history(name, limit=limit),
        }

    # --- экраны ---------------------------------------------------------------

    @application.get("/")
    async def index(request: Request):
        """Единственный экран каркаса: подтверждение, что связка живая."""
        return templates.TemplateResponse(
            request=request, name="index.html", context={"version": VERSION}
        )

    return application


async def _event_stream(
    queue: JobQueue,
    callback: EventCallback,
    inbox: "asyncio.Queue[tuple[str, str, dict]]",
) -> AsyncIterator[str]:
    """Генератор SSE: события из ``inbox`` + heartbeat; отписка на закрытии."""
    try:
        while True:
            try:
                job_id, event_type, data = await asyncio.wait_for(
                    inbox.get(), timeout=SSE_HEARTBEAT_INTERVAL
                )
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            payload = {"job_id": job_id, **data}
            yield (
                f"event: {event_type}\n"
                f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            )
    finally:
        queue.unsubscribe(callback)


app = create_app()
