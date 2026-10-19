"""Whitelist-маппинг kind -> argv в ``app/jobs.py`` (шаг 3d-a).

Проверяется главное свойство шага: ``kind`` из запроса — только ключ таблицы
:data:`app.jobs._KIND_TO_MODULE`, в имя модуля он не подставляется. Второе —
``params`` превращаются во флаги по белому списку, всё лишнее — ``ValueError``
**до** ``subprocess.Popen``.

Реальных подпроцессов нет: ``subprocess.Popen`` подменён записью argv (AGENTS
gotcha 23 — быстрый цикл без единого ``Popen``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from app import jobs as jobs_module
from app.jobs import (
    KIND_PARAM_KEYS,
    KIND_PARAM_TYPES,
    KIND_REQUIRED_KEYS,
    _KIND_TO_MODULE,
    Job,
    _default_runner,
    _params_to_argv,
    missing_required,
)

JOB_ID = "a" * 32


def make_job(kind: str = "packages.check", params: dict | None = None) -> Job:
    """Задача в очереди — такие же поля, как у ``Store.get_job()``."""
    return Job(id=JOB_ID, kind=kind, params=dict(params or {}), status="queued")


class PopenRecorder:
    """Подмена ``subprocess.Popen``: запоминает argv, процесса не создаёт."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> object:
        self.calls.append((list(argv), dict(kwargs)))
        return object()


@pytest.fixture()
def popen(monkeypatch: pytest.MonkeyPatch) -> PopenRecorder:
    recorder = PopenRecorder()
    monkeypatch.setattr(jobs_module.subprocess, "Popen", recorder)
    return recorder


# --- kind -> модуль -----------------------------------------------------------


def test_kind_to_module_is_whitelist_and_covers_job_kinds() -> None:
    """Таблица модулей покрывает ровно те kind, что описаны таблицами params."""
    assert set(_KIND_TO_MODULE) == set(KIND_PARAM_KEYS)
    assert set(KIND_PARAM_TYPES) == set(KIND_PARAM_KEYS)
    assert set(KIND_REQUIRED_KEYS) == set(KIND_PARAM_KEYS)
    for kind, required in KIND_REQUIRED_KEYS.items():
        assert required <= KIND_PARAM_KEYS[kind], kind
    assert all(module.startswith("cli.") for module in _KIND_TO_MODULE.values())


def test_missing_required_treats_empty_values_as_absent() -> None:
    """Пустые names/diff_from — не «передано», а «не передано» (одна правда с API)."""
    assert missing_required("packages.check", {}) == ["names"]
    assert missing_required("packages.check", {"names": []}) == ["names"]
    assert missing_required("packages.check", {"names": ["x"]}) == []
    assert missing_required("monitor.diff", {"diff_from": "  "}) == ["diff_from"]
    assert missing_required("registry.scan", {}) == []


# --- params -> argv -----------------------------------------------------------


def test_packages_check_names_and_keywords() -> None:
    argv = _params_to_argv(
        "packages.check", {"names": ["requests", "httpx"], "keyword": ["mcp", "otel"]}
    )
    assert argv == [
        "requests",
        "httpx",
        "--keyword=mcp",
        "--keyword=otel",
    ]


def test_packages_check_without_keyword_passes_names_only() -> None:
    """Пустой keyword не передаём: CLI возьмёт ``Settings.keywords``."""
    assert _params_to_argv("packages.check", {"names": ["requests"]}) == ["requests"]
    assert _params_to_argv(
        "packages.check", {"names": ["requests"], "keyword": []}
    ) == ["requests"]


def test_catalog_build_flags_and_default_sources() -> None:
    argv = _params_to_argv(
        "catalog.build", {"phrases": ["mcp server"], "min_stars": 50, "pages": 2}
    )
    assert argv == ["mcp server", "--min-stars=50", "--pages=2"]


def test_catalog_build_excludes_sources() -> None:
    argv = _params_to_argv(
        "catalog.build", {"phrases": ["mcp"], "sources": ["github"]}
    )
    assert argv == ["mcp", "--no-npm", "--no-registry"]


def test_registry_scan_flags() -> None:
    argv = _params_to_argv(
        "registry.scan",
        {"base_url": "https://example.test/servers?limit={limit}", "limit": 10, "max_pages": 3},
    )
    assert argv == [
        "--base-url=https://example.test/servers?limit={limit}",
        "--limit=10",
        "--max-pages=3",
    ]
    assert _params_to_argv("registry.scan", {}) == []


def test_github_verify_targets_and_keyword() -> None:
    argv = _params_to_argv("github.verify", {"targets": ["a/b", "c/d"], "keyword": "mcp"})
    assert argv == ["a/b", "c/d", "--keyword=mcp"]


def test_monitor_diff_uses_diff_flag() -> None:
    assert _params_to_argv("monitor.diff", {"diff_from": "raw/prev.json"}) == [
        "--diff=raw/prev.json"
    ]


def test_monitor_diff_without_diff_from_raises() -> None:
    with pytest.raises(ValueError, match="diff_from"):
        _params_to_argv("monitor.diff", {})


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="kind"):
        _params_to_argv("evil; rm -rf /", {})


def test_unknown_param_raises() -> None:
    with pytest.raises(ValueError, match="unknown param for packages.check: unknown"):
        _params_to_argv("packages.check", {"names": ["x"], "unknown": 1})


def test_missing_required_param_raises() -> None:
    with pytest.raises(ValueError, match="names"):
        _params_to_argv("packages.check", {"keyword": ["mcp"]})


# --- _default_runner ----------------------------------------------------------


def test_default_runner_builds_python_m_cli_with_job_json(
    popen: PopenRecorder, tmp_path: Path
) -> None:
    job = make_job("packages.check", {"names": ["requests"]})

    _default_runner(job, tmp_path)

    assert len(popen.calls) == 1
    argv, kwargs = popen.calls[0]
    assert argv[:3] == [jobs_module.sys.executable, "-m", "cli.check_predecessors"]
    assert "--job-json" in argv
    assert f"--json-out={tmp_path / 'jobs' / f'{JOB_ID}.result.json'}" in argv
    assert argv[-1] == "requests"
    assert "cli.packages.check" not in argv
    assert kwargs["cwd"] == str(jobs_module._PROJECT_ROOT)
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["text"] is True


def test_default_runner_rejects_injected_kind_without_popen(
    popen: PopenRecorder, tmp_path: Path
) -> None:
    """``kind`` не может подставить команду: ValueError и ни одного Popen."""
    for bad_kind in ("evil; rm -rf /", "cli.check_predecessors", "packages.check; id"):
        with pytest.raises(ValueError):
            _default_runner(make_job(bad_kind, {}), tmp_path)
    assert popen.calls == []


def test_default_runner_monitor_diff_uses_known_module(
    popen: PopenRecorder, tmp_path: Path
) -> None:
    """Тот самый баг legacy-скелета: ``monitor.diff`` больше не даёт ``cli.monitor.diff``."""
    _default_runner(make_job("monitor.diff", {"diff_from": "raw/prev.json"}), tmp_path)
    argv, _kwargs = popen.calls[0]
    assert argv[2] == "cli.check_predecessors"
    assert "--diff=raw/prev.json" in argv
