"""Тесты core/github.py — runner подменяется фейком, сети нет (шаг 1b).

Фикстуры tests/fixtures/github/ — реальные ответы gh, снятые один раз.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import subprocess

from core.github import (
    GITHUB_HOST,
    GITHUB_SEARCH_PER_MINUTE,
    GhClient,
    age_days,
    default_runner,
    org_candidates,
    relevant_items,
)
from core.http import Throttle

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "github"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeGh:
    """Runner из списка ответов: элемент — (code, stdout, stderr) или исключение."""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[return-value]


class FakeThrottle:
    """Счётчик обращений: сколько раз ждали и с какой скоростью."""

    def __init__(self) -> None:
        self.waits: list[str] = []
        self.rates: dict[str, int] = {}

    def set_rate(self, host: str, per_minute: int) -> None:
        self.rates[host] = per_minute

    def wait(self, host: str) -> float:
        self.waits.append(host)
        return 0.0


def make_client(*responses: object) -> tuple[GhClient, FakeGh, FakeThrottle]:
    runner = FakeGh(*responses)
    throttle = FakeThrottle()
    return GhClient(runner=runner, throttle=throttle), runner, throttle  # type: ignore[arg-type]


# --- search_repositories -------------------------------------------------------------------


def test_search_repositories_parses_real_fixture() -> None:
    client, runner, _ = make_client((0, fixture_text("search_grafana_mcp.json"), ""))

    total, items = client.search_repositories("grafana mcp", per_page=5)

    assert total == 500
    assert len(items) == 5
    assert items[0]["full_name"] == "netdata/netdata"
    assert "grafana/mcp-grafana" in [item["full_name"] for item in items]
    # argv — как в legacy: gh api -X GET search/repositories -f q=... -f per_page=...
    argv = runner.calls[0]
    assert argv[:4] == ["api", "-X", "GET", "search/repositories"]
    assert "q=grafana mcp" in argv
    assert "per_page=5" in argv
    assert "sort=stars" in argv
    assert "order=desc" in argv
    assert "page=1" in argv


def test_search_repositories_error_returns_none_and_empty() -> None:
    client, runner, _ = make_client((1, "", "rate limit exceeded"))

    assert client.search_repositories("grafana mcp") == (None, [])
    assert len(runner.calls) == 1


def test_search_repositories_broken_json_returns_none_and_empty() -> None:
    client, _, _ = make_client((0, "<html>oops</html>", ""))

    assert client.search_repositories("grafana mcp") == (None, [])


def test_search_repositories_waits_throttle_once_with_search_rate() -> None:
    client, _, throttle = make_client((0, fixture_text("search_grafana_mcp.json"), ""))

    client.search_repositories("grafana mcp", per_page=5)

    assert throttle.waits == [GITHUB_HOST]
    assert throttle.rates[GITHUB_HOST] == GITHUB_SEARCH_PER_MINUTE == 30


def test_other_endpoints_use_60_per_minute() -> None:
    client, _, throttle = make_client((0, fixture_text("org_grafana.json"), ""))

    client.org_repos("grafana")

    assert throttle.rates[GITHUB_HOST] == 60


# --- org_repos -----------------------------------------------------------------------------


def test_org_repos_uses_org_endpoint() -> None:
    client, runner, _ = make_client((0, fixture_text("org_grafana.json"), ""))

    repos = client.org_repos("grafana")

    assert [repo["full_name"] for repo in repos][:2] == ["grafana/k6", "grafana/promql-anomaly-detection"]
    assert runner.calls[0][:4] == ["api", "-X", "GET", "orgs/grafana/repos"]
    assert "per_page=100" in runner.calls[0]


def test_org_repos_falls_back_to_users_endpoint() -> None:
    users_payload = json.dumps(
        [{"full_name": "Dokploy/mcp", "description": "MCP server", "stargazers_count": 7,
          "pushed_at": "2025-01-01T00:00:00Z", "html_url": "https://github.com/Dokploy/mcp"}]
    )
    client, runner, _ = make_client((1, "", "org not found"), (0, users_payload, ""))

    repos = client.org_repos("Dokploy")

    assert [repo["full_name"] for repo in repos] == ["Dokploy/mcp"]
    assert [call[3] for call in runner.calls] == ["orgs/Dokploy/repos", "users/Dokploy/repos"]


def test_org_repos_empty_when_both_endpoints_fail() -> None:
    client, runner, _ = make_client((1, "", "404"), (1, "", "404"))

    assert client.org_repos("nobody") == []
    assert len(runner.calls) == 2


def test_org_repos_returns_slim_dicts() -> None:
    client, _, _ = make_client((0, fixture_text("org_grafana.json"), ""))

    repo = client.org_repos("grafana")[0]

    assert set(repo) == {"full_name", "description", "stargazers_count", "pushed_at", "html_url"}


# --- auth_status ---------------------------------------------------------------------------


def test_auth_status_ok() -> None:
    client, _, _ = make_client((0, "Logged in to github.com account SMHotey", ""))

    ok, text = client.auth_status()

    assert ok is True
    assert "SMHotey" in text


def test_auth_status_failed() -> None:
    client, _, _ = make_client((1, "", "not logged in"))

    ok, text = client.auth_status()

    assert ok is False
    assert "not logged in" in text


# --- default_runner ------------------------------------------------------------------------


def test_default_runner_reports_missing_gh(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", boom)

    code, stdout, stderr = default_runner(["auth", "status"])

    assert code == 127
    assert stdout == ""
    assert "gh не найден" in stderr


def test_default_runner_reports_timeout(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=45)

    monkeypatch.setattr(subprocess, "run", boom)

    code, _stdout, stderr = default_runner(["api", "search/repositories"])

    assert code == 124
    assert "не ответил" in stderr


def test_default_runner_passes_argv_to_gh(monkeypatch) -> None:
    captured: list[list[str]] = []
    options: list[dict] = []

    class Result:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(argv, **kwargs):
        captured.append(argv)
        options.append(kwargs)
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    code, stdout, stderr = default_runner(["api", "-X", "GET", "user"])

    assert (code, stdout, stderr) == (0, "{}", "")
    assert captured == [["gh", "api", "-X", "GET", "user"]]
    assert options[0]["text"] is True
    assert options[0]["encoding"] == "utf-8"
    assert options[0]["timeout"] == 45.0


# --- age_days ------------------------------------------------------------------------------


def test_age_days_counts_days_since_push() -> None:
    days = age_days("2024-01-01T00:00:00Z")

    assert isinstance(days, int)
    assert days > 0


def test_age_days_without_data_is_9999() -> None:
    assert age_days(None) == 9999
    assert age_days("") == 9999
    assert age_days("не дата") == 9999
    assert age_days("2024-13-45T99:99:99Z") == 9999


# --- relevant_items ------------------------------------------------------------------------


def items_fixture() -> list[dict]:
    return [
        # 1. релевантен: needle + keyword в описании
        {"full_name": "acme/grafana-mcp-server", "description": "MCP server for Grafana",
         "stargazers_count": 10},
        # 2. сам инструмент — исключается по exclude_full_name
        {"full_name": "grafana/grafana", "description": "The open observability platform mcp",
         "stargazers_count": 60000},
        # 3. релевантен: keyword в имени + needle в описании
        {"full_name": "someone/mcp-grafana-bridge", "description": "Bridge for grafana dashboards",
         "stargazers_count": 5},
        # 4. без keyword — мимо
        {"full_name": "acme/grafana-exporter", "description": "Prometheus exporter for grafana",
         "stargazers_count": 900},
    ]


def test_relevant_items_keeps_only_needle_and_keyword() -> None:
    found = relevant_items(items_fixture(), "grafana", "mcp", exclude_full_name="grafana/grafana")

    # Оба релевантных содержат keyword в имени, поэтому порядок — по звёздам (10 > 5);
    # исключённый «grafana/grafana» и находка без keyword не попадают.
    assert [item["full_name"] for item in found] == [
        "acme/grafana-mcp-server",
        "someone/mcp-grafana-bridge",
    ]


def test_relevant_items_keyword_in_name_goes_first() -> None:
    """При равном положении «свой сервер важнее»: keyword в имени — первым."""
    items = [
        {"full_name": "a/mcp-thing", "description": "mcp tool", "stargazers_count": 1},
        {"full_name": "b/other", "description": "mcp tool for thing", "stargazers_count": 9999},
    ]

    found = relevant_items(items, "thing", "mcp")

    assert [item["full_name"] for item in found] == ["a/mcp-thing", "b/other"]


def test_relevant_items_respects_word_boundary() -> None:
    """«uv» внутри «kubernetes» — не находка: иначе вердикт «ниша закрыта» ложный."""
    items = [
        {"full_name": "astral-sh/uv", "description": "An extremely fast Python package manager with mcp",
         "stargazers_count": 1},
        {"full_name": "kubernetes/kubernetes", "description": "mcp inside kubernetes", "stargazers_count": 2},
    ]

    found = relevant_items(items, "uv", "mcp")

    assert [item["full_name"] for item in found] == ["astral-sh/uv"]


def test_relevant_items_without_exclude_keeps_the_tool_itself() -> None:
    found = relevant_items(items_fixture(), "grafana", "mcp")

    assert "grafana/grafana" in [item["full_name"] for item in found]


def test_relevant_items_empty_input() -> None:
    assert relevant_items([], "grafana", "mcp") == []


# --- org_candidates ------------------------------------------------------------------------


def org_payload() -> str:
    return json.dumps(
        [
            {"full_name": "grafana/k6", "description": "Load testing", "stargazers_count": 25000,
             "pushed_at": "2025-06-01T00:00:00Z", "html_url": "https://github.com/grafana/k6"},
            {"full_name": "grafana/grafana", "description": "The open observability platform",
             "stargazers_count": 65000, "pushed_at": "2025-06-01T00:00:00Z",
             "html_url": "https://github.com/grafana/grafana"},
            {"full_name": "grafana/mcp-grafana", "description": "MCP server for Grafana",
             "stargazers_count": 400, "pushed_at": "2025-06-01T00:00:00Z",
             "html_url": "https://github.com/grafana/mcp-grafana"},
            {"full_name": "grafana/helm-charts", "description": "Helm charts with an mcp helper",
             "stargazers_count": 900, "pushed_at": "2025-06-01T00:00:00Z",
             "html_url": "https://github.com/grafana/helm-charts"},
            {"full_name": "grafana/loki", "description": "Log aggregation", "stargazers_count": 24000,
             "pushed_at": "2025-06-01T00:00:00Z", "html_url": "https://github.com/grafana/loki"},
        ]
    )


def test_org_candidates_keeps_keyword_matches_with_name_first() -> None:
    runner = FakeGh((0, org_payload(), ""))

    found = org_candidates("grafana", "mcp", runner)  # type: ignore[arg-type]

    assert [repo["full_name"] for repo in found] == ["grafana/mcp-grafana", "grafana/helm-charts"]


def test_org_candidates_can_exclude_the_tool_itself() -> None:
    runner = FakeGh((0, org_payload(), ""))

    found = org_candidates("grafana", "mcp", runner, exclude_full_name="grafana/helm-charts")  # type: ignore[arg-type]

    assert [repo["full_name"] for repo in found] == ["grafana/mcp-grafana"]


def test_org_candidates_empty_when_gh_fails(monkeypatch) -> None:
    """Обе точки org_repos падают; троттл подменён, иначе тест ждал бы 1 с (60/мин).

    Скорость GhClient выставляет себе сам (``DEFAULT_PER_MINUTE`` для обычных точек),
    поэтому подменяется не лимит, а часы: с нулевым временем ``Throttle.wait`` считает
    интервал, но реально не спит.
    """
    import core.github as github_module

    monkeypatch.setattr(
        github_module,
        "default_throttle",
        lambda: Throttle(clock=lambda: 0.0, sleep=lambda _seconds: None),
    )
    runner = FakeGh((1, "", "404"))

    assert org_candidates("nobody", "mcp", runner) == []  # type: ignore[arg-type]


def test_org_candidates_uses_default_runner_by_default() -> None:
    """Без аргумента runner берётся default_runner — проверяем сигнатуру, не сеть."""
    assert inspect.signature(org_candidates).parameters["runner"].default is default_runner
