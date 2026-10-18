"""HTTP-транспорт GapAudit: повторы, троттлинг по хосту, кэш (шаг 1a).

Модуль — замена функции ``get()`` из ``legacy/check_predecessors.py``, но без глобалов:

* кэш приходит снаружи (:class:`core.cache.CacheBackend`);
* транспорт инжектируется (``Transport``) — в тестах сеть не нужна;
* троттлинг — объект :class:`Throttle`, его можно разделить между клиентом и
  подпроцессами ``gh`` (шаг 1b).

Форма ответа совпадает с legacy: ``(status, parsed_or_raw)``, для отсутствующего
объекта ``(404, None)``, после исчерпания попыток — ``(None, None)``.

Ничего не печатает: прогресс и логи — забота CLI-обёртки (шаг 1e).
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

from core.cache import CacheBackend, FileCache

__all__ = [
    "CACHE_KEY_PREFIX",
    "DEFAULT_PER_MINUTE",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TRIES",
    "DEFAULT_USER_AGENT",
    "GITHUB_SEARCH_PER_MINUTE",
    "HttpClient",
    "Throttle",
    "Transport",
    "default_client",
    "default_throttle",
    "urllib_transport",
]

#: (url, headers, timeout) -> (status, body_bytes)
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]

DEFAULT_USER_AGENT = "ecosystem-gap-audit"
DEFAULT_TIMEOUT = 25.0
DEFAULT_TRIES = 3
CACHE_KEY_PREFIX = "GET "

#: Задержки между попытками, секунды: 2, 4, 8, 16, 20, 20, ... (как в legacy).
BACKOFF = (2.0, 4.0, 8.0, 16.0, 20.0)

#: 404/410 — валидный «отрицательный ответ», кэшируется как успех.
NEGATIVE_STATUSES = frozenset({404, 410})

DEFAULT_PER_MINUTE = 60
#: GitHub Search живёт на том же хосте, что и остальные эндпоинты, поэтому
#: лимит 30/мин выставляет вызывающий: ``throttle.set_rate("api.github.com", 30)``.
GITHUB_SEARCH_PER_MINUTE = 30

#: Ошибки, после которых запрос имеет смысл повторить.
TRANSPORT_ERRORS = (
    urllib.error.URLError,  # включает таймаут urllib
    socket.timeout,
    TimeoutError,
    ConnectionError,
    OSError,
)


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """Транспорт по умолчанию на stdlib: ``urllib.request``, без записи на диск.

    ``HTTPError`` превращается в обычный ответ ``(code, body)`` — статус 4xx/5xx
    это не исключение, а данные. Сетевые сбои (``URLError``) пробрасываются:
    их обрабатывает :meth:`HttpClient.get`.
    """
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status if response.status is not None else response.getcode())
            return status, response.read()
    except urllib.error.HTTPError as error:
        body = b""
        try:
            body = error.read() or b""
        except Exception:  # тело могло быть уже прочитано/закрыто
            body = b""
        return int(error.code), body


class Throttle:
    """Троттлинг по хосту: не чаще ``per_minute`` запросов в минуту на хост."""

    def __init__(
        self,
        per_minute: dict[str, int] | None = None,
        default_per_minute: int = DEFAULT_PER_MINUTE,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._per_minute: dict[str, int] = dict(per_minute or {})
        self._default_per_minute = int(default_per_minute)
        self._clock = clock
        self._sleep = sleep
        self._next_allowed: dict[str, float] = {}

    def rate_for(self, host: str) -> int:
        """Лимит запросов в минуту для хоста (0 и меньше — без ограничения)."""
        return int(self._per_minute.get(host, self._default_per_minute))

    def set_rate(self, host: str, per_minute: int) -> None:
        """Переопределить лимит на лету (например, 30/мин для GitHub Search)."""
        self._per_minute[host] = int(per_minute)

    def interval(self, host: str) -> float:
        """Минимальный интервал между запросами к хосту, секунды."""
        rate = self.rate_for(host)
        return 0.0 if rate <= 0 else 60.0 / rate

    def wait(self, host: str) -> float:
        """Блокирует до разрешённого момента. Возвращает, сколько секунд ждали."""
        now = self._clock()
        allowed_at = self._next_allowed.get(host, 0.0)
        waited = allowed_at - now
        if waited > 0:
            self._sleep(waited)
            now = max(now, allowed_at)
        else:
            waited = 0.0
        self._next_allowed[host] = now + self.interval(host)
        return waited


def host_of(url: str) -> str:
    """Хост URL (для троттлинга); пустая строка, если хост не разобрался."""
    return (urlsplit(url).hostname or "").lower()


def default_throttle() -> Throttle:
    """Стандартные лимиты проекта.

    ``pypistats.org`` склонен к 429, поэтому 30/мин. GitHub Search ограничен
    отдельно, вызывающим кодом: Search и остальные эндпоинты — один хост.
    """
    return Throttle(
        per_minute={"api.github.com": DEFAULT_PER_MINUTE, "pypistats.org": 30},
        default_per_minute=DEFAULT_PER_MINUTE,
    )


def _backoff_delay(attempt: int, schedule: tuple[float, ...]) -> float:
    """Задержка перед попыткой ``attempt + 1``: 2, 4, 8, 16, 20, 20, ..."""
    if not schedule:
        return 0.0
    return float(schedule[min(attempt, len(schedule) - 1)])


class HttpClient:
    """GET с кэшем, троттлингом и повторами — один на процесс."""

    def __init__(
        self,
        cache: CacheBackend | None = None,
        throttle: Throttle | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: Transport | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        backoff: tuple[float, ...] = BACKOFF,
    ) -> None:
        self.cache = cache
        self.throttle = throttle if throttle is not None else default_throttle()
        self.user_agent = user_agent
        self._transport: Transport = transport if transport is not None else urllib_transport
        self._sleep = sleep
        self._backoff = tuple(backoff)

    # --- контракт -----------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        tries: int = DEFAULT_TRIES,
        raw: bool = False,
        use_cache: bool = True,
    ) -> tuple[int | None, Any]:
        """Получить URL: ``(status, value)``, промах по всем попыткам — ``(None, None)``.

        Кэш: ключ ``"GET " + url`` (как в legacy), значение — ``(status, value)``.
        При ``use_cache=False`` сеть дёргается принудительно (кнопка «обновить»),
        но полученный ответ кэш всё равно обновляет.
        """
        key = CACHE_KEY_PREFIX + url
        if use_cache and self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                status, value = _unpack(cached[1])
                return status, value

        attempts = max(1, int(tries))
        for attempt in range(attempts):
            self.throttle.wait(host_of(url))
            try:
                status, body = self._transport(url, self._headers(), timeout)
            except TRANSPORT_ERRORS:
                if attempt + 1 >= attempts:
                    break
                self._sleep(_backoff_delay(attempt, self._backoff))
                continue

            status = int(status)
            if status in NEGATIVE_STATUSES:
                self._remember(key, status, None, cacheable=True)
                return status, None

            if status == 429 or status >= 500:
                if attempt + 1 >= attempts:
                    break
                self._sleep(_backoff_delay(attempt, self._backoff))
                continue

            if 400 <= status < 500:
                # Прочие 4xx (401/403/400) повтором не лечатся: ответ финальный.
                return status, None

            value, cacheable = _parse_body(body, raw=raw)
            self._remember(key, status, value, cacheable=cacheable)
            return status, value

        return None, None

    # --- внутреннее ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent}

    def _remember(self, key: str, status: int, value: Any, *, cacheable: bool) -> None:
        """Записать ответ в кэш.

        ``use_cache=False`` управляет только чтением: свежий ответ всё равно
        обновляет кэш, иначе кнопка «обновить» в UI показывала бы старое значение.
        """
        if self.cache is None or not cacheable:
            return
        self.cache.put(key, (status, value))


def _unpack(cached: Any) -> tuple[int | None, Any]:
    """Значение из кэша — либо ``(status, value)``, либо что-то из чужого формата."""
    if isinstance(cached, (list, tuple)) and len(cached) == 2:
        return cached[0], cached[1]
    return None, cached


def _parse_body(body: Any, *, raw: bool) -> tuple[Any, bool]:
    """Разобрать тело ответа: ``(value, cacheable)``.

    Тело разбирается в JSON всегда, даже при ``raw=True``: JSON компактнее сырого
    текста, а в кэш кладётся значение, которое переживёт ``json.dump``. Если тело
    не JSON при ``raw=True`` — возвращаем байты и не кэшируем (байты в JSON-файле
    требуют base64, а кэш должен оставаться читаемым).
    """
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    try:
        return json.loads(text), True
    except ValueError:
        if raw:
            return (body if isinstance(body, (bytes, bytearray)) else text.encode("utf-8")), False
        return None, False


def default_client(cache_path: str = "raw/.pcache.json") -> HttpClient:
    """Клиент для CLI: файловый кэш и стандартный троттлинг.

    Кэш персистится вызовом ``client.cache.save()`` в конце прогона — как
    ``_save_cache()`` в legacy. Импорт и конструктор файл не трогают.
    """
    return HttpClient(cache=FileCache(cache_path), throttle=default_throttle())
