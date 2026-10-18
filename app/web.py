"""FastAPI-приложение GapAudit (каркас шага 0.1).

Задачи этого модуля ровно две:

1. отдать один эндпоинт готовности — ``GET /api/health``;
2. закрыть бэкенд локальным контуром: пускать только запросы с ``Host``/``Origin``
   своей же машины и требовать сессионный токен на всё, что начинается с ``/api/``.

Токен и порт создаются в ``desktop.py`` на старте и передаются в ``create_app()``.
Экземпляр ``app`` уровня модуля берёт их из переменных окружения ``GAPAUDIT_TOKEN``
и ``GAPAUDIT_PORT`` (если не заданы — токен генерируется), чтобы приложение можно
было поднять и протестировать без окна.

Живой сервер не модифицируем: middleware читает токен из заголовка
``X-GapAudit-Token``, из cookie ``gapaudit_token`` или из query-параметра ``token``
(query — для EventSource/SSE, который не умеет ставить заголовки).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

__version__ = "0.1.0"
__all__ = ["app", "create_app", "VERSION", "TOKEN_HEADER", "TOKEN_COOKIE", "TOKEN_QUERY"]

VERSION = __version__

TOKEN_HEADER = "X-GapAudit-Token"
TOKEN_COOKIE = "gapaudit_token"
TOKEN_QUERY = "token"

TOKEN_ENV = "GAPAUDIT_TOKEN"
PORT_ENV = "GAPAUDIT_PORT"

ROOT = Path(__file__).resolve().parent.parent
VIEWS_DIR = ROOT / "web" / "views"
STATIC_DIR = ROOT / "web" / "static"

_ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost")


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


class LocalGuardMiddleware(BaseHTTPMiddleware):
    """Пускать только запросы с этой машины и только с валидным токеном на ``/api/*``."""

    def __init__(self, app, token: str, port: int | None = None) -> None:
        super().__init__(app)
        self.token = token
        self.port = port

    def _token_ok(self, request: Request) -> bool:
        supplied = [
            request.headers.get(TOKEN_HEADER),
            request.cookies.get(TOKEN_COOKIE),
            request.query_params.get(TOKEN_QUERY),
        ]
        return any(s and secrets.compare_digest(s, self.token) for s in supplied)

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
        if request.url.path.startswith("/api/") and not self._token_ok(request):
            return JSONResponse(
                {"ok": False, "detail": "forbidden: нужен токен сессии"}, status_code=403
            )
        return await call_next(request)


def create_app(token: str | None = None, port: int | None = None) -> FastAPI:
    """Собрать приложение с конкретным токеном и портом (порт нужен для проверки Host)."""
    token = token or os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(32)
    if port is None:
        raw_port = os.environ.get(PORT_ENV)
        port = int(raw_port) if raw_port and raw_port.isdigit() else None

    application = FastAPI(title="GapAudit", version=VERSION)
    application.state.token = token
    application.state.port = port
    application.add_middleware(LocalGuardMiddleware, token=token, port=port)

    templates = Jinja2Templates(directory=str(VIEWS_DIR))
    if STATIC_DIR.is_dir():
        # Локальная статика вместо CDN (правило из плана: никаких внешних CDN).
        application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @application.get("/api/health")
    async def health() -> dict:
        """Готовность бэкенда: по этому ответу окно решает, что можно открываться."""
        return {"ok": True, "version": VERSION}

    @application.get("/")
    async def index(request: Request):
        """Единственный экран каркаса: подтверждение, что связка живая."""
        return templates.TemplateResponse(
            request=request, name="index.html", context={"version": VERSION}
        )

    return application


app = create_app()
