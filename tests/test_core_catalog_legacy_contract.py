"""Гейт шага 1d: ``core.catalog`` один-в-один с legacy ``catalog_build``.

Из legacy-модуля вызываются РОДНЫЕ функции (``short_name``, ``dedupe``, ``is_noise``,
``relevant``, ``words_of``, ``to_markdown``) — сеть им не нужна, поэтому фикстуры здесь
синтетические, зато входы берутся «неудобные»: кириллица в имени, несколько хвостов,
повторяющиеся источники, мусорные даты.

Известные отклонения (по одному на функцию, каждое проверяется явно):
``report()`` — свежесть: legacy считает ``(age or 9999)`` и потому push сегодняшнего дня
принимает за «данных нет»; в ``core`` ноль — валидный возраст.
"""

from __future__ import annotations

import datetime

import legacy.catalog_build as legacy

from core.catalog import (
    dedupe,
    is_noise,
    relevant,
    report,
    short_name,
    to_markdown,
    words_of,
)

PHRASE = "test impact analysis"
WORDS = words_of(PHRASE)


def legacy_rows() -> list[dict]:
    """Строки каталога с неудобными случаями: дубли, пустые поля, мусорная дата."""
    return [
        {"source": "github", "id": "astral-sh/ruff", "url": "https://github.com/astral-sh/ruff",
         "desc": "An extremely fast Python linter", "stars": 39000, "lang": "Rust",
         "pushed": "2026-10-18", "topics": ["linter"], "query": PHRASE},
        {"source": "npm", "id": "ruff", "url": "https://www.npmjs.com/package/ruff",
         "desc": "Ruff npm wrapper and a slightly longer description", "stars": None,
         "lang": "js", "pushed": "2025-01-02", "topics": [], "query": "flaky tests"},
        {"source": "registry", "id": "io.github.x/ruff", "url": "", "desc": "",
         "stars": None, "lang": None, "pushed": "", "topics": [], "query": "test selection"},
        {"source": "github", "id": "DuckDuckGo/duckduckgo-python", "url": "u", "desc": "поиск",
         "stars": 10, "lang": "Python", "pushed": "не дата", "topics": [], "query": PHRASE},
        {"source": "npm", "id": None, "url": "", "desc": "без имени", "stars": None,
         "lang": "js", "pushed": "", "topics": [], "query": PHRASE},
        {"source": "github", "id": "owner/awesome-test-impact", "url": "u2",
         "desc": "A curated list of papers", "stars": 900, "lang": None,
         "pushed": "2024-05-05", "topics": [], "query": PHRASE},
    ]


def test_short_name_matches_legacy() -> None:
    probes = [
        "owner/Проект", "project-py", "@scope/project", "ruff", "", None,
        "owner/project-server-cli", "rUFF.py", "a/mcp-server", "x_core", "weird---name",
    ]

    for probe in probes:
        assert short_name(probe) == legacy.short_name(probe), probe


def test_dedupe_matches_legacy_on_values_and_order() -> None:
    new_rows = dedupe(legacy_rows())
    old_rows = legacy.dedupe(legacy_rows())

    assert len(new_rows) == len(old_rows)
    for new, old in zip(new_rows, old_rows):
        assert new == old, (new, old)


def test_dedupe_keeps_repeated_source_like_legacy() -> None:
    """Два репозитория одного источника с одним именем дают ``sources`` с повтором."""
    rows = [
        {"source": "github", "id": "a/one-py", "url": "u1", "desc": "d", "stars": 1,
         "lang": "Python", "pushed": "2024-01-01", "topics": [], "query": PHRASE},
        {"source": "github", "id": "a/one", "url": "u2", "desc": "dd", "stars": 2,
         "lang": "Rust", "pushed": "2024-02-02", "topics": [], "query": "q2"},
    ]

    assert dedupe(rows) == legacy.dedupe([dict(row) for row in rows])
    assert dedupe(rows)[0]["sources"] == ["github", "github"]


def test_is_noise_and_relevant_match_legacy() -> None:
    for row in legacy_rows():
        assert is_noise(row) == legacy.is_noise(row), row["id"]
        assert relevant(row, WORDS) == legacy.relevant(row, WORDS), row["id"]
        assert relevant(row, []) == legacy.relevant(row, []) is True


def test_words_of_matches_legacy() -> None:
    for phrase in [PHRASE, "flaky tests", "AI in CI", "", "vector database 200"]:
        assert words_of(phrase) == legacy.words_of(phrase), phrase


def test_to_markdown_matches_legacy() -> None:
    rows = legacy_rows()

    assert to_markdown(rows, "проба") == legacy.to_markdown([dict(row) for row in rows], "проба")


def test_report_common_fields_match_legacy(capsys) -> None:
    """total/by_source/languages сверяем с legacy (остальное legacy только печатает)."""
    rows = dedupe(legacy_rows())

    stats = report(rows)
    legacy_stats = legacy.report([dict(row) for row in rows])
    capsys.readouterr()

    assert stats["total"] == legacy_stats["total"]
    assert stats["by_source"] == legacy_stats["by_source"]
    assert stats["languages"] == legacy_stats["languages"]


def test_report_freshness_deviation_from_legacy() -> None:
    """Отклонение по свежести: push сегодняшнего дня legacy считал «нет данных»."""
    today = datetime.date.today().isoformat()
    rows = [
        {"source": "github", "id": "a/b", "url": "", "desc": "", "stars": 1, "lang": "Python",
         "pushed": today, "topics": [], "query": PHRASE, "sources": ["github"]}
    ]

    assert report(rows)["fresh_90d"] == 1
    # В legacy ``(activity_days(...) or 9999) <= 90`` на нуле даёт 9999 — «не свежий».
    assert ((legacy.activity_days(today) or 9999) <= 90) is False
