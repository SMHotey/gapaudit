"""Реестр MCP (registry.modelcontextprotocol.io) — поиск и постраничный обход (шаг 1d).

Порт legacy ``registry_search()`` из ``catalog_build.py`` с одним расширением:
кроме поиска по фразе есть полный обход реестра по курсору (``list_servers``) —
он нужен шагу 1e (``catalog_scan``), поэтому пагинация живёт здесь, а не в обёртке.

Троттлинг и кэш — в инжектированном :class:`core.http.HttpClient`; на уровне модуля
ничего не кэшируется, ``print`` нет, файлов модуль не трогает.

Про курсор: в ответе реестра он лежит в ``metadata.nextCursor``. В документации
``docs-usage.md`` зафиксирован баг «nextCursor vs next_cursor» — ключ легко перепутать
и получить бесконечный обход одной страницы, поэтому :func:`extract_cursor` различает
оба написания: рабочее берёт, чужое — игнорирует и возвращает ``None``.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from core.http import HttpClient

__all__ = [
    "DEFAULT_CURSOR_PATH",
    "DEFAULT_ITEMS_KEY",
    "DEFAULT_LIMIT",
    "DESC_LIMIT",
    "MCP_HOST",
    "MCP_LIST_URL",
    "MCP_SEARCH_URL",
    "WRONG_CURSOR_KEY",
    "extract_cursor",
    "list_servers",
    "list_servers_raw",
    "next_url",
    "normalize_server",
    "search",
    "servers_page",
]

MCP_HOST = "registry.modelcontextprotocol.io"
MCP_SEARCH_URL = (
    "https://registry.modelcontextprotocol.io/v0/servers?search={q}&limit={limit}"
)
MCP_LIST_URL = "https://registry.modelcontextprotocol.io/v0/servers?limit={limit}"

#: Путь до курсора в ответе; кортеж, чтобы обход был одинаков для вложенных ответов.
DEFAULT_CURSOR_PATH = ("metadata", "nextCursor")
#: Ключ списка элементов в ответе каталога (как в реестре MCP).
DEFAULT_ITEMS_KEY = "servers"
#: Написание, которым курсор НЕ является (защита от бага из docs-usage.md).
WRONG_CURSOR_KEY = "next_cursor"
DEFAULT_LIMIT = 100
DESC_LIMIT = 200


def normalize_server(server: dict) -> dict:
    """Сервер реестра -> строка каталога (форма, которую понимает ``dedupe()``).

    Берётся внутренний объект ``servers[].server``: имя, описание. Звёзд и языка
    у сервера реестра нет, дату push реестр не отдаёт — поля остаются пустыми,
    чтобы каталог из разных источников склеивался без спецобработки.
    """
    if not isinstance(server, dict):
        server = {}
    name = server.get("name")
    return {
        "source": "registry",
        "id": name if isinstance(name, str) and name else None,
        "url": "",
        "desc": str(server.get("description") or "")[:DESC_LIMIT],
        "stars": None,
        "lang": None,
        "pushed": "",
        "topics": [],
    }


def _servers(data: Any, items_key: str = DEFAULT_ITEMS_KEY) -> list[dict]:
    """Внутренние объекты ``server`` из ответа реестра (мусор пропускаем).

    ``items_key`` — ключ списка элементов: у реестра это ``servers``, но пагинируемый
    JSON-каталог другой раздачи может отдавать список под другим именем (шаг 1g).
    """
    if not isinstance(data, dict):
        return []
    items = data.get(items_key)
    if not isinstance(items, list):
        return []
    servers: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        server = item.get("server")
        if isinstance(server, dict):
            servers.append(server)
    return servers


def extract_cursor(data: dict | None, cursor_path: tuple[str, ...] = DEFAULT_CURSOR_PATH) -> str | None:
    """Курсор следующей страницы из ответа реестра; ``None`` — обход закончен.

    ``None`` возвращается и в защитном случае: если на верхнем уровне появился чужой
    ключ ``next_cursor`` (а не ``nextCursor``), курсор считается испорченным. Обход
    после этого просто остановится — это лучше, чем крутить одну страницу вечно
    (см. баг «nextCursor vs next_cursor» в ``docs-usage.md``).
    """
    if not isinstance(data, dict):
        return None

    node: Any = data
    for key in cursor_path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None

    if WRONG_CURSOR_KEY in node:
        return None

    value = node.get(cursor_path[-1]) if cursor_path else None
    if isinstance(value, str) and value:
        return value
    return None


def next_url(base_url: str, cursor: str | None, limit: int = DEFAULT_LIMIT) -> str:
    """URL следующей страницы: ``base_url`` + ``cursor`` + ``limit``.

    Разделитель выбирается по наличию ``?`` в ``base_url``. ``limit`` не дублируется,
    если он уже есть в ``base_url`` (:data:`MCP_LIST_URL` содержит его в себе).
    """
    url = base_url
    separator = "&" if "?" in url else "?"
    if cursor:
        url += f"{separator}cursor={urllib.parse.quote(str(cursor))}"
        separator = "&"
    if "limit=" not in url:
        url += f"{separator}limit={int(limit)}"
    return url


def list_servers(
    http: HttpClient,
    *,
    base_url: str = MCP_LIST_URL,
    items_key: str = DEFAULT_ITEMS_KEY,
    cursor_path: tuple[str, ...] = DEFAULT_CURSOR_PATH,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict], str | None]:
    """Одна страница полного обхода реестра: ``(строки каталога, следующий курсор)``.

    Ошибка сети или статуса — ``([], None)``: обход завершается, вызывающий код
    (шаг 1e) решает, повторять ли попытку. Ничего не печатает.
    """
    servers, next_cursor = list_servers_raw(
        http,
        base_url=base_url,
        items_key=items_key,
        cursor_path=cursor_path,
        cursor=cursor,
        limit=limit,
    )
    return [normalize_server(server) for server in servers], next_cursor


def servers_page(
    http: HttpClient,
    *,
    base_url: str = MCP_LIST_URL,
    items_key: str = DEFAULT_ITEMS_KEY,
    cursor_path: tuple[str, ...] = DEFAULT_CURSOR_PATH,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict] | None, str | None]:
    """Сырая страница обхода: ``(объекты серверов, следующий курсор)``.

    ``base_url`` — шаблон URL каталога с ``{limit}`` (шаг 1g: ``--base-url``/``--catalog-url``
    CLI перестают быть декоративными); курсор добавляется к уже собранному URL, поэтому
    разделитель выбирается по наличию ``?``. ``items_key`` — ключ списка элементов в ответе,
    ``cursor_path`` — путь до курсора (см. :func:`extract_cursor`).

    Отличие от :func:`list_servers_raw` — в одном случае: не полученная страница
    отдаёт ``None`` вместо пустого списка. Слить «серверов на странице нет» и
    «страница не пришла» в ``[]`` нельзя: обходчик (шаг 1e) обязан различать
    «каталог кончился, ``finished=True``» и «сеть отвалилась, курсор сохранён,
    продолжаем позже» — а из ``([], None)`` эти два исхода неразличимы.
    Пустая страница каталога приходит как ``([], ...)``; ``None`` — только сбой.
    """
    url = next_url(base_url.format(limit=int(limit)), cursor, limit)
    status, data = http.get(url)
    if status != 200 or not isinstance(data, dict):
        return None, None
    return _servers(data, items_key), extract_cursor(data, cursor_path)


def list_servers_raw(
    http: HttpClient,
    *,
    base_url: str = MCP_LIST_URL,
    items_key: str = DEFAULT_ITEMS_KEY,
    cursor_path: tuple[str, ...] = DEFAULT_CURSOR_PATH,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict], str | None]:
    """То же, что :func:`list_servers`, но без нормализации строк каталога.

    Нужен там, где важны поля, которых в строке каталога нет: обходчик каталога
    (шаг 1e) считает тип поставки по ``remotes``/``packages``. Не полученная
    страница — ``([], None)``, как у :func:`list_servers`: различение «сбой» и
    «пустая страница» — контракт :func:`servers_page`, эта функция — legacy-обёртка.
    """
    servers, next_cursor = servers_page(
        http,
        base_url=base_url,
        items_key=items_key,
        cursor_path=cursor_path,
        cursor=cursor,
        limit=limit,
    )
    if servers is None:
        return [], None
    return servers, next_cursor


def search(http: HttpClient, query: str, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Поиск по реестру MCP по фразе — одна страница, без пагинации.

    Так же, как в legacy ``registry_search()``: реестр матчит узко (по фразе
    «test impact analysis» выдача пуста), поэтому это дополнительный источник,
    а не основной. Ошибка сети или статуса — ``[]``.
    """
    url = MCP_SEARCH_URL.format(q=urllib.parse.quote(query), limit=int(limit))
    status, data = http.get(url)
    if status != 200 or not isinstance(data, dict):
        return []
    return [normalize_server(server) for server in _servers(data)]
