"""Тесты cli/verify.py — фейковый GhClient, сеть ноль (шаг 1f).

Пауза ``time.sleep(2.5)`` legacy удалена (троттлинг держит GhClient), поэтому
тесты мгновенны: фейковый клиент вообще не спит.
"""

from __future__ import annotations

import datetime
import json
import pathlib

import pytest

from cli import verify as cli_verify

GAP_TOOLS = "raw/gap_tools.json"
RESULT_JSON = "raw/verify.json"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def item(
    full_name: str,
    stars: int,
    *,
    days_ago: int = 10,
    description: str = "MCP server for tests",
) -> dict:
    """Репозиторий в форме Search API; ``days_ago`` — свежесть push."""
    pushed = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days_ago)
    return {
        "full_name": full_name,
        "description": description,
        "stargazers_count": stars,
        "pushed_at": pushed.isoformat(),
        "html_url": f"https://github.com/{full_name}",
        "language": "Python",
        "archived": False,
    }


def install(
    monkeypatch: pytest.MonkeyPatch,
    items: list[dict],
    org_repos: list[dict] | tuple = (),
) -> dict:
    """Фейк GhClient: поиск из ``items``, репозитории организации — из ``org_repos``."""
    calls: dict[str, list] = {"search": [], "org": []}

    def runner(argv: list[str]) -> tuple[int, str, str]:
        calls["org"].append(argv)
        endpoint = argv[3] if len(argv) > 3 else ""
        if endpoint.startswith("orgs/") or endpoint.startswith("users/"):
            return 0, json.dumps(list(org_repos)), ""
        return 1, "", "неожиданный вызов gh"

    class FakeGh:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.runner = runner

        def search_repositories(self, q: str, **kwargs: object) -> tuple[int, list[dict]]:
            calls["search"].append(q)
            return (len(items) if items else 0), [dict(repo) for repo in items]

    monkeypatch.setattr(cli_verify, "GhClient", FakeGh)
    monkeypatch.setattr(cli_verify, "setup_gh_or_warn", lambda: (True, ""))
    return calls


def test_live_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, [item("grafana/mcp-grafana", 3500)])

    code = cli_verify.main(["grafana/grafana"])
    out = capsys.readouterr().out

    assert code == 0
    assert "ЖИВОЙ" in out
    assert "grafana/mcp-grafana" in out

    result = json.loads(pathlib.Path(RESULT_JSON).read_text(encoding="utf-8"))
    assert result["grafana/grafana"]["verdict"] == "ЖИВОЙ"
    assert result["grafana/grafana"]["best"]["full_name"] == "grafana/mcp-grafana"


def test_stub_verdict_on_few_stars(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, [item("grafana/mcp-grafana", 5)])

    code = cli_verify.main(["grafana/grafana"])
    out = capsys.readouterr().out

    assert code == 0
    assert "ЗАГЛУШКА" in out
    result = json.loads(pathlib.Path(RESULT_JSON).read_text(encoding="utf-8"))
    assert result["grafana/grafana"]["verdict"] == "ЗАГЛУШКА"


def test_absent_verdict_on_empty_search(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, [])

    code = cli_verify.main(["nowhere/nada"])
    out = capsys.readouterr().out

    assert code == 0
    assert "НЕТ" in out
    assert "релевантных mcp нет" in out
    result = json.loads(pathlib.Path(RESULT_JSON).read_text(encoding="utf-8"))
    assert result["nowhere/nada"]["verdict"] == "НЕТ"
    assert result["nowhere/nada"]["best"] is None


def test_org_candidates_marked_and_top_reads_gap_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    org = [item("grafana/mcp-alt", 400)]
    calls = install(monkeypatch, [item("grafana/mcp-grafana", 3500)], org_repos=org)

    (tmp_path / "raw").mkdir()
    (tmp_path / GAP_TOOLS).write_text(
        json.dumps([
            {"full_name": "grafana/grafana", "stars": 60000, "desc": "monitoring"},
            {"full_name": "b/two", "stars": 20, "desc": ""},
            {"full_name": "c/three", "stars": 10, "desc": ""},
        ]),
        encoding="utf-8",
    )

    code = cli_verify.main([])
    out = capsys.readouterr().out

    # --top по умолчанию 25: все три цели из gap_tools.json, имена в запросах.
    assert code == 0
    assert calls["search"] == ["grafana mcp", "two mcp", "three mcp"]
    assert "«org»" in out  # находка организации помечена, как в legacy

    result = json.loads(pathlib.Path(RESULT_JSON).read_text(encoding="utf-8"))
    assert set(result) == {"grafana/grafana", "b/two", "c/three"}
    assert result["grafana/grafana"]["stars"] == 60000  # звёзды цели — из gap_tools.json
