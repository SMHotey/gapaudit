"""Тесты tools/compat_check.py — гейт шага 1g: sha256 legacy + L1 + L2, сети нет.

Тяжёлые проверки (реальный прогон L1 и полного L2) помечены ``@pytest.mark.slow``:
быстрый цикл — ``uv run pytest -q -m "not slow"``, полный — ``uv run pytest -q``.
Симуляция расхождения гоняет настоящий L2 на verify с асимметричным фейком gh:
legacy видит 3500 звёзд (ЖИВОЙ), new — 5 (ЗАГЛУШКА) — гейт обязан это поймать.
"""

from __future__ import annotations

import json

import pytest

from tools import compat_check, compat_runner
from tools.compat_allowlist import ALLOWLIST


def test_check_legacy_sha_matches_table() -> None:
    assert compat_check.check_legacy_sha() == []


@pytest.mark.slow
def test_check_l1_is_green_on_head() -> None:
    ok, output = compat_check.check_l1()
    assert ok, output
    assert "passed" in output


@pytest.mark.slow
def test_check_l2_all_scripts_green_with_allowlist_warnings() -> None:
    results = compat_check.check_l2()

    by_name = {result["script"]: result for result in results}
    assert set(by_name) == set(compat_check.SCRIPTS)
    for name, result in by_name.items():
        assert result["ok"], f"{name}: {result['failed']}"
    # Ожидаемые предупреждения allowlist: fix_toml у check_predecessors, курсор у collect.
    assert by_name["check_predecessors"]["warnings"]
    assert by_name["collect"]["warnings"]


def test_l2_detects_drift_via_asymmetric_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Асимметричный фейк gh: обе стороны реально бегут, сравнение ловит дрейф метода."""
    spec = compat_check._verify_gh_spec()
    live = [{
        "full_name": "grafana/mcp-grafana",
        "stargazers_count": 3500,
        "pushed_at": "2026-10-01T00:00:00Z",
        "description": "MCP server",
    }]
    stub = [dict(live[0], stargazers_count=5)]
    spec["search"] = [
        {"match": "grafana", "side": "legacy", "items": live},
        {"match": "grafana", "side": "new", "items": stub},
    ]
    spec["orgs"] = {}
    monkeypatch.setattr(compat_check, "_verify_gh_spec", lambda: json.loads(json.dumps(spec)))

    result = compat_check.check_l2(["verify"])[0]

    assert result["ok"] is False
    fields = {diff["field"] for diff in result["failed"]}
    assert "grafana/grafana.verdict" in fields  # ЖИВОЙ против ЗАГЛУШКА
    assert "grafana/grafana.best.stars" in fields
    assert all(diff["allow"] is None for diff in result["failed"])  # в allowlist такого нет


def test_main_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Матрица кодов выхода: 0 зелёный, 3 sha, 2 L1, 1 L2 — на подменённых гейтах."""
    def green() -> list[dict]:
        return [
            {"script": name, "ok": True, "warnings": [], "diffs": [], "failed": []}
            for name in compat_check.SCRIPTS
        ]

    monkeypatch.setattr(compat_check, "check_legacy_sha", lambda: [])
    monkeypatch.setattr(compat_check, "check_l1", lambda: (True, "359 passed"))
    monkeypatch.setattr(compat_check, "check_l2", lambda scripts=None, verbose=False: green())
    assert compat_check.main([]) == 0
    assert compat_check.main(["--skip-l1", "--skip-l2"]) == 0

    monkeypatch.setattr(
        compat_check, "check_legacy_sha", lambda: ["collect: ожидалось 170fa815fff7, получено deadbeef0000"]
    )
    assert compat_check.main([]) == 3  # sha — самый фундаментальный гейт

    monkeypatch.setattr(compat_check, "check_legacy_sha", lambda: [])
    monkeypatch.setattr(compat_check, "check_l1", lambda: (False, "FAILED"))
    assert compat_check.main([]) == 2

    monkeypatch.setattr(compat_check, "check_l1", lambda: (True, "passed"))
    broken = green()
    hard = {"field": "requests.verdict", "legacy": "ЖИВО", "new": "МЁРТВО", "allow": None}
    broken[0] = {"script": "check_predecessors", "ok": False, "warnings": [], "diffs": [hard], "failed": [hard]}
    monkeypatch.setattr(compat_check, "check_l2", lambda scripts=None, verbose=False: broken)
    assert compat_check.main([]) == 1


def test_main_skip_gates_runs_real_sha_and_writes_json(tmp_path) -> None:
    out = tmp_path / "summary.json"

    code = compat_check.main(["--skip-l1", "--skip-l2", "--json-out", str(out)])

    assert code == 0
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["sha_problems"] == []
    assert summary["l1"]["skipped"] is True
    assert summary["l2"] == []


def test_main_rejects_unknown_script(capsys: pytest.CaptureFixture[str]) -> None:
    assert compat_check.main(["--scripts", "nope"]) == 2
    assert "неизвестные скрипты" in capsys.readouterr().err


def test_allow_keys_used_by_l2_exist_in_allowlist() -> None:
    """Каждый allow-ключ, которым пользуются сценарии, обязан быть в реестре."""
    allowed = {key for script_entries in ALLOWLIST.values() for key in script_entries}
    assert "--from fix_toml=True" in allowed
    assert "default cursor" in allowed


def test_map_transport_first_matching_rule_wins() -> None:
    transport = compat_runner.MapTransport([
        {"match": "cursor=", "body": {"page": 2}},
        {"match": "api.test", "body": {"page": 1}},
    ])

    assert json.loads(transport("https://api.test/x?limit=1", {}, 0)[1]) == {"page": 1}
    assert json.loads(transport("https://api.test/x?limit=1&cursor=t", {}, 0)[1]) == {"page": 2}
    assert transport("https://other/", {}, 0)[0] == 404


def test_cmp_rows_by_name_flags_field_drift_and_name_sets() -> None:
    """Юнит сравнения L2: дрейф поля — diff без allow, лишние имена — с allow."""
    legacy = [{"name": "a", "verdict": "ЖИВО", "src": "pypi"}, {"name": "b", "verdict": "ОТСУТСТВУЕТ"}]
    new = [{"name": "a", "verdict": "МЁРТВО", "src": "pypi"}]

    diffs = compat_check._cmp_rows_by_name(legacy, new, ("src", "verdict"), names_allow="X")

    fields = {diff["field"]: diff for diff in diffs}
    assert fields["a.verdict"] == {"field": "a.verdict", "legacy": "ЖИВО", "new": "МЁРТВО", "allow": None}
    assert fields["имена"]["legacy"] == ["b"] and fields["имена"]["allow"] == "X"
    assert "a.src" not in fields  # совпавшее поле в diff не попадает


def test_check_l2_selected_script_returns_single_result() -> None:
    results = compat_check.check_l2(["coverage"])

    assert [result["script"] for result in results] == ["coverage"]
    assert results[0]["ok"], results[0]["failed"]


def test_make_gh_dispatches_search_and_orgs_in_both_call_forms() -> None:
    runner, legacy_run, auth = compat_runner.make_gh({
        "auth_ok": True,
        "search": [{"match": "grafana", "items": [{"full_name": "g/mcp"}]}],
        "orgs": {"grafana": [{"full_name": "grafana/x"}]},
    })

    code, out, _ = runner(["api", "-X", "GET", "search/repositories", "-f", "q=grafana stars:>0"])
    assert code == 0
    assert json.loads(out)["items"][0]["full_name"] == "g/mcp"

    proc = legacy_run(["gh", "api", "-X", "GET", "orgs/grafana/repos"])
    assert proc.returncode == 0
    assert json.loads(proc.stdout)[0]["full_name"] == "grafana/x"
    assert auth == (True, "")


def test_runner_without_arguments_exits_2() -> None:
    assert compat_runner.main([]) == 2
