"""Тесты cli/collect.py — фейковые HttpClient и GhClient, сеть ноль (шаг 1f).

CONFIG-словарь legacy становится флагами; тесты проверяют все три файла,
сводный ``--json-out`` и поведение без авторизованного gh.
"""

from __future__ import annotations

import json
import pathlib
import urllib.parse

import pytest

import core.http as http_module
from cli import collect
from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.mcp_registry import MCP_HOST, MCP_LIST_URL

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "mcp_registry"
BASE = MCP_LIST_URL.format(limit=100)
CURSOR = "academy.apexai/apex-ai-academy:1.0.1"
PAGE2_URL = BASE + "&cursor=" + urllib.parse.quote(CURSOR)

PAGE1 = json.loads((FIXTURES / "servers_page1.json").read_text(encoding="utf-8"))
PAGE2 = json.loads((FIXTURES / "servers_page2.json").read_text(encoding="utf-8"))
LAST_PAGE = {"servers": PAGE2["servers"], "metadata": {"count": len(PAGE2["servers"])}}

GH_ITEMS = [
    {
        "full_name": "acme/mcp-thing", "stargazers_count": 1200,
        "description": "MCP server thing", "topics": ["mcp"], "language": "Python",
        "pushed_at": "2026-09-01T00:00:00Z", "created_at": "2025-01-01T00:00:00Z",
        "forks_count": 3, "archived": False, "html_url": "https://github.com/acme/mcp-thing",
    },
    {
        "full_name": "beta/mcp-other", "stargazers_count": 900,
        "description": "Other MCP server", "topics": [], "language": "Go",
        "pushed_at": "2026-08-01T00:00:00Z", "created_at": "2025-02-01T00:00:00Z",
        "forks_count": 1, "archived": False, "html_url": "https://github.com/beta/mcp-other",
    },
]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


class FakeTransport:
    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        return self.mapping.get(url, (404, b'{"message": "Not Found"}'))


def install_http(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport({
        BASE: (200, json.dumps(PAGE1).encode("utf-8")),
        PAGE2_URL: (200, json.dumps(LAST_PAGE).encode("utf-8")),
    })

    def build(cache_path: str = "raw/.pcache.json") -> HttpClient:
        return HttpClient(
            cache=DictCache(),
            throttle=Throttle(per_minute={MCP_HOST: 60000}),
            transport=transport,
            sleep=lambda _seconds: None,
        )

    monkeypatch.setattr(http_module, "default_client", build)


def install_map(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, tuple[int, bytes]]) -> FakeTransport:
    """Транспорт по своему маппингу URL -> (статус, тело) — для кастомных --catalog-url."""
    transport = FakeTransport(mapping)

    def build(cache_path: str = "raw/.pcache.json") -> HttpClient:
        return HttpClient(
            cache=DictCache(),
            throttle=Throttle(per_minute={MCP_HOST: 60000}),
            transport=transport,
            sleep=lambda _seconds: None,
        )

    monkeypatch.setattr(http_module, "default_client", build)
    return transport


def install_gh(monkeypatch: pytest.MonkeyPatch, *, authorized: bool = True) -> list[tuple]:
    calls: list[tuple] = []

    class FakeGh:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def search_repositories(self, q: str, **kwargs: object) -> tuple[int, list[dict]]:
            calls.append((q, kwargs.get("page")))
            return len(GH_ITEMS), [dict(item) for item in GH_ITEMS]

    monkeypatch.setattr(collect, "GhClient", FakeGh)
    monkeypatch.setattr(collect, "setup_gh_or_warn", lambda: (authorized, "warn" if not authorized else ""))
    return calls


def test_all_three_files_and_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_http(monkeypatch)
    calls = install_gh(monkeypatch)
    out_dir = tmp_path / "out"
    summary = tmp_path / "summary.json"

    code = collect.main([
        "--keyword", "mcp",
        "--ecosystem-query", "topic:mcp-server",
        "--topic", "devops", "--topic", "cli",
        "--out-dir", str(out_dir),
        "--json-out", str(summary),
    ])

    assert code == 0
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    ecosystem = json.loads((out_dir / "ecosystem_repos.json").read_text(encoding="utf-8"))
    popular = json.loads((out_dir / "popular.json").read_text(encoding="utf-8"))
    stats = json.loads(summary.read_text(encoding="utf-8"))

    assert len(catalog) == 20  # две страницы фикстур по 10 серверов
    assert len(ecosystem) == 2
    assert len(popular) == 2
    assert stats == {"catalog_n": 20, "ecosystem_n": 2, "popular_n": 2}
    # Запросы: экосистема с stars:>0, темы — с min_stars по умолчанию (800).
    assert calls == [("topic:mcp-server stars:>0", 1), ("topic:devops stars:>800", 1),
                     ("topic:cli stars:>800", 1)]
    # Найденное по двум темам помечено двумя тегами — один репозиторий, две находки.
    assert popular["acme/mcp-thing"]["found_by"] == ["devops", "cli"]


def test_without_gh_skips_two_phases_and_exits_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_http(monkeypatch)
    install_gh(monkeypatch, authorized=False)
    out_dir = tmp_path / "out"
    summary = tmp_path / "summary.json"

    code = collect.main([
        "--keyword", "mcp",
        "--topic", "devops",
        "--out-dir", str(out_dir),
        "--json-out", str(summary),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert "пропущены" in captured.err  # предупреждение о gh ушло в stderr
    stats = json.loads(summary.read_text(encoding="utf-8"))
    assert stats["catalog_n"] == 20  # каталог живёт без gh
    assert stats["ecosystem_n"] == 0
    assert stats["popular_n"] == 0
    # Файлы пишутся даже пустыми: coverage.py должен отработать на пустом входе.
    assert json.loads((out_dir / "ecosystem_repos.json").read_text(encoding="utf-8")) == {}


def test_keyword_is_required(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install_http(monkeypatch)
    install_gh(monkeypatch)

    code = collect.main([])
    captured = capsys.readouterr()

    assert code == 2
    assert "--keyword обязателен" in captured.err


# --- каталог через list_servers_raw: флаги работают (шаг 1g) --------------------------


def test_catalog_url_flag_pulls_from_that_url_not_from_mcp_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    custom = "https://catalog.example/api?limit=10"
    payload = {"servers": [{"server": {"name": "custom/one", "description": "d"}}], "metadata": {"count": 1}}
    transport = install_map(monkeypatch, {custom: (200, json.dumps(payload).encode("utf-8"))})
    install_gh(monkeypatch, authorized=False)
    out_dir = tmp_path / "out"

    code = collect.main(["--keyword", "mcp", "--catalog-url", custom, "--out-dir", str(out_dir)])

    assert code == 0
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    # Шаг 1g: элементы — развёрнутые объекты серверов (list_servers_raw), не {"server": {...}}.
    assert [row["name"] for row in catalog] == ["custom/one"]
    assert transport.calls == [custom]  # на MCP_LIST_URL запроса не было


def test_catalog_items_key_flag_reads_that_key_and_ignores_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    payload = {
        "servers": [{"server": {"name": "wrong/one", "description": ""}}],
        "my_items": [{"server": {"name": "custom/two", "description": "d"}}],
        "metadata": {"count": 1},
    }
    install_map(monkeypatch, {BASE: (200, json.dumps(payload).encode("utf-8"))})
    install_gh(monkeypatch, authorized=False)
    out_dir = tmp_path / "out"

    code = collect.main([
        "--keyword", "mcp", "--catalog-items-key", "my_items", "--out-dir", str(out_dir),
    ])

    assert code == 0
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    assert [row["name"] for row in catalog] == ["custom/two"]  # data["servers"] игнорируется


def test_catalog_cursor_path_flag_reads_custom_location(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    page1 = {"my_items": [{"server": {"name": "a/one", "description": ""}}], "metadata": {"pageToken": "tok-1"}}
    page2 = {"my_items": [{"server": {"name": "a/two", "description": ""}}], "metadata": {"count": 2}}
    url2 = BASE + "&cursor=" + urllib.parse.quote("tok-1")
    install_map(monkeypatch, {
        BASE: (200, json.dumps(page1).encode("utf-8")),
        url2: (200, json.dumps(page2).encode("utf-8")),
    })
    install_gh(monkeypatch, authorized=False)
    out_dir = tmp_path / "out"

    code = collect.main([
        "--keyword", "mcp",
        "--catalog-items-key", "my_items",
        "--catalog-cursor-path", "metadata", "pageToken",
        "--out-dir", str(out_dir),
    ])

    assert code == 0
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    # Курсор прочитан из metadata.pageToken (не nextCursor): обход дошёл до второй страницы.
    assert [row["name"] for row in catalog] == ["a/one", "a/two"]


def test_catalog_break_on_error_page_prints_obryv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_map(monkeypatch, {BASE: (500, b"boom")})
    install_gh(monkeypatch, authorized=False)
    out_dir = tmp_path / "out"

    code = collect.main(["--keyword", "mcp", "--out-dir", str(out_dir)])
    out = capsys.readouterr().out

    assert code == 0
    assert "catalog: обрыв на странице 0" in out
    assert json.loads((out_dir / "catalog.json").read_text(encoding="utf-8")) == []


def test_catalog_without_flags_paginates_default_registry_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Дефолтный --catalog-url — реестр MCP; обе страницы уходят туда же (шаг 1g)."""
    transport = install_map(monkeypatch, {
        BASE: (200, json.dumps(PAGE1).encode("utf-8")),
        PAGE2_URL: (200, json.dumps(LAST_PAGE).encode("utf-8")),
    })
    install_gh(monkeypatch, authorized=False)
    out_dir = tmp_path / "out"

    code = collect.main(["--keyword", "mcp", "--out-dir", str(out_dir)])

    assert code == 0
    catalog = json.loads((out_dir / "catalog.json").read_text(encoding="utf-8"))
    assert len(catalog) == 20
    assert transport.calls == [BASE, PAGE2_URL]
