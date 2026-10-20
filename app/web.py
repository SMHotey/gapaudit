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

  HTML-страницы (шаг 3a): запрос с валидным ``?token=`` получает cookie
  ``gapaudit_token`` (HttpOnly, SameSite=Strict, сессионная) — дальше вся
  навигация работает без query, потому что ни браузер, ни WebView2 не
  переносят `?token=` при клике по ссылке (AGENTS gotcha 40).

  Query для мутаций запрещен намеренно: ``?token=`` понадобился только
  EventSource (SSE), он умеет читать GET-стрим и не умеет ставить заголовки;
  POST из ``<form>``/``curl`` с токеном в строке запроса — признак того, что
  токен утечёт в логи и в ``Referer``.

* **lifespan** (§8): открывает ``Store``, поднимает ``JobQueue``, кладёт их в
  ``app.state``; на выключении гасит очередь и закрывает БД.

* **эндпоинты** (§7): задачи ``/api/jobs*`` (создание, список, карточка,
  отмена, хвост лога, ``POST /resume`` — реальное продолжение
  ``interrupted``-задачи, шаг 3d-b), SSE ``/api/stream``, пакеты
  ``/api/packages*`` (шаг 3a: ``/search`` — поиск с пагинацией),
  события ``/api/events`` (шаг 3a: таймлайн «Мониторинга»),
  каталоги ``/api/catalogs*`` (шаг 3b: сборка по фразам, список, элементы с
  фильтрами, экспорт CSV/Markdown),
  матрица ``/api/matrix`` (шаг 2b — агрегат экрана
  «Сейчас решать»), сравнение ``/api/compare`` (шаг 3c — 2–3 пакета бок о
  бок по последнему замеру) и выгрузки ``POST /api/events/export``,
  ``POST /api/matrix/export``, ``POST /api/compare/export`` (шаг 3c: файл в
  ответе, CSV — ``utf-8-sig``), настройки ``GET/PUT /api/settings`` (шаг 2c —
  пороги и ключевые слова, запись в ``<data_dir>/settings.json``),
  ключи внешних API ``GET/PUT /api/settings/api-keys``,
  ``POST /api/settings/api-keys/{provider}/check`` (заглушка P4a),
  ``DELETE /api/settings/api-keys/{provider}`` (шаг P4a — файл
  ``<data_dir>/api_keys.json``, наружу только статус),
  расширенный ``/api/health``.
* **восстановление после падения** (шаг 3c): lifespan зовёт
  ``JobQueue.recover_interrupted()`` до ``queue.start()`` — задачи,
  работавшие в упавшем приложении, становятся ``interrupted``.
* **запись результатов по завершении задачи** (шаги 3d-b и 3b): lifespan
  подписан на события очереди; на ``done`` он по ``kind`` выбирает обработчик
  (таблица ``_POST_PROCESS_KINDS`` внутри lifespan) и читает
  ``<data_dir>/jobs/<id>.result.json`` (файл, который CLI записал по
  ``--json-out``):

  =================  ====================================================
  kind                что ложится в БД
  =================  ====================================================
  ``packages.check``  строки → ``Store.record_observations``
  ``monitor.diff``    ``{"events": [...]}`` → ``Store.record_events``
  ``catalog.build``   ``{"queries","stats","items"}`` → ``Store.record_catalog``
                      + ``Store.record_catalog_items``
  =================  ====================================================

  Подписчик вызывается в потоке воркера под ``_emit``, поэтому сам он ничего
  не читает с диска и не пишет в БД — только ставит задачу в
  ``ThreadPoolExecutor(max_workers=1)``. Порядок выключения (важен): отписка от
  очереди → ``post_executor.shutdown(wait=True)`` → ``queue.stop()`` →
  ``store.close()``, иначе последняя завершённая задача может не успеть
  записать результат в уже закрытый ``Store``.

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
import csv
import io
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
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
from core.api_keys import META_FIELDS, PROVIDERS, ApiKeysStore
from core.collectors.cross_ecosystem import compute_cross_ecosystem
from core.collectors.momentum import compute_momentum
from core.settings import (
    SETTINGS_FILENAME,
    Settings,
    from_env,
    load as load_settings,
    resolve_data_dir,
    save as save_settings,
    to_dict as settings_to_dict,
)

__version__ = "0.1.0"

#: Логгер этого модуля: в ``lifespan`` он сообщает, какие задачи восстановлены
#: после падения приложения (шаг 3c). ``print`` в ``web.py`` запрещён —
#: вывод бэкенда принадлежит uvicorn'у, а мы пишем через logging.
logger = logging.getLogger(__name__)

__all__ = [
    "EDITABLE_SETTING_KEYS",
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

#: Границы пагинации ``GET /api/events`` (шаг 3a). Верх — не «на всякий
#: случай»: экран листает по 50, а 500 — потолок для внешнего клиента,
#: которому нужен разовый срез без постраничного обхода.
EVENTS_LIMIT_DEFAULT = 100
EVENTS_LIMIT_MAX = 500

#: Границы пагинации ``GET /api/packages/search`` (шаг 3a). Ниже, чем у
#: событий: строка пакета несёт summary, и ответ на 500 строк — это уже
#: десятки килобайт на один поисковый запрос с клавиатуры.
PACKAGES_SEARCH_LIMIT_DEFAULT = 50
PACKAGES_SEARCH_LIMIT_MAX = 200

#: Границы списка каталогов и их элементов (шаг 3b). Элементы — 100 на
#: страницу, как и у остальных экранов: строка каталога несёт описание.
CATALOGS_LIMIT_DEFAULT = 100
CATALOGS_LIMIT_MAX = 500
CATALOG_ITEMS_LIMIT_DEFAULT = 100
CATALOG_ITEMS_LIMIT_MAX = 500

#: Источники сборки каталога — те же три, что знает ``core.catalog``.
#: Пустой список — ошибка, а не «все»: `POST /api/jobs` трактует отсутствие
#: ``sources`` как «все источники», и явная пустота означала бы разное в двух
#: слоях (AGENTS gotcha 41).
CATALOG_SOURCES: tuple[str, ...] = ("github", "npm", "registry")

#: Границы экспорта событий (шаг 3c). Порог не «на всякий случай»: выгрузка —
#: это весь таймлайн, но ответ на 500 тысяч строк убил бы и SQLite, и окно.
#: 10 000 по умолчанию — с запасом на год мониторинга десятка пакетов.
EVENTS_EXPORT_LIMIT_DEFAULT = 10_000
EVENTS_EXPORT_LIMIT_MAX = 50_000

#: Сколько ниш сравнивает экран «Сравнение» (шаг 3c): меньше двух — не сравнение,
#: больше трёх — не влезает в окно без горизонтальной прокрутки.
COMPARE_NAMES_MIN, COMPARE_NAMES_MAX = 2, 3

#: Фразы и страницы на фразу (шаг 3b). Потолки не «на всякий случай»: 10 фраз
#: по 10 страниц — это 300 запросов к GitHub Search при лимите 30/мин, то есть
#: заведомо бессмысленная задача на час.
CATALOG_PHRASES_MIN, CATALOG_PHRASES_MAX = 1, 10
CATALOG_PAGES_MIN, CATALOG_PAGES_MAX = 1, 10
CATALOG_MIN_STARS_DEFAULT = 20
CATALOG_PAGES_DEFAULT = 2

#: Пауза SSE-heartbeat. Тесты опускают константу в 0.1 с вместо реальных 15.
SSE_HEARTBEAT_INTERVAL = 15.0

#: TTL кэша ``gh auth status`` в /api/health: проверка — подпроцесс, на каждый
#: запрос её гонять нельзя (§7: в обработчиках только чтение из SQLite/кэша).
GH_OK_TTL_SECONDS = 60.0

ROOT = Path(__file__).resolve().parent.parent
VIEWS_DIR = ROOT / "web" / "views"
STATIC_DIR = ROOT / "web" / "static"
#: Документация для окна: ссылка «как получить ключ» в «Настройках → API-ключи»
#: ведёт на ``docs/API-KEYS.md`` (шаг P4a), а файл лежит не среди шаблонов —
#: поэтому он отдаётся отдельным маршрутом (см. ``create_app``).
DOCS_DIR = ROOT / "docs"

#: Белый список файлов ``docs/``, доступных по ``/docs/{filename}`` (шаг P4b, §7).
#: Один файл, а не каталог: остальное в ``docs/`` — внутренние заметки проекта.
DOCS_PUBLIC = frozenset({"API-KEYS.md"})

#: Сколько наблюдений читать на пакет для momentum (шаг P4b): окно 90 дней
#: покрывается десятками замеров, а 100 — потолок, после которого история
#: одного пакета начала бы заметно утяжелять ``GET /api/matrix``.
MOMENTUM_HISTORY_LIMIT = 100

#: Сигналы §18.3, которых ещё нет. Отдаются в ``signals_unavailable``, чтобы UI
#: честно показывал «сигнал будет позже», а не «сигнала нет»: в P4c из списка
#: уходит ``cross_ecosystem`` (он посчитан, если у пакета есть concept), остальные
#: — P5. ``gap_score`` здесь нет: это агрегат по сигналам, а не сигнал (§18.3).
SIGNALS_PENDING: tuple[str, ...] = (
    "cross_ecosystem",
    "jobs",
    "patents",
    "funding",
    "search_interest",
)

#: Экосистемы concept_packages (§5): POST /api/concepts принимает только их —
#: «четвёртая» экосистема означала бы опечатку, а не расширение.
CONCEPT_ECOSYSTEMS: frozenset[str] = frozenset({"pypi", "npm", "github"})

#: Виды сигналов для POST /api/signals/collect (P5a: только jobs). Дубль
#: ``cli.signals.SIGNAL_KINDS`` осознанно (gotcha 54): web не импортирует
#: cli, но «согласие слоёв» проверяется тестом «согласие слоёв» — оба
#: набора обязаны совпадать, иначе API ответит 202 задаче, которая умрёт
#: в подпроцессе argparse'ом.
SIGNAL_COLLECT_KINDS: frozenset[str] = frozenset({"jobs"})

_ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Валидация ``params`` строится по таблицам из ``app/jobs.py`` — единый источник
#: правды: ``KIND_PARAM_KEYS`` (какие ключи допустимы), ``KIND_REQUIRED_KEYS``
#: (обязательные) и ``KIND_PARAM_TYPES`` (типы значений). Локального списка
#: kind в этом модуле нет намеренно (шаг 3d-b): два описания «валидной задачи»
#: расходились — API отвечал 202, а задача падала с ``missing param``.

# --- настройки (шаг 2c) ------------------------------------------------------

#: Что экран «Настройки» вправе менять через ``PUT /api/settings``.
#: ``data_dir`` здесь нет: путь данных задаётся ``--data-dir``/env и требует
#: перезапуска (guard в ``core.settings.resolve_data_dir``); ``language`` — тоже
#: (реализован только ``ru``, переключатель был бы фикцией).
EDITABLE_SETTING_KEYS: tuple[str, ...] = (
    "alive_weekly",
    "alive_monthly",
    "dead_weekly",
    "dead_monthly",
    "stale_days",
    "adoption_delta_pct",
    "keywords",
    "cache_ttl_hours",
    "max_parallel_jobs",
    "registry_limit",
)

#: Изменение любого из этих полей меняет вердикты и раскладку матрицы.
#: Список намеренно шире пары ``alive_*``: ``stale_days`` и
#: ``adoption_delta_pct`` уезжают в тот же ``VerdictConfig``.
VERDICT_SETTING_KEYS: tuple[str, ...] = (
    "alive_weekly",
    "alive_monthly",
    "dead_weekly",
    "dead_monthly",
    "stale_days",
    "adoption_delta_pct",
)

#: Целочисленные поля: ключ -> (минимум, максимум|None, что писать при нарушении).
_INT_SETTING_BOUNDS: dict[str, tuple[int, int | None, str]] = {
    "alive_weekly": (0, None, "порог не может быть отрицательным"),
    "alive_monthly": (0, None, "порог не может быть отрицательным"),
    "dead_weekly": (0, None, "порог не может быть отрицательным"),
    "dead_monthly": (0, None, "порог не может быть отрицательным"),
    "stale_days": (1, None, "застой — минимум 1 день"),
    "max_parallel_jobs": (1, 10, "параллельных задач — от 1 до 10"),
    "registry_limit": (1, 1000, "лимит реестра — от 1 до 1000"),
}

#: Дробные поля: ключ -> (минимум, строго ли больше, что писать при нарушении).
_FLOAT_SETTING_BOUNDS: dict[str, tuple[float, bool, str]] = {
    "adoption_delta_pct": (0.0, False, "Δ adoption не может быть отрицательной"),
    "cache_ttl_hours": (0.0, True, "TTL кэша должен быть больше нуля"),
}

#: Границы списка ключевых слов (``PUT /api/settings``).
KEYWORDS_MIN, KEYWORDS_MAX = 1, 50

WARNING_VERDICTS = (
    "изменение порогов повлияет на /api/matrix и вердикты — "
    "нажмите «Обновить» на экранах"
)
WARNING_RESTART = (
    "изменение max_parallel_jobs вступит в силу после перезапуска приложения"
)


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
        return await self._page(request, call_next)

    async def _page(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """HTML-страница: ``?token=`` валиден → ставим cookie сессии (шаг 3a).

        Cookie — то, на чём живёт навигация: ссылки в шапке идут без ``?token=``,
        и ни WebView2, ни браузер его при клике не переносят (AGENTS gotcha 40).
        До 3a токен подставлялся из ``sessionStorage`` в JS; теперь сервер сам
        подтверждает сессию, а JS остаётся запасным путём для заголовка.

        Невалидный ``?token=`` — 403 прямо на странице: иначе окно открылось бы
        пустым, а причина (старый токен после перезапуска) осталась бы в консоли.
        Без ``?token=`` страница отдаётся как раньше (200) — её запросы к
        ``/api/*`` сами получат 403.

        ``/static/*`` не трогаем: статике токен не нужен, и 403 на неё сломал бы
        страницу, уже отданную с просроченным ``?token=`` в адресе картинки.
        """
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        supplied = request.query_params.get(TOKEN_QUERY)
        if not supplied:
            return await call_next(request)
        if not secrets.compare_digest(supplied, self.token):
            return JSONResponse(
                {"ok": False, "detail": "forbidden: неверный токен сессии"},
                status_code=403,
            )
        response = await call_next(request)
        response.set_cookie(
            TOKEN_COOKIE,
            self.token,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=None,  # сессионная: живёт до закрытия окна
        )
        return response


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


def _query_int(
    name: str,
    raw: str | None,
    *,
    default: int,
    low: int,
    high: int | None = None,
) -> int:
    """Целочисленный query-параметр с проверкой границ; нарушение — 400.

    Не ``Query(ge=…, le=…)``: FastAPI на такой параметр отвечает 422, а контракт
    экранов (шаг 3a) ждёт 400 «плохой запрос» — ``limit=1000`` это не ошибка
    схемы тела, а просьба больше, чем API готов отдать. Пустая строка
    (``?limit=``) — как отсутствие параметра: ``URLSearchParams`` в JS не
    различает «не задано» и «пусто».
    """
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail=f"{name}: ожидалось целое, пришло {raw!r}"
        ) from None
    if value < low or (high is not None and value > high):
        allowed = f"от {low} до {high}" if high is not None else f"не меньше {low}"
        raise HTTPException(
            status_code=400, detail=f"{name}: допустимо {allowed}, пришло {value}"
        )
    return value


def _store(request: Request) -> Store:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="хранилище не открыто (lifespan)")
    return store


def _api_keys(request: Request) -> ApiKeysStore:
    """Хранилище ключей из ``app.state`` (шаг P4a, создаётся в lifespan).

    Без lifespan (голый ``create_app`` без ``with TestClient``) хранилища нет —
    это 503, как у ``Store`` и очереди: молча создавать второе хранилище по
    каталогу настроек значило бы писать ключи не туда, где читает приложение.
    """
    store = getattr(request.app.state, "api_keys", None)
    if store is None:
        raise HTTPException(status_code=503, detail="хранилище ключей не открыто (lifespan)")
    return store


def _bool_query(name: str, raw: str | None) -> bool | None:
    """Булев query-параметр: ``true``/``1`` → True, ``false``/``0`` → False, пусто → None.

    Не ``bool(raw)``: строка ``"false"`` истинна, и фильтр «Заброшенные» на
    экране каталога показывал бы ровно живые проекты — ошибка, которую видно
    только глазами. Пустая строка — как отсутствие параметра: ``URLSearchParams``
    в JS не различает «не задано» и «пусто» (та же причина, что у
    :func:`_query_int`).
    """
    if raw is None or raw == "":
        return None
    text = raw.strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise HTTPException(
        status_code=400, detail=f"{name}: ожидалось true/false, пришло {raw!r}"
    )


def _catalog_params(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Разобрать тело ``POST /api/catalogs``: params задачи и список проблем.

    Отдельная проверка поверх таблиц ``app/jobs.py``: там whitelist ключей и
    типов, здесь — границы, которые знает только этот эндпоинт (сколько фраз,
    сколько страниц на фразу, какие источники). Экран «Каталоги» — единственный
    клиент эндпоинта, и задача, которая заведомо упрётся в лимит GitHub, не
    должна получить 202.

    Возвращаются только ключи ``catalog.build``: всё прочее в тело запроса —
    ошибка, а не «лишнее поле проигнорируем». ``min_stars``/``pages`` при
    отсутствии берутся дефолтными и уходят в задачу явно — так в карточке
    задачи видно, с какими параметрами её собирали.
    """
    problems: list[str] = []

    raw_phrases = payload.get("phrases")
    phrases: list[str] = []
    if not isinstance(raw_phrases, list) or not all(
        isinstance(item, str) for item in raw_phrases
    ):
        problems.append("phrases: ожидался список строк")
    else:
        phrases = [item.strip() for item in raw_phrases if item.strip()]
        if not CATALOG_PHRASES_MIN <= len(phrases) <= CATALOG_PHRASES_MAX:
            problems.append(
                f"phrases: нужно от {CATALOG_PHRASES_MIN} до {CATALOG_PHRASES_MAX} "
                f"непустых фраз, пришло {len(phrases)}"
            )

    min_stars = payload.get("min_stars", CATALOG_MIN_STARS_DEFAULT)
    if isinstance(min_stars, bool) or not isinstance(min_stars, int):
        problems.append("min_stars: ожидалось целое")
    elif min_stars < 0:
        problems.append("min_stars: не может быть отрицательным")

    pages = payload.get("pages", CATALOG_PAGES_DEFAULT)
    if isinstance(pages, bool) or not isinstance(pages, int):
        problems.append("pages: ожидалось целое")
    elif not CATALOG_PAGES_MIN <= pages <= CATALOG_PAGES_MAX:
        problems.append(
            f"pages: допустимо от {CATALOG_PAGES_MIN} до {CATALOG_PAGES_MAX}, "
            f"пришло {pages}"
        )

    raw_sources = payload.get("sources", list(CATALOG_SOURCES))
    sources: list[str] = []
    if not isinstance(raw_sources, list) or not all(
        isinstance(item, str) for item in raw_sources
    ):
        problems.append("sources: ожидался список строк")
    else:
        unknown = [item for item in raw_sources if item not in CATALOG_SOURCES]
        if unknown:
            problems.append(
                "sources: неизвестные источники " + ", ".join(sorted(set(unknown)))
            )
        elif not raw_sources:
            problems.append(
                "sources: пусто — укажите хотя бы один из "
                + ", ".join(CATALOG_SOURCES)
            )
        sources = list(dict.fromkeys(raw_sources))

    extra = sorted(set(payload) - {"phrases", "min_stars", "pages", "sources"})
    if extra:
        problems.append("неизвестные параметры: " + ", ".join(extra))

    params: dict[str, Any] = {
        "phrases": phrases,
        "min_stars": min_stars,
        "pages": pages,
        "sources": sources,
    }
    return params, problems


def _catalog_markdown(catalog: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """Экспорт каталога в Markdown: шапка + таблица ``core.catalog.to_markdown``.

    Таблица не пишется здесь заново: ``to_markdown`` уже рисует её для CLI, и
    второй формат разошёлся бы с первым при первой же правке колонок. Строки
    БД переводятся в форму каталога (``id``/``sources``) — колонки те же,
    ``|`` внутри описания ``to_markdown`` заменяет сам.
    """
    from core.catalog import to_markdown  # ленивый импорт: нужен только экспорту

    queries = [str(query) for query in (catalog.get("queries") or [])]
    rows = [
        {
            "id": item.get("name"),
            "stars": item.get("stars"),
            "lang": item.get("lang"),
            "pushed": item.get("pushed"),
            "desc": item.get("desc"),
            "sources": [
                source
                for source in str(item.get("source") or "").split(",")
                if source
            ],
        }
        for item in items
    ]
    title = " · ".join(queries) if queries else str(catalog.get("id"))
    head = [
        f"Каталог `{catalog.get('id')}`",
        "",
        "Фразы: " + (", ".join(queries) if queries else "—"),
        f"Собран: {catalog.get('created_at')}",
        f"Элементов: {len(items)}",
        "",
        "",
    ]
    return "\n".join(head) + to_markdown(rows, title)


def _catalog_rows(items: list[dict[str, Any]]) -> list[list[Any]]:
    """Строки CSV-экспорта: те же колонки, что в шапке ``CATALOG_CSV_HEADER``.

    ``None`` превращается в пустую строку: ``csv.writer`` записал бы ``None``
    словом «None», и Excel показал бы его как значение.
    """
    return [
        [
            item.get("name") or "",
            item.get("source") or "",
            "" if item.get("stars") is None else item.get("stars"),
            item.get("lang") or "",
            item.get("pushed") or "",
            item.get("desc") or "",
        ]
        for item in items
    ]


#: Колонки CSV-экспорта каталога (шаг 3b) — порядок фиксирован: его читает Excel.
CATALOG_CSV_HEADER = ("name", "source", "stars", "lang", "pushed", "desc")

#: Колонки экспорта событий (шаг 3c): те же поля, что у ``GET /api/events``,
#: плюс ``run_id`` — по нему события группируются в один прогон мониторинга.
EVENTS_CSV_HEADER = ("id", "kind", "name", "detail", "at", "run_id")

#: Колонки экспорта матрицы и сравнения (шаг 3c): одна строка — один пакет.
#: Порядок колонок — как у точки ``/api/matrix``, чтобы файл и экран читались
#: слева направо одинаково.
MATRIX_CSV_HEADER = (
    "name",
    "source",
    "version",
    "released_at",
    "adoption_week",
    "adoption_month",
    "flag",
    "verdict",
    "measured_at",
)

#: Десятая колонка экспорта матрицы (шаг P4c): ``momentum_30d`` — то же поле,
#: что на экране (точки матрицы несут его с P4b). Отдельный кортеж, а не
#: расширение ``MATRIX_CSV_HEADER``: сравнение (``/api/compare/export``) свою
#: историю не считает и остаётся с прежними девятью колонками — иначе шапка
#: обещала бы колонку, которой в строках нет.
MATRIX_EXPORT_CSV_HEADER = MATRIX_CSV_HEADER + ("momentum_30d",)


def _csv_body(header: tuple[str, ...], rows: list[list[Any]]) -> bytes:
    """CSV с BOM (``utf-8-sig``) и CRLF: без BOM Excel показывает «Ð¡Ð¾Ð±Ñ‹Ñ‚Ð¸Ñ».

    Один построитель на все выгрузки шага (события, матрица, сравнение):
    каждая копия ``csv.writer`` — это ещё одно место, где забывается BOM или
    ``lineterminator``, и файл открывается одной строкой в старом Excel.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def _attachment(
    body: bytes, *, filename: str, media_type: str = "text/csv; charset=utf-8"
) -> Response:
    """Файл в ответе: ``Content-Disposition: attachment`` и запрет кэша.

    ``Cache-Control: no-store`` — не украшение: выгрузка читает БД, и повторное
    скачивание после новой задачи обязано дать новые строки, а не копию из
    кэша браузера.
    """
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _export_date() -> str:
    """Дата в имени файла выгрузки (UTC, ``YYYY-MM-DD``)."""
    return time.strftime("%Y-%m-%d", time.gmtime())


def _compare_names(raw: str) -> list[str]:
    """Разобрать ``names=a,b,c`` экрана «Сравнение»: 2–3 имени, без повторов.

    Пустые куски (``a,,b``, хвостовая запятая) отбрасываются: пользователь
    вводит строку руками, и лишняя запятая не должна давать 400 на «двух»
    именах. Границы проверяются после дедупликации — ``a,a,b`` это два имени.
    """
    parts = [part.strip() for part in (raw or "").split(",")]
    names = list(dict.fromkeys(part for part in parts if part))
    if len(names) < COMPARE_NAMES_MIN or len(names) > COMPARE_NAMES_MAX:
        raise HTTPException(
            status_code=400,
            detail=(
                f"names: нужно {COMPARE_NAMES_MIN}–{COMPARE_NAMES_MAX} имени через "
                f"запятую, пришло {len(names)}"
            ),
        )
    return names


def _compare_row(observation: dict[str, Any]) -> dict[str, Any]:
    """Наблюдение -> колонка экрана «Сравнение» (шаг 3c).

    Признак разложен на два поля, как в ``/api/matrix``: ``flag`` — булево (из
    ``flag_value``), ``flag_label`` — метка ключевого слова (``flag`` TEXT).
    ``bool("MCP")`` всегда истинно, поэтому одного поля было бы мало.
    """
    return {
        "name": observation.get("name"),
        "source": observation.get("source"),
        "version": observation.get("version"),
        "released_at": observation.get("released_at"),
        "adoption_month": observation.get("adoption_month"),
        "adoption_week": observation.get("adoption_week"),
        "flag": bool(observation.get("flag_value")),
        "flag_label": observation.get("flag"),
        "verdict": observation.get("verdict"),
        "measured_at": observation.get("measured_at"),
    }


def _matrix_points(
    observations: list[dict[str, Any]], alive_weekly: int
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Точки матрицы и квадранты из последних наблюдений (шаг 2b, общий с 3c).

    Один построитель на ``GET /api/matrix`` и ``POST /api/matrix/export``:
    экспорт обязан содержать ровно те точки, что на экране, и отдельный цикл
    разошёлся бы с ним при первой правке фильтра (например, «показывать и
    нулевой adoption»).

    Точка — только наблюдение с ``adoption_week > 0``: на логарифмической оси
    ``log10(0)`` не существует. Такие наблюдения остаются в ``counts.total``,
    но не в ``counts.with_week`` — разница и есть ответ на вопрос «почему
    точек меньше, чем пакетов».
    """
    quadrants: dict[str, list[str]] = {
        "high_no_flag": [],
        "high_with_flag": [],
        "low_no_flag": [],
        "low_with_flag": [],
    }
    points: list[dict[str, Any]] = []
    for observation in observations:
        name = str(observation.get("name") or "")
        week = observation.get("adoption_week") or 0
        if not name or not isinstance(week, int) or week <= 0:
            continue
        flag = bool(observation.get("flag_value"))
        if week >= alive_weekly:
            bucket = "high_with_flag" if flag else "high_no_flag"
        else:
            bucket = "low_with_flag" if flag else "low_no_flag"
        quadrants[bucket].append(name)
        points.append(
            {
                "name": name,
                "source": observation.get("source"),
                "version": observation.get("version"),
                "released_at": observation.get("released_at"),
                "adoption_week": week,
                "adoption_month": observation.get("adoption_month"),
                "flag": flag,
                "flag_label": observation.get("flag"),
                "verdict": observation.get("verdict"),
                "measured_at": observation.get("measured_at"),
            }
        )
    return points, quadrants


# --- momentum (шаг P4b) -------------------------------------------------------


def _momentum_payload(store: Store, name: str) -> dict[str, Any]:
    """Momentum пакета словарём — то, что уезжает в ``/api/signals`` и в карточку.

    ``name`` подставляется из запроса, а не берётся из первого наблюдения:
    у пакета без истории поля ``name`` в наблюдении нет, и ответ «про пакет X»
    с пустым ``name`` выглядел бы как чужой сигнал.
    """
    history = store.get_observations_history(name, limit=MOMENTUM_HISTORY_LIMIT)
    payload = compute_momentum(history).as_dict()
    payload["name"] = name
    return payload


def _attach_momentum(store: Store, points: list[dict[str, Any]]) -> None:
    """Дописать в точки матрицы ``momentum_30d``/``momentum_color``/``samples``.

    Правит точки на месте, а не возвращает копию: ``_matrix_points`` — общий
    построитель с ``POST /api/matrix/export``, и у экспорта свой фиксированный
    набор колонок (шаг 3c). Расширение здесь, а не в построителе, оставляет CSV
    ровно тем же, что был, — иначе «экспорт = картинка» перестал бы выполняться
    по числу колонок (с P4c экспорт получает ``momentum_30d`` десятой колонкой
    через :data:`MATRIX_EXPORT_CSV_HEADER`, построитель по-прежнему общий).
    """
    for point in points:
        payload = _momentum_payload(store, str(point.get("name") or ""))
        point["momentum_30d"] = payload["delta_30d"]
        point["momentum_color"] = payload["color"]
        point["momentum_samples"] = payload["samples"]


# --- cross-ecosystem (шаг P4c) -------------------------------------------------


def _cross_ecosystem_payload(store: Store, package: str) -> dict[str, Any] | None:
    """Сигнал cross-ecosystem пакета словарём; ``None`` — concept не задан.

    Один источник данных на два ответа (``/api/signals/{package}`` и карточка
    ``/api/packages/{name}``): карточка рисует «3 полосы» из signals того же
    запроса, что и momentum, поэтому дублирование запроса в двух обработчиках
    разошлось бы при первой правке.

    В P4c у пакета может быть несколько concept-строк (одно имя в разных
    концептах или экосистемах): concept берётся из строки, чья экосистема
    совпала с источником наблюдения, иначе — из первой
    (``concept_packages[0]``); список концептов — отдельная задача, не P4c.
    """
    rows = store.get_concept_packages(package=package)
    if not rows:
        return None
    concept = str(rows[0].get("concept") or "")
    # Своя экосистема — по источнику последнего наблюдения, а не «первая
    # строка mapping»: одно имя может быть привязано и к pypi, и к npm, и
    # без этого пакет с источником pypi показывал бы себя под npm-полосой
    # (живой прогон P4c: chromadb маплен в обе экосистемы). Источник вне
    # трёх экосистем ('-', 'registry') — берём строку mapping, как раньше.
    latest = store.get_latest_observations([package])
    source = str(latest[0].get("source") or "") if latest else ""
    if source in CONCEPT_ECOSYSTEMS:
        ecosystem = source
        matched = [row for row in rows if str(row.get("ecosystem")) == source]
        if matched:
            concept = str(matched[0].get("concept") or concept)
    else:
        ecosystem = str(rows[0].get("ecosystem") or "")
    all_rows = store.get_concept_packages(concept=concept)
    names = {str(row.get("package") or "") for row in all_rows}
    observations = {
        name: store.get_observations_history(name, limit=MOMENTUM_HISTORY_LIMIT)
        for name in names
        if name
    }
    cross = compute_cross_ecosystem(
        package,
        concept_packages=all_rows,
        observations_by_name=observations,
        package_ecosystem=ecosystem,
    )
    return cross.as_dict() if cross else None


# --- jobs (шаг P5a) -----------------------------------------------------------


def _jobs_payload(store: Store, package: str) -> tuple[dict[str, Any] | None, str]:
    """Последнее наблюдение jobs + его статус для UI (§18.3, шаг P5a).

    Возвращает ``(detail | None, status)``:

    * наблюдения нет — ``(None, "not_collected")``: сигнал ещё не собирали;
    * есть, ``configured=False`` — ``(None, "not_configured")``: ключ Adzuna
      не введён (в UI — ссылка «Настройки → API-ключи»);
    * есть, ``configured=True`` — ``(detail, "ok")``: ``total`` /
      ``by_country`` / ``error`` из ``signal_observations.detail``.

    Три состояния различимы осознанно (шаг P5a, §4.3): «ключ не настроен»
    и «не собирали» — разные действия пользователя, и одно «jobs: null» не
    могло бы их развести. ``detail`` без ``configured`` (битая старая
    запись) считается «не собрано»: показать пустой блок как данные было
    бы ложью.
    """
    rows = store.get_signal_observations(package, signal_kind="jobs", limit=1)
    if not rows:
        return None, "not_collected"
    detail = rows[0].get("detail")
    if not isinstance(detail, dict):
        return None, "not_collected"
    if not detail.get("configured"):
        return None, "not_configured"
    return detail, "ok"


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


# --- настройки: валидация и сохранение (шаг 2c) ------------------------------


def _is_number(value: Any) -> bool:
    """``bool`` числом не считается: JSON ``true`` не должен стать порогом 1."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _settings_problem(key: str, value: Any) -> str | None:
    """Проверить одно значение из ``PUT /api/settings``; ``None`` — годится.

    Правила — по типу поля, а не по имени ключа в обработчике: таблицы границ
    (:data:`_INT_SETTING_BOUNDS`, :data:`_FLOAT_SETTING_BOUNDS`) задают и тип, и
    диапазон, поэтому новый изменяемый ключ добавляется одной строкой, а не
    ещё одной веткой ``if``.

    Ключевые слова нормализуются здесь же (``strip``): из формы приходит текст,
    разбитый по строкам, и пробелы вокруг слова — не часть признака. Пустая
    строка — ошибка (а не «молча выкинуть»): иначе список из одних переводов
    строки прошёл бы проверку длины и записался пустым.
    """
    if key == "keywords":
        if not isinstance(value, list):
            return "keywords: ожидался список строк"
        words = [item.strip() for item in value if isinstance(item, str)]
        if len(words) != len(value):
            return "keywords: ожидался список строк"
        if not KEYWORDS_MIN <= len(words) <= KEYWORDS_MAX:
            return (
                f"keywords: нужно от {KEYWORDS_MIN} до {KEYWORDS_MAX} слов, "
                f"пришло {len(words)}"
            )
        if not all(words):
            return "keywords: пустое слово в списке"
        return None

    if key in _INT_SETTING_BOUNDS:
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{key}: ожидалось целое"
        low, high, message = _INT_SETTING_BOUNDS[key]
        if value < low or (high is not None and value > high):
            return f"{key}: {message}"
        return None

    if key in _FLOAT_SETTING_BOUNDS:
        if not _is_number(value):
            return f"{key}: ожидалось число"
        low_f, strict, message = _FLOAT_SETTING_BOUNDS[key]
        if value < low_f or (strict and value <= low_f):
            return f"{key}: {message}"
        return None

    return f"{key}: поле нельзя менять через API"


def _normalized_setting(key: str, value: Any) -> Any:
    """Значение в том виде, в каком оно живёт в :class:`Settings`.

    Отличается от присланного только ``keywords``: в dataclass это ``tuple``
    (хешируемое поле, удобно сравнивать), а по JSON приходит список.
    """
    if key == "keywords":
        return tuple(item.strip() for item in value)
    if key in _FLOAT_SETTING_BOUNDS:
        return float(value)
    return value


def _merge_saved_settings(settings: Settings, path: Path) -> Settings:
    """Наложить изменяемые поля ``settings.json`` на настройки из окружения.

    ``desktop.py`` собирает :class:`Settings` из окружения и о файле настроек не
    знает, а P2 требует, чтобы сохранённое на экране «Настройки» переживало
    перезапуск. Поэтому чтение файла живёт здесь — в слое, который этот файл и
    пишет (``PUT /api/settings``).

    Накладываются только :data:`EDITABLE_SETTING_KEYS`: ``data_dir`` приходит из
    ``--data-dir``/``GAPAUDIT_DATA_DIR`` (иначе сохранённый путь перебил бы
    явно заданный при запуске и guard ``resolve_data_dir`` стал бы непредсказуем),
    ``language`` пока не переключается.

    Битый файл не роняет приложение: ``core.settings.load`` возвращает дефолты с
    причиной в ``load_error``, здесь причина уходит в лог, а настройки остаются
    теми, с которыми приложение стартовало.
    """
    if not path.exists():
        return settings
    saved = load_settings(path)
    if saved.load_error:
        logger.warning("settings.json не прочитан (%s): %s", path, saved.load_error)
        return settings
    updates = {key: getattr(saved, key) for key in EDITABLE_SETTING_KEYS}
    return replace(settings, **updates)


def _settings_payload(settings: Settings) -> dict[str, Any]:
    """Ответ ``GET /api/settings``: изменяемые поля, readonly-поля, список ключей."""
    data_dir = resolve_data_dir(settings)
    return {
        "settings": settings_to_dict(settings),
        "readonly": {
            "data_dir": str(data_dir),
            "db_path": str(data_dir / "gapaudit.db"),
            "version": VERSION,
            "api_token_required": True,
        },
        "editable_keys": list(EDITABLE_SETTING_KEYS),
    }


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
        settings_path = data_dir / SETTINGS_FILENAME
        # Сохранённые на экране «Настройки» значения переживают перезапуск:
        # файл накладывается на Settings из окружения до сборки Store/очереди,
        # чтобы задача стартовала уже с новым TTL и лимитом реестра.
        active_settings = _merge_saved_settings(resolved_settings, settings_path)
        store = Store(data_dir / "gapaudit.db")
        store.__enter__()
        # Концепты (шаг P4c): <data_dir>/concepts.json подмешивается в mapping
        # при каждом старте — merge, а не replace (INSERT OR IGNORE): концепты,
        # добавленные через API/Store, переживают перезапуск с тем же файлом.
        # Битый файл не роняет приложение: сигнал просто останется «не
        # настроен» (gotcha 50 — исключение коллектора в UI/API не выходит).
        concepts_path = data_dir / "concepts.json"
        if concepts_path.exists():
            try:
                payload = json.loads(concepts_path.read_text(encoding="utf-8"))
                concepts = payload.get("concepts") or {}
                loaded = 0
                skipped = 0
                for concept, ecosystems in concepts.items():
                    if not isinstance(ecosystems, dict):
                        skipped += 1
                        continue
                    for ecosystem, packages_ in ecosystems.items():
                        if not isinstance(packages_, list):
                            skipped += 1
                            continue
                        for pkg in packages_:
                            store.record_concept_package(
                                concept=str(concept),
                                ecosystem=str(ecosystem),
                                package=str(pkg),
                            )
                            loaded += 1
                logger.info(
                    "loaded %d concepts, %d packages from %s",
                    len(concepts),
                    loaded,
                    concepts_path,
                )
                if skipped:
                    logger.warning(
                        "concepts.json: пропущено неверных записей: %d", skipped
                    )
            except Exception as error:  # noqa: BLE001 - битый файл не роняет старт
                logger.warning("cannot load concepts.json: %s", error)
        queue = JobQueue(
            store, data_dir=data_dir, settings=active_settings, runner=runner
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
        application.state.store = store
        application.state.queue = queue
        application.state.data_dir = str(data_dir)
        # Настройки, с которыми реально работают запросы, и путь их файла:
        # GET/PUT /api/settings читают и пишут именно это (шаг 2c).
        application.state.settings = active_settings
        application.state.settings_path = settings_path
        # Ключи внешних API (шаг P4a): свои файл в каталоге данных, НЕ в SQLite
        # (§16 п. 7). Экземпляр живёт в app.state, потому что файл он читает на
        # каждый вызов, а «где» известно только здесь, в lifespan.
        application.state.api_keys = ApiKeysStore(data_dir)

        # --- хук завершения задачи (шаги 3d-b, 3b) ---------------------------
        # ``_post_process`` зовётся синхронно из потока воркера под ``_emit``:
        # ни чтения файла, ни записи в БД здесь — только постановка в пул.
        # Один воркер: записи не должны обгонять друг друга (порядок
        # measured_at и id остаётся предсказуемым).
        post_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="post")
        application.state.post_executor = post_executor

        def _read_result(job_id: str, result_path: Path) -> Any:
            """Прочитать ``result.json`` задачи; ошибка — в лог, ``None`` наружу."""
            try:
                return json.loads(result_path.read_text(encoding="utf-8"))
            except Exception as error:  # noqa: BLE001 - битый файл не роняет приложение
                logger.error("не читается результат задачи %s: %s", job_id, error)
                return None

        def _record_observations(job_id: str, result_path: Path) -> None:
            """``packages.check``: строки наблюдений → ``Store.record_observations``."""
            rows = _read_result(job_id, result_path)
            if rows is None:
                return
            if not isinstance(rows, list):
                logger.error("результат задачи %s: ожидался список строк", job_id)
                return
            try:
                ids = application.state.store.record_observations(
                    rows, run_id=job_id
                )
                logger.info("записано наблюдений: %d (задача %s)", len(ids), job_id)
            except Exception as error:  # noqa: BLE001 - БД не должна ронять приложение
                logger.error("record_observations не удался для %s: %s", job_id, error)

        def _record_events(job_id: str, result_path: Path) -> None:
            """``monitor.diff``: ``{"events": [...]}`` → ``Store.record_events``.

            Формат — объект с ключом ``events`` (так его пишет
            ``cli.check_predecessors --diff`` под ``--job-json``); список
            верхнего уровня тоже принимается — старые result.json и ручные
            прогоны. Строки без ``kind``/``name`` не пишутся: ``events.kind``
            в схеме NOT NULL, и одна мусорная строка отменила бы весь батч.
            """
            payload = _read_result(job_id, result_path)
            if payload is None:
                return
            raw = payload.get("events") if isinstance(payload, dict) else payload
            if not isinstance(raw, list):
                logger.error("результат задачи %s: нет списка events", job_id)
                return
            events = [
                event
                for event in raw
                if isinstance(event, dict) and event.get("kind") and event.get("name")
            ]
            if len(events) != len(raw):
                logger.warning(
                    "задача %s: пропущено строк без kind/name — %d",
                    job_id,
                    len(raw) - len(events),
                )
            try:
                ids = application.state.store.record_events(events, run_id=job_id)
                logger.info("записано событий: %d (задача %s)", len(ids), job_id)
            except Exception as error:  # noqa: BLE001 - БД не должна ронять приложение
                logger.error("record_events не удался для %s: %s", job_id, error)

        def _record_catalog(job_id: str, result_path: Path) -> None:
            """``catalog.build``: каталог и его элементы → таблицы catalogs*.

            ``catalog_id`` — id задачи: каталог и задача — одна сущность в
            жизни пользователя («каталог, собранный такой-то задачей»), и
            экран открывает его сразу по id из ответа ``POST /api/catalogs``.
            ``path`` — сам result.json: по нему видно, что читал хук.
            """
            payload = _read_result(job_id, result_path)
            if payload is None:
                return
            if not isinstance(payload, dict):
                logger.error(
                    "результат задачи %s: ожидался объект каталога", job_id
                )
                return
            queries = payload.get("queries") or []
            stats = payload.get("stats") or {}
            items = payload.get("items") or []
            if not isinstance(items, list):
                logger.error("результат задачи %s: items — не список", job_id)
                return
            try:
                store_ = application.state.store
                store_.record_catalog(
                    catalog_id=job_id,
                    queries=[str(query) for query in queries],
                    stats=stats if isinstance(stats, dict) else {},
                    path=str(result_path),
                )
                written = store_.record_catalog_items(job_id, items)
                logger.info("записан каталог %s: элементов %d", job_id, written)
            except Exception as error:  # noqa: BLE001 - БД не должна ронять приложение
                logger.error("record_catalog не удался для %s: %s", job_id, error)

        def _record_signals(job_id: str, result_path: Path) -> None:
            """``signals.collect``: ``{"package", "signals": {...}}`` → signal_observations.

            Формат — то, что пишет ``cli.signals --json-out``: по одному
            наблюдению на собранный вид сигнала (P5a: jobs). ``value`` —
            ``total`` (для будущих сортировок/агрегаций), ``detail`` — весь
            блок (``by_country``/``configured``/``error``): карточка рисует
            таблицу стран из ``detail``, а не из ``value``. Ошибка одного
            вида не мешает записи остальных — каждый вид независим.
            """
            payload = _read_result(job_id, result_path)
            if payload is None:
                return
            if not isinstance(payload, dict):
                logger.error(
                    "результат задачи %s: ожидался объект signals", job_id
                )
                return
            package = str(payload.get("package") or "").strip()
            signals = payload.get("signals")
            if not package or not isinstance(signals, dict):
                logger.error("результат задачи %s: нет package/signals", job_id)
                return
            store_ = application.state.store
            for kind, data in signals.items():
                if not isinstance(data, dict):
                    logger.warning(
                        "задача %s: сигнал %s не объект — пропущен", job_id, kind
                    )
                    continue
                try:
                    store_.record_signal_observation(
                        package=package,
                        signal_kind=str(kind),
                        value=float(data.get("total") or 0),
                        detail=data,
                    )
                except Exception as error:  # noqa: BLE001 - БД не должна ронять приложение
                    logger.error(
                        "record_signal %s/%s failed: %s", package, kind, error
                    )

        #: kind → что делать с result.json. Таблица, а не цепочка ``if``:
        #: новый вид задачи с записью в БД — одна строка, и обработчик виден
        #: целиком в одном месте.
        _POST_PROCESS_KINDS: dict[str, Callable[[str, Path], None]] = {
            "packages.check": _record_observations,
            "monitor.diff": _record_events,
            "catalog.build": _record_catalog,
            "signals.collect": _record_signals,
        }

        def _post_process(job_id: str, event_type: str, _data: dict) -> None:
            """Callback :meth:`JobQueue.subscribe`: записать результат задачи.

            Вызывается из потока воркера (синхронно, под ``_emit``). Не должен
            блокировать emit и не должен бросать исключения — поэтому всё
            опасное завёрнуто в try, а работа с диском и БД живёт в пуле.
            """
            if event_type != "done":
                return
            try:
                job = application.state.queue.get(job_id)
            except Exception as error:  # noqa: BLE001 - emit не должен падать
                logger.error(
                    "post_process: queue.get(%s) не удался: %s", job_id, error
                )
                return
            if not job:
                return
            recorder = _POST_PROCESS_KINDS.get(job.kind)
            if recorder is None:
                return
            result_path = data_dir / "jobs" / f"{job_id}.result.json"
            if not result_path.exists():
                logger.warning(
                    "нет result.json для задачи %s (%s)", job_id, job.kind
                )
                return
            try:
                post_executor.submit(recorder, job_id, result_path)
            except RuntimeError as error:
                # пул уже закрыт (идёт shutdown) — терять нечего, только сообщить
                logger.warning(
                    "post_executor закрыт, запись для %s отброшена: %s",
                    job_id,
                    error,
                )

        # подписка до queue.start(): задача, завершившаяся мгновенно, не должна
        # проскочить мимо хука.
        queue.subscribe(_post_process)
        queue.start()
        try:
            yield
        finally:
            # 1. отписка ДО остановки: cancelled из queue.stop в хук не нужен.
            queue.unsubscribe(_post_process)
            # 2. дождаться записей, пока Store ещё открыт.
            post_executor.shutdown(wait=True)
            # 3. остановить очередь.
            queue.stop(timeout=5.0)
            # 4. закрыть БД.
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

    @application.get("/docs/{filename}")
    def docs_file(filename: str) -> Response:
        """``docs/`` наружу — только белый список (шаг P4b, §7: ``API-KEYS.md``).

        До P4b каталог ``docs/`` монтировался целиком, и любой файл проекта
        (внутренние заметки, скриншоты, черновики) отдавался бы по адресу
        ``/docs/<имя>`` без токена. Шаг P4a уже сделал ``API-KEYS.md`` целью
        ссылки «Инструкции по получению» (§18.6), поэтому нужен ровно один
        файл: всё остальное — 404, а не «нет такого маршрута», чтобы спец.
        документ не искали перебором имён.

        Токен здесь не требуется (это не ``/api/*``): документация — это
        инструкция «как получить ключ», в ней нет ни данных приложения, ни
        значений ключей.
        """
        if filename not in DOCS_PUBLIC:
            raise HTTPException(status_code=404, detail=f"документа нет: {filename}")
        path = DOCS_DIR / filename
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise HTTPException(
                status_code=404, detail=f"документа нет: {filename}"
            ) from error
        return Response(text, media_type="text/markdown; charset=utf-8")

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
        """Продолжить ``interrupted``-задачу (шаг 3d-b): ``{"resumed": true}``.

        Коды: ``404`` — задачи нет; ``409`` — задача не ``interrupted`` (продолжать
        нечего) или ``JobQueue.resume_interrupted`` отказал (нет лога прошлого
        запуска — свидетельства, что запуск вообще был). Успех — задача снова
        ``queued`` и уже в рабочей очереди; статус подтверждается через
        ``GET /api/jobs/{id}`` (источник правды — Store).
        """
        store = _store(request)
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"задача {job_id} не найдена")
        if job["status"] != "interrupted":
            raise HTTPException(
                status_code=409,
                detail=(
                    "продолжить можно только interrupted, "
                    f"текущий статус: {job['status']}"
                ),
            )
        if not _queue(request).resume_interrupted(job_id):
            raise HTTPException(
                status_code=409,
                detail="продолжение не удалось: нет лога прошлого запуска",
            )
        return {"resumed": True, "job_id": job_id}

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

    # --- события «Мониторинг» (шаг 3a) ----------------------------------------

    @application.get("/api/events")
    async def get_events(request: Request) -> dict[str, Any]:
        """Таймлайн событий: ``since``, ``kind``, ``name`` + пагинация.

        ``total`` считается тем же набором фильтров, что и страница (общий
        построитель WHERE в ``Store._events_where``): экран рисует «N событий»
        и число страниц, и расхождение счётчика со списком заметно сразу.

        Пустые значения фильтров — как отсутствие фильтра: ``monitor.js``
        отправляет ``kind=`` и ``name=`` всегда, даже когда чипс «Все» активен.
        """
        params = request.query_params
        limit = _query_int(
            "limit",
            params.get("limit"),
            default=EVENTS_LIMIT_DEFAULT,
            low=1,
            high=EVENTS_LIMIT_MAX,
        )
        offset = _query_int("offset", params.get("offset"), default=0, low=0)
        since = params.get("since") or None
        kind = params.get("kind") or None
        name = params.get("name") or None
        store = _store(request)
        events = store.get_events(
            since=since, kind=kind, name=name, limit=limit, offset=offset
        )
        total = store.count_events_filtered(since=since, kind=kind, name=name)
        return {
            "events": events,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @application.post("/api/events/export")
    def export_events(request: Request) -> Response:
        """Выгрузка событий файлом (шаг 3c): ``?format=csv|json``.

        Отдаются все события под фильтрами экрана (``kind``, ``name``,
        ``since``) и без пагинации: выгрузка — это «забрать таймлайн целиком»,
        и файл с одной страницей из двадцати был бы ловушкой. Верх ``limit``
        (50 000) ограничивает не удобство, а память: строки складываются в
        список в памяти процесса.

        CSV — ``utf-8-sig`` (иначе Excel показывает кириллицу как cp1251),
        JSON — ``{"events": [...]}`` с ``ensure_ascii=False``: файл читают и
        Excel, и внешний клиент, а экранированные ``\\uXXXX`` вместо кириллицы
        сломали бы второй сценарий.

        POST, а не GET: выгрузка — действие пользователя с побочным эффектом
        (скачивание), и токен в query для мутаций запрещён (см. докстринг
        модуля).
        """
        params = request.query_params
        export_format = (params.get("format") or "csv").strip().lower()
        if export_format not in ("csv", "json"):
            raise HTTPException(
                status_code=400,
                detail=f"format: допустимо csv или json, пришло {params.get('format')!r}",
            )
        limit = _query_int(
            "limit",
            params.get("limit"),
            default=EVENTS_EXPORT_LIMIT_DEFAULT,
            low=1,
            high=EVENTS_EXPORT_LIMIT_MAX,
        )
        events = _store(request).get_events(
            since=params.get("since") or None,
            kind=params.get("kind") or None,
            name=params.get("name") or None,
            limit=limit,
            offset=0,
        )
        if export_format == "csv":
            rows = [
                [
                    event.get("id"),
                    event.get("kind"),
                    event.get("name"),
                    event.get("detail") or "",
                    event.get("at"),
                    event.get("run_id"),
                ]
                for event in events
            ]
            return _attachment(
                _csv_body(EVENTS_CSV_HEADER, rows),
                filename=f"events-{_export_date()}.csv",
            )
        body = json.dumps({"events": events}, ensure_ascii=False, default=str)
        return _attachment(
            body.encode("utf-8"),
            filename=f"events-{_export_date()}.json",
            media_type="application/json; charset=utf-8",
        )

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

    @application.get("/api/packages/search")
    async def search_packages(request: Request) -> dict[str, Any]:
        """Поиск по имени и описанию с пагинацией (экран «Пакеты», шаг 3a).

        Роут объявлен ДО ``/api/packages/{name}``: FastAPI сверяет маршруты в
        порядке регистрации, и в обратном порядке слово ``search`` стало бы
        именем пакета — экран получал бы 404 на каждый поисковый запрос.

        Пустой ``q`` — все пакеты (пагинация без поиска), поэтому ``q``
        возвращается эхом: экран показывает, по какой строке собран ответ.
        """
        params = request.query_params
        q = (params.get("q") or "").strip()
        limit = _query_int(
            "limit",
            params.get("limit"),
            default=PACKAGES_SEARCH_LIMIT_DEFAULT,
            low=1,
            high=PACKAGES_SEARCH_LIMIT_MAX,
        )
        offset = _query_int("offset", params.get("offset"), default=0, low=0)
        store = _store(request)
        packages = store.search_packages(q, limit=limit, offset=offset)
        total = store.count_packages_filtered(q)
        return {
            "packages": packages,
            "total": total,
            "limit": limit,
            "offset": offset,
            "q": q,
        }

    @application.get("/api/packages/{name}")
    async def get_package(name: str, request: Request) -> dict[str, Any]:
        """Карточка: строка пакета, последнее наблюдение, его события.

        Фильтр событий по имени — в Store (``get_events(name=...)``, шаг 3a):
        раньше здесь читались все события и отбирались в Python, из-за чего
        у пакета с давней историей карточка показывала чужие 200 событий и
        теряла свои (``LIMIT`` срабатывал до фильтра).
        """
        store = _store(request)
        package = store.get_package(name)
        if package is None:
            raise HTTPException(status_code=404, detail=f"пакет {name} не найден")
        events = store.get_events(name=name)
        jobs, jobs_status = _jobs_payload(store, name)
        return {
            "package": package,
            "observations": store.get_latest_observations([name]),
            "events": events,
            # Сигналы — в карточке (шаги P4b/P4c/P5a): momentum считается из истории,
            # которую карточка и так запрашивает отдельно для мини-графика.
            # Один ответ вместо двух запросов: card.js получает полную картину
            # из /api/packages/{name} (вариант A шага). cross_ecosystem — None,
            # если у пакета concept не задан (сигнал «не настроен»). jobs —
            # detail последнего наблюдения (P5a); три состояния jobs видны по
            # signals_status (собран / ключ не настроен / ещё не собирали).
            "signals": {
                "momentum": _momentum_payload(store, name),
                "cross_ecosystem": _cross_ecosystem_payload(store, name),
                "jobs": jobs,
            },
            "signals_status": {"jobs": jobs_status},
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

    # --- сигналы ниш (P4b: momentum, P4c: cross-ecosystem) --------------------

    @application.get("/api/signals/{package}")
    def get_signals(package: str, request: Request) -> dict[str, Any]:
        """Все сигналы по пакету; в P5a посчитаны ``momentum``, ``cross_ecosystem``
        и ``jobs`` (§18.3).

        Ответ — расширяемый конверт: сигналы рядом со списками имён.
        ``signals_available`` перечисляет то, что реально посчитано сейчас, а
        ``signals_unavailable`` — то, что появится в P5. Так UI отличает
        «сигнала нет» от «сигнал ещё не сделан» без знания календаря шагов:
        список непустых сигналов никогда не выдаёт отсутствующий за ноль.

        ``cross_ecosystem`` — ``None`` без concept (сигнал «не настроен»), и
        тогда его имя уходит в ``signals_unavailable``: непосчитанный сигнал
        не должен значить «данные есть, соседей нет».

        ``jobs`` — ``None`` и имя в ``signals_unavailable``, пока сигнала нет
        или ключ Adzuna не введён; три различимых состояния («собран» /
        «ключ не настроен» / «ещё не собирали») отдаются в
        ``signals_status``: одно «jobs: null» не могло бы развести действия
        пользователя (шаг P5a, §4.3).

        ``gap_score`` в списках нет: это агрегат по сигналам, а не сигнал.

        404 — пакет без единого замера. Отдавать «momentum = None» значило бы
        отвечать «пакет есть, данных мало» на имя, которого приложение не
        знает: сигналы считаются только из истории наблюдений, а строка в
        ``packages`` без замеров не создаётся вовсе (``_upsert_package`` пишет
        её из того же замера).
        """
        store = _store(request)
        history = store.get_observations_history(package, limit=MOMENTUM_HISTORY_LIMIT)
        if not history:
            raise HTTPException(status_code=404, detail=f"package not found: {package}")
        momentum = compute_momentum(history)
        cross = _cross_ecosystem_payload(store, package)
        jobs, jobs_status = _jobs_payload(store, package)
        available = ["momentum"] + (["cross_ecosystem"] if cross else [])
        if jobs is not None:
            available.append("jobs")
        return {
            "package": package,
            "momentum": momentum.as_dict(),
            "cross_ecosystem": cross,
            "jobs": jobs,
            "signals_status": {"jobs": jobs_status},
            "signals_available": available,
            "signals_unavailable": [
                name for name in SIGNALS_PENDING if name not in available
            ],
        }

    @application.post("/api/signals/collect", status_code=202)
    def collect_signals(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """Собрать сигналы: ``{"package": ..., "kinds": ["jobs"]}`` → ``202 {job_id}``.

        Прокси на ``POST /api/jobs`` kind=``signals.collect``: для UI один
        эндпоинт «собрать сигналы пакета» удобнее, валидация — та же
        (:func:`_param_errors`), плюс проверка вида сигнала по
        :data:`SIGNAL_COLLECT_KINDS`: ``kinds`` — список, и таблицы типов
        элементы не разбирают, а задача с ``kinds=["evil"]`` умерла бы
        argparse'ом в подпроцессе.

        ``kinds`` необязателен: без него CLI собирает всё, что умеет
        (P5a — jobs). Пустой ``package`` и неизвестный вид — 400: задача,
        которая заведомо не запустится, не должна создаваться (fail fast,
        как у ``/api/jobs``).
        """
        params: dict[str, Any] = {
            "package": str(payload.get("package") or "").strip()
        }
        raw_kinds = payload.get("kinds")
        if raw_kinds is not None:
            params["kinds"] = raw_kinds
        errors = _param_errors("signals.collect", params)
        if isinstance(raw_kinds, list):
            unknown = sorted({str(kind) for kind in raw_kinds} - SIGNAL_COLLECT_KINDS)
            if unknown:
                errors.append(
                    f"kinds: неизвестный вид сигнала: {', '.join(unknown)}"
                )
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))
        job_id = _queue(request).submit("signals.collect", params)
        return {"job_id": job_id, "status": "queued"}

    # --- концепты (шаг P4c) ----------------------------------------------------

    @application.get("/api/concepts")
    def list_concepts(request: Request) -> dict[str, Any]:
        """Список концептов: ``[{concept, n_packages, ecosystems}, ...]``.

        Чтение из ``concept_packages`` — ни сети, ни расчёта (§7): отвечает на
        вопрос «какие концепты вообще заданы», в том числе после загрузки
        ``<data_dir>/concepts.json`` на старте.
        """
        return {"concepts": _store(request).list_concepts()}

    @application.post("/api/concepts", status_code=202)
    def add_concept(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """Добавить привязку: ``{"concept", "ecosystem", "package"}`` → 202.

        Валидация до записи: пустые concept/package и чужая экосистема — 400.
        Ошибка данных не должна превращаться в молчаливую строку-мусор в
        mapping: ``concept_packages`` потом сравнивает целые экосистемы.
        """
        concept = str(payload.get("concept") or "").strip()
        ecosystem = str(payload.get("ecosystem") or "").strip()
        package = str(payload.get("package") or "").strip()
        problems = []
        if not concept:
            problems.append("concept: не может быть пустым")
        if ecosystem not in CONCEPT_ECOSYSTEMS:
            problems.append(
                f"ecosystem: допустимо {sorted(CONCEPT_ECOSYSTEMS)}, "
                f"пришло {payload.get('ecosystem')!r}"
            )
        if not package:
            problems.append("package: не может быть пустым")
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))
        _store(request).record_concept_package(
            concept=concept, ecosystem=ecosystem, package=package
        )
        return {"ok": True}

    @application.delete("/api/concepts/{concept}")
    def delete_concept(concept: str, request: Request) -> dict[str, Any]:
        """Удалить концепт целиком; 404 — если такого не было."""
        deleted = _store(request).delete_concept(concept)
        if deleted == 0:
            raise HTTPException(status_code=404, detail=f"concept not found: {concept}")
        return {"deleted": deleted}

    # --- матрица «Сейчас решать» (шаг 2b) -------------------------------------

    @application.get("/api/matrix")
    async def get_matrix(request: Request) -> dict[str, Any]:
        """Агрегат экрана «Сейчас решать»: точки adoption × признак и квадранты.

        Источник — ``Store.get_latest_observations()``: по одному наблюдению на
        имя, поэтому на графике одна точка на пакет. В ``points`` попадают
        только наблюдения с ``adoption_week > 0``: на логарифмической оси
        ``log10(0)`` не существует, и точка «без adoption» уехала бы в -inf.
        Такие наблюдения учитываются в ``counts.total``, но не в
        ``counts.with_week`` — разница и есть ответ на вопрос «почему точек
        меньше, чем пакетов».

        Квадранты делятся порогом ``settings.alive_weekly`` (по умолчанию 100):
        ``*_no_flag`` — признака ниши нет (кандидаты «ВОТ СЮДА»), ``*_with_flag``
        — признак есть (занято). Порядок имён — как его отдал Store (по
        алфавиту): стабильный ответ без сортировки на клиенте.

        Признак в схеме — две колонки (DEVPLAN §5): ``flag`` TEXT хранит метку
        ключевого слова (``"MCP"``), ``flag_value`` INTEGER — 0/1. В ответе
        ``flag`` — булево (так его читает экран), ``flag_label`` — та самая
        метка; путать их нельзя: ``bool("MCP")`` всегда истинно.

        Только чтение SQLite: сетевой работы в обработчике нет (§7), обновление
        данных — задачей ``packages.check``.
        """
        store = _store(request)
        settings = getattr(request.app.state, "settings", None) or Settings()
        thresholds = {
            "alive_weekly": int(settings.alive_weekly),
            "alive_monthly": int(settings.alive_monthly),
        }
        observations = store.get_latest_observations()
        points, quadrants = _matrix_points(observations, thresholds["alive_weekly"])
        _attach_momentum(store, points)
        return {
            "points": points,
            "quadrants": quadrants,
            "thresholds": thresholds,
            "counts": {
                "total": len(observations),
                "with_week": len(points),
                "high_no_flag": len(quadrants["high_no_flag"]),
            },
        }

    @application.post("/api/matrix/export")
    def export_matrix(request: Request) -> Response:
        """Выгрузка точек матрицы CSV (шаг 3c): ``?format=csv``.

        Ровно те точки, что на экране: построитель общий с ``GET /api/matrix``
        (:func:`_matrix_points`), и «экспорт полнее экрана» здесь был бы
        дефектом — файл сверяют с картинкой. Порог ``alive_weekly`` берётся из
        текущих настроек, как и у экрана.

        ``flag`` пишется как 0/1, а не словом: колонку считают в Excel
        формулами, и текст «да» в числовом столбце ломает сумму.

        Десятая колонка (шаг P4c) — ``momentum_30d``: то же значение, что на
        точке экрана (``_attach_momentum``, история до 100 замеров). Без
        истории — пустая строка, а не «0»: отсутствие данных и нулевой рост —
        разные ответы. Первые девять колонок — в прежнем порядке
        (``MATRIX_EXPORT_CSV_HEADER[:9] == MATRIX_CSV_HEADER``): файлы,
        собранные до P4c, читаются теми же скриптами.
        """
        export_format = (request.query_params.get("format") or "csv").strip().lower()
        if export_format != "csv":
            raise HTTPException(
                status_code=400,
                detail=(
                    "format: допустимо только csv, "
                    f"пришло {request.query_params.get('format')!r}"
                ),
            )
        settings = getattr(request.app.state, "settings", None) or Settings()
        store = _store(request)
        observations = store.get_latest_observations()
        points, _quadrants = _matrix_points(observations, int(settings.alive_weekly))
        _attach_momentum(store, points)
        rows = [
            [
                point.get("name"),
                point.get("source") or "",
                point.get("version") or "",
                point.get("released_at") or "",
                "" if point.get("adoption_week") is None else point.get("adoption_week"),
                "" if point.get("adoption_month") is None else point.get("adoption_month"),
                1 if point.get("flag") else 0,
                point.get("verdict") or "",
                point.get("measured_at") or "",
                "" if point.get("momentum_30d") is None else point.get("momentum_30d"),
            ]
            for point in points
        ]
        return _attachment(
            _csv_body(MATRIX_EXPORT_CSV_HEADER, rows),
            filename=f"matrix-{_export_date()}.csv",
        )

    # --- сравнение (шаг 3c) ---------------------------------------------------

    @application.get("/api/compare")
    def compare_packages(request: Request, names: str = "") -> dict[str, Any]:
        """Сравнение 2–3 пакетов бок о бок: последнее наблюдение каждого.

        ``names`` — строка через запятую (``requests,flask``); 400, если имён не
        2–3 после дедупликации. По одному наблюдению на имя — то же правило,
        что у матрицы: сравнивать историю незачем, экран про «сейчас».

        ``missing`` — имена, по которым в Store нет ни одного наблюдения. Они
        не ошибка: два из трёх пакетов могли ещё не проверяться, и экран
        показывает оставшиеся колонки с пометкой «нет данных». 404 — только
        когда нет ни одного: пустая таблица выглядела бы сломанным экраном.
        """
        wanted = _compare_names(names)
        observations = _store(request).get_latest_observations(wanted)
        by_name = {str(row.get("name")): row for row in observations}
        found = [name for name in wanted if name in by_name]
        if not found:
            raise HTTPException(
                status_code=404,
                detail="нет наблюдений ни по одному имени: " + ", ".join(wanted),
            )
        return {
            "packages": [_compare_row(by_name[name]) for name in found],
            "missing": [name for name in wanted if name not in by_name],
        }

    @application.post("/api/compare/export")
    def export_compare(request: Request) -> Response:
        """Выгрузка сравнения CSV (шаг 3c): ``?format=csv&names=a,b``.

        Колонки — как у матрицы (:data:`MATRIX_CSV_HEADER`): строка на пакет, что
        и на экране «Сравнение», только транспонировано. Валидация имён и
        выборка те же, что у ``GET /api/compare`` (400/404 совпадают), — файл не
        должен уметь то, чего не умеет экран.
        """
        export_format = (request.query_params.get("format") or "csv").strip().lower()
        if export_format != "csv":
            raise HTTPException(
                status_code=400,
                detail=(
                    "format: допустимо только csv, "
                    f"пришло {request.query_params.get('format')!r}"
                ),
            )
        wanted = _compare_names(request.query_params.get("names") or "")
        observations = _store(request).get_latest_observations(wanted)
        by_name = {str(row.get("name")): row for row in observations}
        found = [name for name in wanted if name in by_name]
        if not found:
            raise HTTPException(
                status_code=404,
                detail="нет наблюдений ни по одному имени: " + ", ".join(wanted),
            )
        rows = []
        for name in found:
            package = _compare_row(by_name[name])
            rows.append(
                [
                    package.get("name"),
                    package.get("source") or "",
                    package.get("version") or "",
                    package.get("released_at") or "",
                    "" if package.get("adoption_week") is None else package["adoption_week"],
                    "" if package.get("adoption_month") is None else package["adoption_month"],
                    1 if package.get("flag") else 0,
                    package.get("verdict") or "",
                    package.get("measured_at") or "",
                ]
            )
        return _attachment(
            _csv_body(MATRIX_CSV_HEADER, rows),
            filename=f"compare-{_export_date()}.csv",
        )

    # --- каталоги (шаг 3b) ----------------------------------------------------

    @application.post("/api/catalogs", status_code=202)
    def create_catalog(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """Собрать каталог: ``{"phrases", "min_stars", "pages", "sources"}`` → 202.

        Ответ — задача, а не каталог: сборка ходит в GitHub/npm/реестр минутами,
        а обработчик по правилу §7 не имеет права на сеть. ``catalog_id`` равен
        ``job_id``: хук lifespan пишет каталог под id задачи, и экран может
        открыть детальный экран сразу, не дожидаясь списка.

        Валидация строже ``POST /api/jobs`` (границы фраз/страниц/источников,
        см. :func:`_catalog_params`): задача на 300 запросов к GitHub Search не
        должна получить 202 и умереть в лимите через час.
        """
        params, problems = _catalog_params(payload)
        if not problems:
            # Тот же валидатор, которым проверяет ``POST /api/jobs``: если
            # таблицы ``app/jobs.py`` разойдутся с этим эндпоинтом, ответ будет
            # 400 сразу, а не 202 с задачей, которая умрёт в runner'е.
            problems = _param_errors("catalog.build", params)
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))
        job_id = _queue(request).submit("catalog.build", params)
        return {"job_id": job_id, "catalog_id": job_id}

    @application.get("/api/catalogs")
    def list_catalogs(request: Request) -> dict[str, Any]:
        """Список каталогов: ``id``, фразы, дата, статистика, число элементов.

        Отсортировано по ``created_at DESC`` — свежая сборка сверху, как у
        задач. ``items_count`` считается в том же запросе к SQLite: экран
        рисует список целиком, и COUNT на каждый каталог был бы N+1.
        """
        limit = _query_int(
            "limit",
            request.query_params.get("limit"),
            default=CATALOGS_LIMIT_DEFAULT,
            low=1,
            high=CATALOGS_LIMIT_MAX,
        )
        return {"catalogs": _store(request).list_catalogs(limit=limit)}

    @application.get("/api/catalogs/{catalog_id}")
    def get_catalog(catalog_id: str, request: Request) -> dict[str, Any]:
        """Метаданные каталога + ``items_count``; неизвестный id — 404."""
        store = _store(request)
        catalog = store.get_catalog(catalog_id)
        if catalog is None:
            raise HTTPException(
                status_code=404, detail=f"каталог {catalog_id} не найден"
            )
        catalog["items_count"] = store.count_catalog_items(catalog_id)
        return catalog

    @application.get("/api/catalogs/{catalog_id}/items")
    def get_catalog_items(catalog_id: str, request: Request) -> dict[str, Any]:
        """Элементы каталога: ``alive``, ``lang``, ``min_stars``, ``q`` + пагинация.

        ``total`` считается тем же набором фильтров, что и страница (общий
        построитель WHERE в ``Store._catalog_items_where``): экран показывает
        «N элементов» и число страниц, и расхождение счётчика со списком
        заметно сразу.

        Пустые значения фильтров — как отсутствие фильтра: ``catalog_detail.js``
        отправляет ``alive=`` всегда, даже когда чипс «Все» активен.
        """
        store = _store(request)
        if store.get_catalog(catalog_id) is None:
            raise HTTPException(
                status_code=404, detail=f"каталог {catalog_id} не найден"
            )
        params = request.query_params
        alive = _bool_query("alive", params.get("alive"))
        lang = params.get("lang") or None
        q = params.get("q") or None
        raw_stars = params.get("min_stars")
        min_stars = (
            _query_int("min_stars", raw_stars, default=0, low=0)
            if raw_stars not in (None, "")
            else None
        )
        limit = _query_int(
            "limit",
            params.get("limit"),
            default=CATALOG_ITEMS_LIMIT_DEFAULT,
            low=1,
            high=CATALOG_ITEMS_LIMIT_MAX,
        )
        offset = _query_int("offset", params.get("offset"), default=0, low=0)
        items = store.get_catalog_items(
            catalog_id,
            alive=alive,
            lang=lang,
            min_stars=min_stars,
            q=q,
            limit=limit,
            offset=offset,
        )
        total = store.count_catalog_items(
            catalog_id, alive=alive, lang=lang, min_stars=min_stars, q=q
        )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @application.post("/api/catalogs/{catalog_id}/export")
    def export_catalog(catalog_id: str, request: Request) -> Response:
        """Выгрузка каталога файлом: ``?format=csv`` (по умолчанию) или ``md``.

        Отдаётся весь каталог, а не текущая страница экрана: выгрузка — это
        «забрать всё в Excel», и фильтры страницы в файл не переносятся
        (их можно повторить запросом, но файл с 100 строками из 900 был бы
        ловушкой).

        CSV — ``utf-8-sig``: без BOM Excel открывает кириллицу как cp1251 и
        показывает «Ð¡Ð¾Ð±Ñ‹Ñ‚Ð¸Ñ». Markdown собирает ``core.catalog.to_markdown`` —
        тот же формат, что у CLI-выгрузки каталога.
        """
        store = _store(request)
        catalog = store.get_catalog(catalog_id)
        if catalog is None:
            raise HTTPException(
                status_code=404, detail=f"каталог {catalog_id} не найден"
            )
        export_format = (request.query_params.get("format") or "csv").strip().lower()
        if export_format not in ("csv", "md"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "format: допустимо csv или md, "
                    f"пришло {request.query_params.get('format')!r}"
                ),
            )
        total = store.count_catalog_items(catalog_id)
        items = store.get_catalog_items(catalog_id, limit=max(total, 1))
        if export_format == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\r\n")
            writer.writerow(CATALOG_CSV_HEADER)
            writer.writerows(_catalog_rows(items))
            body = buffer.getvalue().encode("utf-8-sig")
            media_type = "text/csv; charset=utf-8"
            filename = f"catalog-{catalog_id}.csv"
        else:
            body = _catalog_markdown(catalog, items).encode("utf-8")
            media_type = "text/markdown; charset=utf-8"
            filename = f"catalog-{catalog_id}.md"
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                # Ответ зависит от БД, а не от кэша браузера: повторная
                # выгрузка после пересборки должна содержать новые строки.
                "Cache-Control": "no-store",
            },
        )

    # --- настройки (шаг 2c) ---------------------------------------------------

    @application.get("/api/settings")
    def get_settings(request: Request) -> dict[str, Any]:
        """Текущие настройки, readonly-поля и список изменяемых ключей.

        Отдаётся то, что живёт в ``app.state.settings``: после ``PUT`` это уже
        новое значение, поэтому экран читает состояние одним запросом, а не
        собирает его из ответа сохранения.

        ``readonly`` — не настройки, а факты о запущенном приложении:
        ``data_dir`` вычисляется тем же :func:`core.settings.resolve_data_dir`,
        что и в lifespan (один источник правды о пути), ``db_path`` — файл
        SQLite внутри него, ``api_token_required`` зашит ``True``: миддлварь
        требует токен на всё ``/api/*``, кроме ``/api/health``.

        Пустой ``editable_keys`` не бывает: список — контракт экрана, по нему
        ``settings.js`` решает, какие поля показывать редактируемыми.
        """
        settings = getattr(request.app.state, "settings", None) or Settings()
        return _settings_payload(settings)

    @application.put("/api/settings")
    def put_settings(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """Обновить изменяемые настройки и сохранить их в ``settings.json``.

        Валидация — до любой записи: при ошибке не меняется ни ``app.state``,
        ни файл (иначе экран показал бы «сохранено» после 400 с половиной
        применённых полей). Все проблемы собираются в один ответ.

        Инвариант ``alive_weekly >= dead_weekly`` проверяется на **сведённых**
        значениях, а не на присланных: ``PUT {"dead_weekly": 200}`` при
        ``alive_weekly=100`` из текущих настроек тоже должен быть отвергнут.

        Запись атомарна (:func:`core.settings.save`: tmp + ``os.replace``) —
        файл читают и UI при следующем старте, и подпроцесс задачи.

        ``warnings`` — что изменилось, но не вступило в силу сразу:
        пороги видны в ``/api/matrix`` и вердиктах после перечитывания экранов,
        а ``max_parallel_jobs`` — только после перезапуска (очередь собрана со
        старым значением). Экран показывает эти строки рядом с «Сохранено».
        """
        state = request.app.state
        current = getattr(state, "settings", None) or Settings()
        settings_path = getattr(state, "settings_path", None)
        if settings_path is None:
            raise HTTPException(
                status_code=503, detail="путь настроек неизвестен (lifespan не запускался)"
            )

        problems: list[str] = []
        updates: dict[str, Any] = {}
        for key, value in payload.items():
            if key not in EDITABLE_SETTING_KEYS:
                problems.append(f"{key}: неизвестная настройка")
                continue
            problem = _settings_problem(key, value)
            if problem is not None:
                problems.append(problem)
                continue
            updates[key] = _normalized_setting(key, value)
        if problems:
            raise HTTPException(status_code=400, detail="; ".join(problems))

        alive = updates.get("alive_weekly", current.alive_weekly)
        dead = updates.get("dead_weekly", current.dead_weekly)
        if alive < dead:
            raise HTTPException(
                status_code=400,
                detail=(
                    "alive_weekly: порог «живой» не может быть меньше "
                    f"«мёртвого» ({alive} < {dead})"
                ),
            )

        new_settings = replace(current, **updates) if updates else current
        changed = {
            key for key in updates if getattr(current, key) != getattr(new_settings, key)
        }
        save_settings(new_settings, settings_path)
        state.settings = new_settings

        warnings: list[str] = []
        if changed.intersection(VERDICT_SETTING_KEYS):
            warnings.append(WARNING_VERDICTS)
        if "max_parallel_jobs" in changed:
            warnings.append(WARNING_RESTART)
        return {"settings": settings_to_dict(new_settings), "warnings": warnings}

    # --- API-ключи внешних провайдеров (шаг P4a) ------------------------------

    @application.get("/api/settings/api-keys")
    def list_api_keys(request: Request) -> dict[str, Any]:
        """Провайдеры и их статус. Значения ключей не отдаются никогда.

        Ответ — контракт вкладки «API-ключи»: ``settings.js`` строит по нему
        карточку провайдера (поля ввода — из ``fields``, вид — ``kind``) и
        показывает статус. Значения ключей тут отсутствуют по построению: их
        собирает :meth:`core.api_keys.ApiKeysStore.status_all`, а расшифровка
        живёт в отдельном методе ``get``, который зовут только коллекторы (P5).

        ``meta_public`` — несекретные метаданные (``user_agent`` у EDGAR,
        ``client_id`` у Wordstat, ``actor_id`` у Apify, §7): без них после
        перезапуска окна не видно даже того, что вводить не секрет. Секреты в
        ``meta`` попасть не могут — ``save`` пускает туда только объявленные
        несекретные поля. ``meta_fields`` перечисляет эти поля по именам: по ним
        UI решает, показывать поле как ``text`` (значение вернулось) или как
        ``password`` (не вернулось и не вернётся).
        """
        store = _api_keys(request)
        statuses = {status.provider: status for status in store.status_all()}
        providers: list[dict[str, Any]] = []
        for provider, spec in PROVIDERS.items():
            status = statuses[provider]
            providers.append(
                {
                    "id": provider,
                    "label": spec["label"],
                    "kind": spec["kind"],
                    "fields": list(spec["fields"]),
                    "meta_fields": list(META_FIELDS.get(provider, ())),
                    "docs_anchor": spec["docs_anchor"],
                    "configured": status.configured,
                    "last_check": status.last_check,
                    "last_check_ok": status.last_check_ok,
                    "meta_public": store.public_meta(provider),
                }
            )
        return {"providers": providers, "load_error": store.load_error}

    @application.put("/api/settings/api-keys")
    def save_api_key(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        """Сохранить ключ провайдера: ``{provider, values, meta?}`` → ``{"ok": true}``.

        Правила «все поля провайдера заполнены» и «в ``meta`` только
        несекретные поля» живут в :meth:`core.api_keys.ApiKeysStore.save` (один
        источник правды для UI и будущего CLI), здесь только перевод
        ``ValueError`` в 400 — как у ``PUT /api/settings``, где половина
        применённого тела хуже отказа.

        В лог уходит провайдер и число полей, но не значения: §18.5 запрещает
        логировать ключи, и подпись «полей: 2» отладочной ценности не теряет.
        """
        unknown = sorted(set(payload) - {"provider", "values", "meta"})
        if unknown:
            raise HTTPException(
                status_code=400, detail=f"неизвестные поля тела: {', '.join(unknown)}"
            )
        provider = payload.get("provider")
        if not isinstance(provider, str) or not provider:
            raise HTTPException(status_code=400, detail="provider: ожидалось имя провайдера")
        # Аннотации ``Any`` — не послабление проверки, а её перенос: форму
        # ``values``/``meta`` проверяет ``ApiKeysStore.save`` (там же, где
        # правило «все поля заполнены»), и дублировать формулировки здесь нельзя.
        values: Any = payload.get("values")
        meta: Any = payload.get("meta")
        try:
            _api_keys(request).save(provider, values, meta)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        logger.info("сохранён ключ провайдера %s (полей: %d)", provider, len(PROVIDERS[provider]["fields"]))
        return {"ok": True}

    @application.post("/api/settings/api-keys/{provider}/check")
    def check_api_key(provider: str, request: Request) -> dict[str, Any]:
        """Проверка ключа — заглушка P4a: ни сети, ни записи статуса.

        Тестовый запрос у каждого провайдера свой (``docs/API-KEYS.md``) и
        осмыслен только рядом с коллектором сигнала (P5): проверять формат
        ответа API, к которому ещё нет клиента, нечего. Поэтому эндпоинт честно
        отвечает «в разработке» и НЕ зовёт ``mark_checked``: статус «проверка
        пройдена» без проверки — ложь, которую увидит пользователь.

        Коды: 404 — провайдера нет в :data:`core.api_keys.PROVIDERS`, 200 —
        ответ-заглушка (``ok=False`` и для настроенного, и для пустого: причина
        разная, и «сначала сохраните ключ» полезнее общего текста).
        """
        store = _api_keys(request)
        if provider not in PROVIDERS:
            raise HTTPException(status_code=404, detail=f"неизвестный провайдер: {provider}")
        if not store.status(provider).configured:
            return {"ok": False, "reason": "проверка будет в P5: сначала сохраните ключ"}
        return {
            "ok": False,
            "reason": "проверка будет в P5 (появится вместе с коллектором сигнала)",
        }

    @application.delete("/api/settings/api-keys/{provider}")
    def delete_api_key(provider: str, request: Request) -> dict[str, Any]:
        """Удалить ключ провайдера: ``{"ok": true}``; удалять нечего — 404.

        Один и тот же 404 на «провайдера не знаю» и «ключа нет»: наружу это
        одно состояние — «такого ключа у меня нет», и UI (кнопка «Удалить»)
        обрабатывает их одинаково.
        """
        store = _api_keys(request)
        if provider not in PROVIDERS or not store.delete(provider):
            raise HTTPException(status_code=404, detail=f"ключ провайдера {provider} не найден")
        logger.info("удалён ключ провайдера %s", provider)
        return {"ok": True}

    # --- экраны ---------------------------------------------------------------

    @application.get("/")
    async def index(request: Request):
        """Экран «Задачи» (шаг 2a): очередь, прогресс, отмена и продолжение."""
        return templates.TemplateResponse(
            request=request, name="index.html", context={"version": VERSION}
        )

    @application.get("/matrix")
    async def matrix(request: Request):
        """Экран «Сейчас решать» (шаг 2b): матрица adoption × признак.

        Данные рисует ``matrix.js`` из ``GET /api/matrix``; шаблон отдаёт
        только контейнеры — по той же причине, что и у «Задач»: строки
        приходят из задачи, а разметка не должна собираться на сервере.
        """
        return templates.TemplateResponse(
            request=request, name="matrix.html", context={"version": VERSION}
        )

    @application.get("/packages")
    async def packages_screen(request: Request):
        """Экран «Пакеты» (шаг 3a): поиск, таблица с пагинацией, карточка.

        Как и прочие экраны, шаблон отдаёт только контейнеры: строки (имена,
        summary) приходят из ``/api/packages/search`` и рисуются в ``packages.js``
        — данные из задачи/PyPI не должны попадать в разметку (gotcha 37).
        """
        return templates.TemplateResponse(
            request=request, name="packages.html", context={"version": VERSION}
        )

    @application.get("/monitor")
    async def monitor_screen(request: Request):
        """Экран «Мониторинг» (шаг 3a): таймлайн ``events`` с фильтрами.

        Данные — ``GET /api/events``; сервер не впечатывает события в HTML по
        той же причине, что и «Пакеты».
        """
        return templates.TemplateResponse(
            request=request, name="monitor.html", context={"version": VERSION}
        )

    @application.get("/catalogs")
    async def catalogs_screen(request: Request):
        """Экран «Каталоги» (шаг 3b): список каталогов и форма сборки.

        Список и форма — ``catalogs.js`` поверх ``GET/POST /api/catalogs``:
        каталог приходит из задачи (фразы, число элементов), и сервер не
        впечатывает его в HTML — та же причина, что у «Пакетов».
        """
        return templates.TemplateResponse(
            request=request, name="catalogs.html", context={"version": VERSION}
        )

    @application.get("/catalogs/{catalog_id}")
    async def catalog_detail_screen(catalog_id: str, request: Request):
        """Экран каталога (шаг 3b): элементы, фильтры, пагинация, экспорт.

        ``catalog_id`` подставляется в шаблон только как данные для JS
        (``catalog_detail.js`` берёт его из ``location.pathname``); проверки
        существования здесь нет — экран сам покажет «каталог не найден» по 404
        от ``GET /api/catalogs/{id}``, а 404 на странице означал бы, что окно
        не открывается вовсе.
        """
        return templates.TemplateResponse(
            request=request,
            name="catalog_detail.html",
            context={"version": VERSION, "catalog_id": catalog_id},
        )

    @application.get("/compare")
    async def compare_screen(request: Request):
        """Экран «Сравнение» (шаг 3c): 2–3 пакета бок о бок.

        Шаблон отдаёт только форму и пустую таблицу: колонки — это данные из
        ``GET /api/compare``, и сервер их в HTML не впечатывает (та же причина,
        что у «Пакетов» и «Каталогов»; gotcha 37).
        """
        return templates.TemplateResponse(
            request=request, name="compare.html", context={"version": VERSION}
        )

    @application.get("/settings")
    async def settings_screen(request: Request):
        """Экран «Настройки» (шаг 2c): пороги, keywords, readonly-поля.

        Форма заполняется ``settings.js`` из ``GET /api/settings``: значения
        порогов — данные, а не разметка, и сервер их в HTML не впечатывает
        (та же причина, что у «Задач» и матрицы).
        """
        return templates.TemplateResponse(
            request=request, name="settings.html", context={"version": VERSION}
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
