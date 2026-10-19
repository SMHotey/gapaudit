"""Тесты core/mcp_registry.py — фикстуры реестра MCP, сети нет (шаг 1d).

Фикстуры tests/fixtures/mcp_registry/ сняты один раз из живого реестра:
``servers_page1.json`` (limit=10, без курсора), ``servers_page2.json`` (та же страница
по курсору первой), ``servers_search_test_impact.json`` (поиск по фразе — пустая выдача,
реестр матчит узко) и ``servers_search_impact.json`` (поиск, который что-то находит).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.mcp_registry import (
    DEFAULT_CURSOR_PATH,
    DEFAULT_LIMIT,
    MCP_HOST,
    MCP_LIST_URL,
    MCP_SEARCH_URL,
    extract_cursor,
    list_servers,
    list_servers_raw,
    next_url,
    normalize_server,
    search,
    servers_page,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "mcp_registry"
PAGE1 = "servers_page1.json"
PAGE2 = "servers_page2.json"
SEARCH_EMPTY = "servers_search_test_impact.json"
SEARCH_IMPACT = "servers_search_impact.json"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def body(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def page_cursor(name: str) -> str | None:
    return extract_cursor(fixture(name))


PAGE1_CURSOR = page_cursor(PAGE1)
PAGE2_CURSOR = page_cursor(PAGE2)


class FakeTransport:
    """Транспорт по карте {url: (status, body)}; неизвестный URL — 404."""

    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        return self.mapping.get(url, (404, b'{"message": "Not Found"}'))


def make_http(mapping: dict[str, tuple[int, bytes]] | None = None) -> tuple[HttpClient, FakeTransport]:
    transport = FakeTransport(mapping or {})
    http = HttpClient(
        cache=DictCache(),
        throttle=Throttle(per_minute={MCP_HOST: 60000}),
        transport=transport,
        sleep=lambda _seconds: None,
    )
    return http, transport


def list_url(cursor: str | None, limit: int) -> str:
    return next_url(MCP_LIST_URL.format(limit=int(limit)), cursor, limit)


# --- normalize_server ----------------------------------------------------------------------


def test_normalize_server_gives_catalog_row_from_real_fixture() -> None:
    server = fixture(PAGE1)["servers"][0]["server"]

    row = normalize_server(server)

    assert set(row) == {"source", "id", "url", "desc", "stars", "lang", "pushed", "topics"}
    assert row["source"] == "registry"
    assert row["id"] == server["name"]
    assert row["url"] == ""
    assert row["desc"] == server["description"][:200]
    assert row["stars"] is None
    assert row["lang"] is None
    assert row["pushed"] == ""
    assert row["topics"] == []


def test_normalize_server_truncates_description_to_200() -> None:
    row = normalize_server({"name": "x/y", "description": "я" * 300})

    assert len(row["desc"]) == 200


def test_normalize_server_survives_garbage() -> None:
    assert normalize_server({"name": None})["id"] is None
    assert normalize_server({})["desc"] == ""
    assert normalize_server("не словарь")["id"] is None  # type: ignore[arg-type]


# --- extract_cursor ------------------------------------------------------------------------


def test_extract_cursor_reads_next_cursor_from_both_real_pages() -> None:
    """В фикстурах курсор есть на обеих страницах: обход реестра далеко не закончен."""
    assert isinstance(PAGE1_CURSOR, str) and PAGE1_CURSOR
    assert isinstance(PAGE2_CURSOR, str) and PAGE2_CURSOR
    assert PAGE1_CURSOR == fixture(PAGE1)["metadata"]["nextCursor"]


def test_extract_cursor_returns_none_on_last_page() -> None:
    """Последняя страница приходит без курсора (или без метаданных вовсе)."""
    assert extract_cursor({"servers": [], "metadata": {"count": 0}}) is None
    assert extract_cursor({"servers": []}) is None


def test_extract_cursor_ignores_wrong_key_next_cursor() -> None:
    """Баг «nextCursor vs next_cursor»: чужой ключ — курсора нет, исключения тоже нет."""
    assert extract_cursor({"metadata": {"next_cursor": "X"}}) is None
    # Рабочий ключ рядом с чужим не спасает: значение испорчено, обход надо прекратить.
    assert extract_cursor({"metadata": {"next_cursor": "X", "nextCursor": "Y"}}) is None


@pytest.mark.parametrize(
    "data",
    [None, {}, {"metadata": {}}, {"metadata": None}, {"metadata": "строка"}, {"servers": []}],
)
def test_extract_cursor_none_on_empty_or_broken_payload(data: object) -> None:
    assert extract_cursor(data) is None  # type: ignore[arg-type]


def test_extract_cursor_uses_empty_string_as_no_cursor() -> None:
    assert extract_cursor({"metadata": {"nextCursor": ""}}) is None
    assert extract_cursor({"metadata": {"nextCursor": 42}}) is None


def test_extract_cursor_respects_custom_path() -> None:
    data = {"meta": {"cursor": "abc"}}

    assert extract_cursor(data, ("meta", "cursor")) == "abc"
    assert extract_cursor(data, DEFAULT_CURSOR_PATH) is None


# --- next_url ------------------------------------------------------------------------------


def test_next_url_without_cursor_adds_limit() -> None:
    assert next_url("https://h.test/v0/servers", None, 100) == "https://h.test/v0/servers?limit=100"


def test_next_url_with_query_and_cursor() -> None:
    url = next_url("https://h.test/v0/servers?search=x", "abc/1:2", 10)

    assert url.startswith("https://h.test/v0/servers?search=x&cursor=")
    assert url.endswith("&limit=10")
    assert "abc" in url and "1%3A2" in url  # курсор квотируется целиком


def test_next_url_does_not_duplicate_limit() -> None:
    """``MCP_LIST_URL`` уже содержит limit — второй раз он не добавляется."""
    url = next_url(MCP_LIST_URL.format(limit=10), None, 10)

    assert url == "https://registry.modelcontextprotocol.io/v0/servers?limit=10"


def test_next_url_limit_default_is_default_limit() -> None:
    assert next_url("https://h.test/v0/servers", None).endswith(f"limit={DEFAULT_LIMIT}")


# --- list_servers --------------------------------------------------------------------------


def test_list_servers_first_page_returns_rows_and_cursor() -> None:
    payload = fixture(PAGE1)
    http, transport = make_http({list_url(None, 10): (200, body(payload))})

    items, cursor = list_servers(http, limit=10)

    assert len(items) == len(payload["servers"]) == 10
    assert cursor == payload["metadata"]["nextCursor"] == PAGE1_CURSOR
    assert items[0]["source"] == "registry"
    assert items[0]["id"] == payload["servers"][0]["server"]["name"]
    assert transport.calls == [list_url(None, 10)]
    assert "cursor=" not in transport.calls[0]


def test_list_servers_second_page_uses_cursor() -> None:
    payload = fixture(PAGE2)
    url = list_url(PAGE1_CURSOR, 10)
    http, transport = make_http({url: (200, body(payload))})

    items, cursor = list_servers(http, cursor=PAGE1_CURSOR, limit=10)

    assert len(items) == 10
    assert cursor == payload["metadata"]["nextCursor"]
    assert transport.calls == [url]
    assert "cursor=" in transport.calls[0]


def test_list_servers_last_page_has_no_cursor() -> None:
    payload = {"servers": [{"server": {"name": "a/b", "description": "d"}}], "metadata": {"count": 1}}
    http, _ = make_http({list_url(None, 10): (200, body(payload))})

    items, cursor = list_servers(http, limit=10)

    assert [row["id"] for row in items] == ["a/b"]
    assert cursor is None


def test_list_servers_error_returns_empty_and_no_cursor() -> None:
    http, transport = make_http({list_url(None, 10): (500, b"boom")})

    assert list_servers(http, limit=10) == ([], None)
    assert len(transport.calls) == 3  # три попытки, как в DEFAULT_TRIES


def test_list_servers_404_is_final() -> None:
    http, transport = make_http({})

    assert list_servers(http, limit=10) == ([], None)
    assert len(transport.calls) == 1


def test_list_servers_skips_broken_items() -> None:
    payload = {"servers": ["мусор", {"server": "нет"}, {"server": {"name": "ok", "description": "d"}}]}
    http, _ = make_http({list_url(None, 10): (200, body(payload))})

    items, cursor = list_servers(http, limit=10)

    assert [row["id"] for row in items] == ["ok"]
    assert cursor is None


# --- search --------------------------------------------------------------------------------


def test_search_on_phrase_returns_empty_for_narrow_registry() -> None:
    """По фразе «test impact analysis» реестр отдаёт пусто — как и в legacy-прогоне."""
    url = MCP_SEARCH_URL.format(q="test%20impact%20analysis", limit=10)
    http, transport = make_http({url: (200, body(fixture(SEARCH_EMPTY)))})

    assert search(http, "test impact analysis", limit=10) == []
    assert transport.calls == [url]


def test_search_returns_normalized_rows_when_registry_matches() -> None:
    url = MCP_SEARCH_URL.format(q="impact", limit=10)
    payload = fixture(SEARCH_IMPACT)
    http, transport = make_http({url: (200, body(payload))})

    rows = search(http, "impact", limit=10)

    assert len(rows) == len(payload["servers"]) == 10
    assert all(row["source"] == "registry" for row in rows)
    assert [row["id"] for row in rows] == [item["server"]["name"] for item in payload["servers"]]
    assert transport.calls == [url]


def test_search_error_returns_empty_list() -> None:
    url = MCP_SEARCH_URL.format(q="impact", limit=10)
    http, transport = make_http({url: (403, b"forbidden")})

    assert search(http, "impact", limit=10) == []
    assert transport.calls == [url]


def test_search_quotes_query_and_limit() -> None:
    # quote() с safe="/" — как в legacy: пробел квотируется, слэш остаётся собой.
    url = MCP_SEARCH_URL.format(q="a%20b/c", limit=100)
    http, transport = make_http({url: (200, body({"servers": []}))})

    search(http, "a b/c", limit=100)

    assert transport.calls == [url]
    assert transport.calls[0].startswith("https://" + MCP_HOST)


# --- list_servers_raw / servers_page (шаг 1e) ----------------------------------------------


def test_list_servers_raw_first_page_keeps_raw_fields() -> None:
    """Сырая страница: объекты серверов как есть — с ``remotes``/``packages``."""
    payload = fixture(PAGE1)
    http, transport = make_http({list_url(None, 10): (200, body(payload))})

    servers, cursor = list_servers_raw(http, limit=10)

    assert len(servers) == 10
    assert servers[0] == payload["servers"][0]["server"]  # без нормализации
    assert "remotes" in servers[0]
    assert cursor == PAGE1_CURSOR
    assert transport.calls == [list_url(None, 10)]


def test_list_servers_raw_second_page_uses_cursor() -> None:
    payload = fixture(PAGE2)
    url = list_url(PAGE1_CURSOR, 10)
    http, transport = make_http({url: (200, body(payload))})

    servers, cursor = list_servers_raw(http, cursor=PAGE1_CURSOR, limit=10)

    assert len(servers) == 10
    assert servers[-1] == payload["servers"][-1]["server"]
    assert cursor == payload["metadata"]["nextCursor"]
    assert transport.calls == [url]


def test_list_servers_raw_error_is_empty_page() -> None:
    """Контракт 1d не изменился: сбой сети — ``([], None)``, а не исключение."""
    http, transport = make_http({list_url(None, 10): (500, b"boom")})

    assert list_servers_raw(http, limit=10) == ([], None)
    assert len(transport.calls) == 3


def test_servers_page_distinguishes_empty_catalog_from_failed_page() -> None:
    """Пустая страница — ``([], None)``, не полученная — ``(None, None)``.

    Различие принципиальное: обходчик каталога (шаг 1e) по пустой странице объявляет
    обход законченным, а по не полученной — сохраняет курсор и продолжает позже.
    """
    empty = {"servers": [], "metadata": {"count": 0}}
    ok, _ = make_http({list_url(None, 10): (200, body(empty))})
    boom, _ = make_http({list_url(None, 10): (500, b"boom")})
    missing, _ = make_http({})  # 404

    assert servers_page(ok, limit=10) == ([], None)
    assert servers_page(boom, limit=10) == (None, None)
    assert servers_page(missing, limit=10) == (None, None)
