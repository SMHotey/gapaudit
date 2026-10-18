"""Тесты core/catalog.py — фикстуры, фейковые клиенты, сети нет (шаг 1d).

GitHub отдаёт фейковый runner (:class:`FakeGh`, фикстура ``catalog/gh_search_test_impact.json``
— реальный ответ ``gh api search/repositories``), npm и реестр MCP — фейковый транспорт
``HttpClient`` по карте URL. Все три клиента инжектируются в ``build_catalog()``, поэтому
сеть в этом файле не нужна ни разу.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import subprocess
import sys

import pytest

from core.cache import DictCache
from core.catalog import (
    GH_PER_PAGE,
    NPM_SIZE,
    build_catalog,
    dedupe,
    gh_search,
    is_noise,
    registry_search,
    relevant,
    report,
    short_name,
    to_markdown,
    words_of,
)
from core.github import GhClient
from core.http import HttpClient, Throttle
from core.mcp_registry import MCP_HOST, MCP_SEARCH_URL
from core.registry import RegistryClient, npm_search_url

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
GH_FIXTURE = FIXTURES / "catalog" / "gh_search_test_impact.json"
MCP_EMPTY_FIXTURE = FIXTURES / "mcp_registry" / "servers_search_test_impact.json"
MCP_IMPACT_FIXTURE = FIXTURES / "mcp_registry" / "servers_search_impact.json"

PHRASE = "test impact analysis"
WORDS = ["test", "impact", "analysis"]
ARCHIVE = datetime.date(2020, 1, 1)


def fixture_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def fixture_json(path: pathlib.Path) -> dict:
    return json.loads(fixture_text(path))


def days_ago(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


class FakeGh:
    """Runner: ответ по подстроке в ``q=``, иначе пустая выдача; пишет argv."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        query = next((item.split("=", 1)[1] for item in argv if item.startswith("q=")), "")
        for fragment, text in self.mapping.items():
            if fragment in query:
                return 0, text, ""
        return 0, json.dumps({"total_count": 0, "items": []}), ""

    @property
    def queries(self) -> list[str]:
        return [
            next((item.split("=", 1)[1] for item in argv if item.startswith("q=")), "")
            for argv in self.calls
        ]


class FakeTransport:
    """Транспорт по карте {url: (status, body)}; неизвестный URL — 404."""

    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        return self.mapping.get(url, (404, b'{"message": "Not Found"}'))


def make_http(host: str, mapping: dict[str, tuple[int, bytes]]) -> tuple[HttpClient, FakeTransport]:
    transport = FakeTransport(mapping)
    http = HttpClient(
        cache=DictCache(),
        throttle=Throttle(per_minute={host: 60000}),
        transport=transport,
        sleep=lambda _seconds: None,
    )
    return http, transport


def npm_payload() -> dict:
    """Выдача npm search: релевантный пакет, шум и пакет-дубль к находке GitHub."""
    return {
        "total": 3,
        "objects": [
            {
                "package": {
                    "name": "test-impact-analyzer",
                    "description": "Selects tests affected by a code change",
                    "keywords": ["testing"],
                    "date": "2026-01-15T00:00:00.000Z",
                    "links": {"npm": "https://www.npmjs.com/package/test-impact-analyzer"},
                }
            },
            {
                "package": {
                    "name": "awesome-test-list",
                    "description": "Curated awesome list of testing resources",
                    "keywords": [],
                    "date": "2024-01-01T00:00:00.000Z",
                    "links": {"npm": "https://www.npmjs.com/package/awesome-test-list"},
                }
            },
            {
                "package": {
                    "name": "drill4j",
                    "description": "Node wrapper for Drill4J",
                    "keywords": ["java"],
                    "date": "2025-06-01T00:00:00.000Z",
                    "links": {"npm": "https://www.npmjs.com/package/drill4j"},
                }
            },
        ],
    }


def gh_client_for_phrase(fragment: str = "test impact") -> tuple[GhClient, FakeGh]:
    runner = FakeGh({fragment: fixture_text(GH_FIXTURE)})
    return GhClient(runner=runner, search_per_minute=60000, throttle=Throttle(per_minute={"api.github.com": 60000})), runner


def http_for(host: str, url: str, payload: dict | None) -> tuple[HttpClient, FakeTransport]:
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    return make_http(host, {url: (200, body)})


def sample_rows() -> list[dict]:
    return [
        {"source": "github", "id": "a/top", "url": "u1", "desc": "d1", "stars": 100,
         "lang": "Python", "pushed": days_ago(10), "topics": ["t1"], "query": "q"},
        {"source": "github", "id": "b/mid", "url": "u2", "desc": "d2", "stars": 50,
         "lang": "Go", "pushed": days_ago(200), "topics": ["t1", "t2"], "query": "q"},
        {"source": "github", "id": "c/old", "url": "u3", "desc": "d3", "stars": 10,
         "lang": "Go", "pushed": days_ago(500), "topics": [], "query": "q"},
        {"source": "npm", "id": "n1", "url": "u4", "desc": "d4", "stars": None,
         "lang": "js", "pushed": "", "topics": [], "query": "q"},
        {"source": "npm", "id": "n2", "url": "u5", "desc": "d5", "stars": None,
         "lang": "js", "pushed": "не дата", "topics": [], "query": "q"},
    ]


# --- short_name ----------------------------------------------------------------------------


def test_short_name_normalizes_names_like_legacy() -> None:
    assert short_name("project-py") == "project"
    assert short_name("@scope/project") == "project"
    assert short_name("ruff") == "ruff"
    assert short_name("") == ""
    assert short_name(None) == ""


def test_short_name_drops_cyrillic_like_legacy() -> None:
    """Осознанное отклонение от буквального текста задания: legacy-регексп ``[^a-z0-9]``
    выбрасывает кириллицу целиком, поэтому «owner/Проект» даёт пустую строку (и такая
    строка отсеивается в dedupe). Поведение оставлено legacy-совместимым — иначе
    паритетный тест с legacy разойдётся; проверено прогоном обоих кодов."""
    assert short_name("owner/Проект") == ""


def test_short_name_strips_several_suffixes_in_a_row() -> None:
    """Хвосты отсекаются в порядке списка хвостов, а не «до упора».

    В legacy цикл идёт по кортежу ``-py, _py, .py, -js, -ts, -mcp, -server, -cli, -core``:
    встретив ``-cli`` последним, он уже прошёл ``-server`` и второй раз к нему не вернётся,
    поэтому «project-server-cli» -> «projectserver». Поведение сохранено как есть (паритет),
    хотя выглядит как недоделка — вынесено в вопросы ревью.
    """
    assert short_name("owner/project-server-cli") == "projectserver"
    assert short_name("owner/project-server") == "project"
    assert short_name("rUFF.py") == "ruff"


# --- dedupe --------------------------------------------------------------------------------


def ruff_rows() -> list[dict]:
    return [
        {"source": "github", "id": "astral-sh/ruff", "url": "https://github.com/astral-sh/ruff",
         "desc": "An extremely fast Python linter", "stars": 39000, "lang": "Rust",
         "pushed": "2026-10-18", "topics": ["linter"], "query": PHRASE},
        {"source": "npm", "id": "ruff", "url": "https://www.npmjs.com/package/ruff",
         "desc": "Ruff npm wrapper", "stars": None, "lang": "js",
         "pushed": "2025-01-02", "topics": [], "query": "flaky tests"},
        {"source": "registry", "id": "io.github.x/ruff", "url": "", "desc": "",
         "stars": None, "lang": None, "pushed": "", "topics": [], "query": "test selection"},
    ]


def test_dedupe_merges_one_project_from_three_sources() -> None:
    merged = dedupe(ruff_rows())

    assert len(merged) == 1
    row = merged[0]
    assert row["sources"] == ["github", "npm", "registry"]
    assert row["queries"] == ["flaky tests", PHRASE, "test selection"]
    assert row["stars"] == 39000
    assert row["id"] == "astral-sh/ruff"
    assert row["lang"] == "Rust"
    assert row["pushed"] == "2026-10-18"  # максимум по датам
    assert row["desc"] == "An extremely fast Python linter"  # самый длинный


def test_dedupe_keeps_distinct_projects_apart() -> None:
    rows = [
        {"source": "github", "id": "a/one", "url": "u", "desc": "d", "stars": 1,
         "lang": None, "pushed": "", "topics": [], "query": "q"},
        {"source": "github", "id": "b/two", "url": "u", "desc": "d", "stars": 1,
         "lang": None, "pushed": "", "topics": [], "query": "q"},
    ]

    assert len(dedupe(rows)) == 2


def test_dedupe_takes_id_url_lang_from_the_starrier_row() -> None:
    rows = [
        {"source": "github", "id": "old/name", "url": "u-old", "desc": "d", "stars": 5,
         "lang": "Python", "pushed": "2024-01-01", "topics": [], "query": "q"},
        {"source": "github", "id": "new/name", "url": "u-new", "desc": "dd", "stars": 99,
         "lang": "Rust", "pushed": "2025-01-01", "topics": [], "query": "q2"},
    ]

    row = dedupe(rows)[0]

    assert (row["id"], row["url"], row["lang"], row["stars"]) == ("new/name", "u-new", "Rust", 99)


def test_dedupe_drops_rows_without_usable_name() -> None:
    rows = [
        {"source": "npm", "id": None, "url": "", "desc": "", "stars": None, "lang": "js",
         "pushed": "", "topics": [], "query": "q"},
        {"source": "registry", "id": "///", "url": "", "desc": "", "stars": None,
         "lang": None, "pushed": "", "topics": [], "query": "q"},
    ]

    assert dedupe(rows) == []


# --- is_noise / relevant / words_of --------------------------------------------------------


def test_is_noise_cuts_collections_and_courses() -> None:
    assert is_noise({"id": "awesome-mcp", "desc": ""}) is True
    assert is_noise({"id": "ruff", "desc": "An extremely fast Python linter"}) is False
    assert is_noise({"id": "x", "desc": "A collection of tutorials"}) is True


def test_relevant_needs_a_meaningful_word_of_the_phrase() -> None:
    assert relevant({"id": "impact-analyzer", "desc": ""}, WORDS) is True
    assert relevant({"id": "random", "desc": "unrelated"}, WORDS) is False
    assert relevant({"id": "a", "desc": "Test selection for CI"}, WORDS) is True
    # Пустой список слов — «релевантно всё» (как в legacy).
    assert relevant({"id": "random", "desc": ""}, []) is True


def test_words_of_keeps_only_long_words() -> None:
    assert words_of(PHRASE) == WORDS
    assert words_of("ai in ci") == []


# --- gh_search -----------------------------------------------------------------------------


def test_gh_search_returns_catalog_rows_from_fixture() -> None:
    client, runner = gh_client_for_phrase()

    rows = gh_search(client, PHRASE, pages=2, min_stars=20)

    assert len(rows) == 10
    assert all(row["source"] == "github" and row["query"] == PHRASE for row in rows)
    assert all(isinstance(row["topics"], list) and len(row["pushed"]) == 10 for row in rows)
    assert rows[0]["id"] == "ai-boost/awesome-prompts"
    assert rows[0]["stars"] == 9094
    assert runner.queries[0] == f'"{PHRASE}" in:description,readme stars:>=20'
    # Страница неполная (10 из 100) — обход остановился на первой, как в legacy.
    assert len(runner.calls) == 1


def test_gh_search_without_min_stars_has_no_stars_filter() -> None:
    client, runner = gh_client_for_phrase()

    gh_search(client, PHRASE)

    assert runner.queries[0] == f'"{PHRASE}" in:description,readme'


def test_gh_search_paginates_until_short_page() -> None:
    full = {"total_count": 103, "items": [
        {"full_name": f"o/r{i}", "html_url": "u", "description": "d test impact",
         "stargazers_count": 1, "language": "Python", "pushed_at": "2026-01-01T00:00:00Z",
         "topics": []}
        for i in range(GH_PER_PAGE)
    ]}
    tail = {"total_count": 103, "items": [
        {"full_name": f"o/t{i}", "html_url": "u", "description": "d test impact",
         "stargazers_count": 1, "language": "Python", "pushed_at": "2026-01-01T00:00:00Z",
         "topics": []}
        for i in range(3)
    ]}
    pages_seen: list[tuple[int, int]] = []
    runner = FakeGh({"x": "unused"})
    runner.mapping = {}
    responses = [json.dumps(full), json.dumps(tail)]

    def fake_run(argv: list[str]) -> tuple[int, str, str]:
        runner.calls.append(argv)
        return 0, responses.pop(0), ""

    client = GhClient(
        runner=fake_run, search_per_minute=60000, throttle=Throttle(per_minute={"api.github.com": 60000})
    )

    rows = gh_search(client, PHRASE, pages=5, on_page=lambda page, total: pages_seen.append((page, total)))

    assert len(rows) == GH_PER_PAGE + 3
    assert pages_seen == [(1, GH_PER_PAGE), (2, GH_PER_PAGE + 3)]
    assert len(runner.calls) == 2  # третья страница не запрашивалась: страница неполная


def test_gh_search_breaks_when_gh_fails() -> None:
    client = GhClient(
        runner=lambda argv: (1, "", "rate limit"),
        search_per_minute=60000,
        throttle=Throttle(per_minute={"api.github.com": 60000}),
    )

    assert gh_search(client, PHRASE) == []


# --- report --------------------------------------------------------------------------------


def test_report_counts_sources_languages_and_freshness(capsys) -> None:
    stats = report(sample_rows())

    assert stats["total"] == 5
    assert stats["by_source"] == {"github": 3, "npm": 2}
    assert stats["languages"] == {"Go": 2, "Python": 1, "js": 2}
    assert stats["fresh_90d"] == 1
    assert stats["year_365d"] == 2
    assert stats["abandoned_365d"] == 3  # 500 дней, пустая дата и мусор
    assert [row["id"] for row in stats["top_by_stars"]] == ["a/top", "b/mid", "c/old"]
    assert stats["top_by_stars"][0]["stars"] == 100
    assert stats["topics"] == {"t1": 2, "t2": 1}
    # Ничего не печатаем: печать — забота CLI (шаг 1f).
    assert capsys.readouterr().out == ""


def test_report_counts_push_today_as_fresh() -> None:
    """Отклонение от legacy: там ``(age or 9999)`` считал push сегодняшнего дня
    отсутствием данных — проект сразу после релиза попадал в заброшенные."""
    stats = report([{"source": "github", "id": "a/b", "desc": "d", "stars": 1,
                     "lang": "Python", "pushed": days_ago(0), "topics": [], "query": "q"}])

    assert stats["fresh_90d"] == 1
    assert stats["abandoned_365d"] == 0


def test_report_limits_top_rows() -> None:
    assert len(report(sample_rows(), top=2)["top_by_stars"]) == 2


def test_report_on_empty_catalog() -> None:
    assert report([]) == {
        "total": 0,
        "by_source": {},
        "languages": {},
        "fresh_90d": 0,
        "year_365d": 0,
        "abandoned_365d": 0,
        "top_by_stars": [],
        "topics": {},
    }


# --- to_markdown ---------------------------------------------------------------------------


def test_to_markdown_sorts_by_stars_and_escapes_pipes() -> None:
    rows = [dict(row) for row in sample_rows()[:3]]
    rows[0]["desc"] = "a | b"
    rows[0]["sources"] = ["github", "npm"]

    text = to_markdown(rows, "тест")
    lines = text.splitlines()

    assert lines[0] == "# Каталог: тест"
    assert lines[2] == "Всего проектов: 3"
    assert lines[4] == "| Звёзды | Проект | Язык | Push | Источники | Описание |"
    assert lines[5] == "|---:|---|---|---|---|---|"
    assert len(lines) == 6 + 3
    # Порядок — по звёздам вниз; «|» внутри описания заменён на «/».
    assert [line.split(" | ")[1] for line in lines[6:]] == ["a/top", "b/mid", "c/old"]
    assert "a / b" in lines[6]
    assert "github,npm" in lines[6]
    assert text.endswith("\n")


def test_to_markdown_puts_dashes_for_unknown_values() -> None:
    rows = [{"source": "registry", "id": "x/y", "url": "", "desc": "", "stars": None,
             "lang": None, "pushed": "", "topics": [], "query": "q", "sources": ["registry"]}]

    line = to_markdown(rows, "t").splitlines()[6]

    assert line == "| - | x/y | - | - | registry |  |"


# --- registry_search -----------------------------------------------------------------------


def test_registry_search_adds_query_to_rows() -> None:
    url = MCP_SEARCH_URL.format(q="impact", limit=100)
    http, transport = http_for(MCP_HOST, url, fixture_json(MCP_IMPACT_FIXTURE))

    rows = registry_search(http, "impact")

    assert len(rows) == 10
    assert all(row["source"] == "registry" and row["query"] == "impact" for row in rows)
    assert transport.calls == [url]


def test_registry_search_empty_on_registry_error() -> None:
    http, _ = make_http(MCP_HOST, {})

    assert registry_search(http, "impact") == []


# --- build_catalog -------------------------------------------------------------------------


def build_for(phrase: str = PHRASE, **kwargs) -> tuple[dict, list[tuple[float, str]]]:
    """Прогон build_catalog на фикстурах; возвращает (результат, вызовы прогресса)."""
    progress: list[tuple[float, str]] = []
    gh_client, _runner = gh_client_for_phrase()
    npm_url = npm_search_url(phrase, NPM_SIZE)
    npm_http, _ = http_for("registry.npmjs.org", npm_url, npm_payload())
    mcp_url = MCP_SEARCH_URL.format(q=phrase.replace(" ", "%20"), limit=100)
    mcp_http, _ = http_for(MCP_HOST, mcp_url, fixture_json(MCP_EMPTY_FIXTURE))

    result = build_catalog(
        [phrase],
        gh_client=gh_client,
        registry_client=RegistryClient(npm_http),
        mcp_registry_http=mcp_http,
        on_progress=lambda value, message: progress.append((value, message)),
        **kwargs,
    )
    return result, progress


def test_build_catalog_merges_dedupes_and_filters_noise() -> None:
    result, progress = build_for()

    assert result["queries"] == [PHRASE]
    items = result["items"]
    ids = [row["id"] for row in items]

    # github-находка склеилась с npm-пакетом с тем же short_name (drill4j) — это и есть dedupe между источниками.
    assert "Drill4J/drill4j" in ids
    merged = next(row for row in items if row["id"] == "Drill4J/drill4j")
    assert merged["sources"] == ["github", "npm"]
    assert merged["queries"] == [PHRASE]
    # npm-пакеты прошли фильтр: релевантный остался, awesome-список отсеян как шум.
    assert "test-impact-analyzer" in ids
    assert "awesome-test-list" not in ids
    # Из github-фикстуры остались только релевантные и не шумные находки.
    assert "ai-boost/awesome-prompts" not in ids
    assert not any(is_noise(row) for row in items)
    assert all(relevant(row, WORDS) for row in items)
    # Склейка отработала: одинаковых short_name в каталоге нет.
    keys = [short_name(row["id"]) for row in items]
    assert len(keys) == len(set(keys))
    # Статистика считается по отфильтрованному каталогу.
    assert result["stats"]["total"] == len(items)
    assert result["stats"]["by_source"]["github"] == len(
        [row for row in items if "github" in row["sources"]]
    )


def test_build_catalog_reports_progress_for_every_source() -> None:
    _result, progress = build_for()

    assert len(progress) == 3  # github, npm, registry — по разу на фразу
    assert [message.split("→ ")[1].split(":")[0] for _value, message in progress] == [
        "github",
        "npm",
        "registry",
    ]
    assert progress[-1][0] == 1.0
    assert "«test impact analysis»" in progress[0][1]
    assert progress[0][1].startswith("фраза 1/1:")


def test_build_catalog_accepts_merge_rows() -> None:
    extra = {"source": "github", "id": "old/keep", "url": "u", "desc": "kept test impact",
             "stars": 7, "lang": "Python", "pushed": "2024-02-02", "topics": [], "query": "старая фраза",
             "sources": ["github"], "queries": ["старая фраза"]}

    result, _progress = build_for(merge_rows=[dict(extra)])

    kept = [row for row in result["items"] if row["id"] == "old/keep"]
    assert len(kept) == 1
    assert kept[0]["stars"] == 7


def test_build_catalog_with_empty_phrases_returns_empty_result() -> None:
    result = build_catalog(
        [],
        gh_client=GhClient(runner=lambda argv: (0, "{}", ""), throttle=Throttle(per_minute={})),
        registry_client=RegistryClient(make_http("registry.npmjs.org", {})[0]),
        mcp_registry_http=make_http(MCP_HOST, {})[0],
        merge_rows=[],
    )

    assert result["queries"] == []
    assert result["items"] == []
    assert result["stats"]["total"] == 0


def test_build_catalog_works_without_progress_callback() -> None:
    gh_client, _runner = gh_client_for_phrase()
    npm_http, _ = http_for("registry.npmjs.org", npm_search_url(PHRASE, NPM_SIZE), npm_payload())
    mcp_http, _ = make_http(MCP_HOST, {})

    result = build_catalog(
        [PHRASE],
        gh_client=gh_client,
        registry_client=RegistryClient(npm_http),
        mcp_registry_http=mcp_http,
    )

    assert result["stats"]["total"] > 0


def test_build_catalog_registry_only_source() -> None:
    """Источник реестра проверяется на фразе, по которой он что-то находит."""
    url = MCP_SEARCH_URL.format(q="impact", limit=100)
    mcp_http, transport = http_for(MCP_HOST, url, fixture_json(MCP_IMPACT_FIXTURE))

    result = build_catalog(
        ["impact"],
        gh_client=GhClient(runner=lambda argv: (0, "{}", ""), throttle=Throttle(per_minute={})),
        registry_client=RegistryClient(make_http("registry.npmjs.org", {})[0]),
        mcp_registry_http=mcp_http,
        sources=("registry",),
    )

    assert transport.calls == [url]
    assert result["items"], "реестр должен был отдать хотя бы одну релевантную строку"
    assert all(row["source"] == "registry" for row in result["items"])
    assert all(relevant(row, ["impact"]) for row in result["items"])
    assert set(result["stats"]["by_source"]) == {"registry"}
    # В фикстуре есть повторы серверов (одно имя с разными версиями), поэтому после dedupe
    # строк меньше, а by_source считает все вошедшие источники — в legacy так же.
    assert len(result["items"]) < 10 <= result["stats"]["by_source"]["registry"]


def test_build_catalog_dedupes_phrases_but_keeps_order() -> None:
    gh_client, runner = gh_client_for_phrase()
    npm_http, _ = make_http("registry.npmjs.org", {})
    mcp_http, _ = make_http(MCP_HOST, {})

    result = build_catalog(
        [PHRASE, "flaky tests", PHRASE],
        gh_client=gh_client,
        registry_client=RegistryClient(npm_http),
        mcp_registry_http=mcp_http,
        sources=("github",),
    )

    assert result["queries"] == [PHRASE, "flaky tests"]
    assert len(runner.queries) == 2  # повтор фразы не удвоил поиск
    assert runner.queries[0].startswith(f'"{PHRASE}"')
    assert runner.queries[1].startswith('"flaky tests"')


# --- чистота импорта -----------------------------------------------------------------------


def test_catalog_and_mcp_registry_have_no_mutable_globals() -> None:
    """Никаких глобалов-накопителей: кэш и состояние живут в клиентах."""
    import core.catalog as catalog
    import core.mcp_registry as mcp_registry

    for module in (catalog, mcp_registry):
        mutable = [
            name
            for name in vars(module)
            if not name.startswith("__")
            and isinstance(vars(module)[name], (dict, list, set, bytearray))
        ]
        assert mutable == [], f"{module.__name__}: изменяемые глобалы {mutable}"


def test_import_has_no_network_and_no_file_writes() -> None:
    """Импорт core.catalog и core.mcp_registry не ходит в сеть и не пишет файлов."""
    script = """
import builtins, socket, urllib.request

def boom(*args, **kwargs):
    raise AssertionError("побочный эффект на импорте")

urllib.request.urlopen = boom
socket.socket.connect = boom
real_open = builtins.open

def guarded(file, mode="r", *args, **kwargs):
    if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
        raise AssertionError("запись файла на импорте")
    return real_open(file, mode, *args, **kwargs)

builtins.open = guarded

import core.catalog, core.mcp_registry

print("ok", core.catalog.build_catalog.__name__, core.mcp_registry.list_servers.__name__)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ok build_catalog list_servers")


@pytest.mark.parametrize("module", ["core.catalog", "core.mcp_registry"])
def test_modules_do_not_print_on_import(module: str) -> None:
    script = f"""
import importlib, io, contextlib
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    importlib.import_module("{module}")
print(repr(buffer.getvalue()))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(ROOT), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "''"
