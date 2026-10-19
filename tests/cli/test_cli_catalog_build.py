"""Тесты cli/catalog_build.py — фейки всех трёх источников, сеть ноль (шаг 1f).

GitHub подменён фейковым ``GhClient``, npm и реестр MCP идут реальными клиентами
поверх фикстурного транспорта — так проверяется и склейка ``core.catalog``.
"""

from __future__ import annotations

import json
import pathlib
import urllib.parse

import pytest

import core.http as http_module
from cli import catalog_build
from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.mcp_registry import MCP_SEARCH_URL
from core.registry import npm_search_url

PHRASE = "test impact analysis"

GITHUB_ITEM = {
    "full_name": "acme/impact-tool",
    "html_url": "https://github.com/acme/impact-tool",
    "description": "Impact analysis for test selection",
    "stargazers_count": 500,
    "language": "Python",
    "pushed_at": "2026-09-01T00:00:00Z",
    "topics": ["testing"],
}

NPM_PAYLOAD = {
    "objects": [
        {
            "package": {
                "name": "impact-tool",
                "description": "Test impact analysis CLI",
                "links": {"npm": "https://www.npmjs.com/package/impact-tool"},
                "date": "2026-09-02T00:00:00.000Z",
                "keywords": ["testing"],
            }
        }
    ]
}

MCP_PAYLOAD = {"servers": [], "metadata": {}}

FAST_HOSTS = {
    "registry.npmjs.org": 60000,
    "registry.modelcontextprotocol.io": 60000,
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def install(monkeypatch: pytest.MonkeyPatch, gh_items: list[dict]) -> list[tuple]:
    """Фейк GhClient + фикстурный транспорт для npm и реестра MCP."""
    calls: list[tuple] = []

    transport = _MapTransport({
        npm_search_url(PHRASE, 250): (200, json.dumps(NPM_PAYLOAD).encode("utf-8")),
        MCP_SEARCH_URL.format(q=urllib.parse.quote(PHRASE), limit=100): (
            200, json.dumps(MCP_PAYLOAD).encode("utf-8"),
        ),
    })

    def build(cache_path: str = "raw/.pcache.json") -> HttpClient:
        return HttpClient(
            cache=DictCache(),
            throttle=Throttle(per_minute=dict(FAST_HOSTS)),
            transport=transport,
            sleep=lambda _seconds: None,
        )

    monkeypatch.setattr(http_module, "default_client", build)

    class FakeGh:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def search_repositories(self, q: str, **kwargs: object) -> tuple[int, list[dict]]:
            calls.append((q, kwargs.get("page")))
            return len(gh_items), [dict(item) for item in gh_items]

    monkeypatch.setattr(catalog_build, "GhClient", FakeGh)
    monkeypatch.setattr(catalog_build, "setup_gh_or_warn", lambda: (True, ""))
    return calls


class _MapTransport:
    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = mapping

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        return self.mapping.get(url, (404, b"{}"))


def test_three_sources_dedupe_and_all_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    install(monkeypatch, [GITHUB_ITEM])
    out = tmp_path / "cat.json"
    md = tmp_path / "cat.md"
    summary = tmp_path / "summary.json"

    code = catalog_build.main(
        [PHRASE, "--out", str(out), "--md", str(md), "--json-out", str(summary)]
    )

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["queries"] == [PHRASE]
    assert payload["stats"]["total"] == 1
    # Один проект с GitHub и из npm склеен в одну строку каталога.
    row = payload["items"][0]
    assert set(row["sources"]) == {"github", "npm"}
    assert row["stars"] == 500

    markdown = md.read_text(encoding="utf-8")
    assert markdown.startswith("# Каталог: ")
    assert "impact-tool" in markdown

    stats = json.loads(summary.read_text(encoding="utf-8"))
    assert stats["total"] == 1
    assert "items" not in stats  # сводка без строк: сравнивать во времени


def test_no_npm_no_registry_keeps_only_github(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    install(monkeypatch, [GITHUB_ITEM])
    out = tmp_path / "cat.json"

    code = catalog_build.main(
        [PHRASE, "--no-npm", "--no-registry", "--out", str(out), "--quiet"]
    )

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["stats"]["by_source"] == {"github": 1}


def test_merge_includes_previous_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    install(monkeypatch, [GITHUB_ITEM])
    merge = tmp_path / "old.json"
    merge.write_text(
        json.dumps({
            "items": [{
                "source": "registry", "id": "old/impact-legacy", "url": "",
                "desc": "Legacy impact analyzer", "stars": None, "lang": None,
                "pushed": "", "topics": [], "query": "legacy",
            }]
        }),
        encoding="utf-8",
    )
    out = tmp_path / "cat.json"

    code = catalog_build.main([PHRASE, "--merge", str(merge), "--out", str(out), "--quiet"])

    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    ids = {row["id"] for row in payload["items"]}
    assert "old/impact-legacy" in ids
    assert "acme/impact-tool" in ids


def test_quiet_hides_report_but_keeps_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, [GITHUB_ITEM])
    out = tmp_path / "cat.json"

    code = catalog_build.main([PHRASE, "--out", str(out), "--quiet"])
    captured = capsys.readouterr()

    assert code == 0
    assert "=== КАТАЛОГ" not in captured.out
    assert "СОХРАНЕНО" in captured.out
    assert json.loads(out.read_text(encoding="utf-8"))["stats"]["total"] == 1
