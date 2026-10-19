"""Гейт «метод не изменился незаметно» (шаг 1g).

L1: прогон pytest по паритетным тестам (``tests/test_core_*_legacy_contract.py``)
    и CLI-тестам (``tests/cli/``). Это уже написанные сравнения new vs legacy на
    одних и тех же фикстурах. Быстро (~2–3 с).

L2: для каждого из шести CLI — свой сценарий на фикстурах, сети нет ни одного
    вызова:

    * legacy загружается ``importlib``'ом и запускается in-process: транспорт,
      ``subprocess.run`` (gh) и ``time.sleep`` подменяются на модуле скрипта —
      как в паритетных тестах 1a–1e;
    * new запускается subprocess'ом ``python -m tools.compat_runner <модуль>``,
      который подставляет тот же фейковый транспорт/gh и зовёт ``cli.<модуль>.main``;
    * сравниваются ключевые поля JSON (белый список по скриптам);
    * расхождение из allowlist (``tools/compat_allowlist.py``) — предупреждение,
      любое другое — FAIL с diff.

Exit code: 0 — всё зелёное; 1 — есть FAIL в L2; 2 — L1 не зелёный;
3 — ``legacy/`` изменён (sha256 != таблица «Железных правил» в AGENTS.md).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from tools.compat_allowlist import ALLOWLIST
from tools.compat_runner import (
    FakeTime,
    MapTransport,
    gh_spec_for_side,
    http_rules_for_side,
    make_gh,
)

__all__ = [
    "LEGACY_SHA256",
    "L1_FILES",
    "ROOT",
    "SCRIPTS",
    "check_l1",
    "check_l2",
    "check_legacy_sha",
    "main",
]

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

#: Префиксы sha256 из «Железных правила 1» (AGENTS.md); сверка — по блобу HEAD.
LEGACY_SHA256 = {
    "catalog_build": "e98061593acc",
    "catalog_scan": "dda476f4c55e",
    "check_predecessors": "05eadcb96d13",
    "collect": "170fa815fff7",
    "coverage": "72e1f9177d0a",
    "verify": "dc8ddb238ff6",
}

#: L1 — паритетные контракты core + CLI-тесты.
L1_FILES = (
    "tests/test_core_http_legacy_contract.py",
    "tests/test_core_packages_legacy_contract.py",
    "tests/test_core_catalog_legacy_contract.py",
    "tests/test_core_scan_legacy_contract.py",
    "tests/cli/",
)

SCRIPTS = ("check_predecessors", "verify", "catalog_build", "catalog_scan", "collect", "coverage")

#: Фейковый каталог L2: идентичен для обеих сторон, limit подставляет servers_page.
FAKE_REGISTRY_URL = "https://fake.registry.test/v0/servers?limit={limit}"


# --- L1: sha и pytest ----------------------------------------------------------


def check_legacy_sha() -> list[str]:
    """``git cat-file -p HEAD:legacy/<name>.py`` -> sha256 -> сверка с LEGACY_SHA256.

    Возвращает список расхождений («имя: ожидалось X, получено Y»); пусто — всё сходится.
    Блоб читается из HEAD, а не из рабочего дерева (AGENTS.md, gotcha 12).
    """
    problems: list[str] = []
    for name, expected in sorted(LEGACY_SHA256.items()):
        proc = subprocess.run(
            ["git", "cat-file", "-p", f"HEAD:legacy/{name}.py"],
            capture_output=True,
            cwd=ROOT,
        )
        if proc.returncode != 0:
            problems.append(f"{name}: блоб HEAD:legacy/{name}.py не прочитан: {proc.stderr.decode('utf-8', 'replace').strip()}")
            continue
        got = hashlib.sha256(proc.stdout).hexdigest()[:12]
        if got != expected:
            problems.append(f"{name}: ожидалось {expected}, получено {got}")
    return problems


def check_l1() -> tuple[bool, str]:
    """Быстрый гейт: pytest по паритетным контрактам и CLI-тестам. ``(зелёный, вывод)``."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *L1_FILES],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


# --- L2: legacy vs new на фикстурах --------------------------------------------


def check_l2(scripts: list[str] | None = None, *, verbose: bool = False) -> list[dict]:
    """Сценарии legacy vs new; список результатов ``{script, ok, warnings, diffs, failed}``.

    ``scripts`` — подмножество :data:`SCRIPTS` (по умолчанию все). Каждый сценарий
    получает свой временный каталог: legacy и new не делят файлы, но видят одни
    и те же входные данные и одинаковую подмену транспорта.
    """
    wanted = [name for name in SCRIPTS if scripts is None or name in scripts]
    scenarios = _scenarios()
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="gapaudit-compat-") as tmp:
        work = Path(tmp)
        for name in wanted:
            scenario = scenarios.get(name)
            if scenario is None:
                results.append(_crash(name, "сценарий не найден"))
                continue
            try:
                result = scenario(work / name)
            except Exception as error:  # noqa: BLE001 — гейт не должен падать, он должен сообщить
                result = _crash(name, f"{type(error).__name__}: {error}")
            if verbose:
                _print_details(result)
            results.append(result)
    return results


def _scenarios() -> dict[str, Callable[[Path], dict]]:
    """Словарь сценариев L2; тесты гейта могут подменять отдельные поставщики данных."""
    return {
        "check_predecessors": _scenario_check_predecessors,
        "verify": _scenario_verify,
        "catalog_build": _scenario_catalog_build,
        "catalog_scan": _scenario_catalog_scan,
        "collect": _scenario_collect,
        "coverage": _scenario_coverage,
    }


# --- сценарий: check_predecessors ----------------------------------------------


def _rules_check_predecessors() -> list[dict]:
    return [
        _rule("pypi.org/pypi/requests/json", _http_json("pypi_requests.json")),
        _rule("registry.npmjs.org/requests", _http_json("npm_requests.json")),
        _rule("pypistats.org/api/packages/requests/recent", _http_json("pypistats_requests.json")),
    ]


def _scenario_check_predecessors(work: Path) -> dict:
    legacy_dir, new_dir = _side_dirs(work)
    toml_path = work / "deps.toml"
    # Имена с «хвостами» TOML — legacy соберёт и их, new с fix_toml=True нет (allowlist).
    toml_path.write_text(
        '[project]\nname = "gapaudit-demo"\nrequires-python = ">=3.11"\n'
        'dependencies = ["requests>=2.31"]\n',
        encoding="utf-8",
        newline="\n",
    )
    rules = _rules_check_predecessors()
    argv = ["requests", "test-xyz-absent", "--from", str(toml_path), "--json", "raw/predecessors.json"]

    legacy = _load_legacy("check_predecessors")
    transport = MapTransport(http_rules_for_side(rules, "legacy"))
    legacy.get = _legacy_http_get(transport)
    legacy._CACHE = {}
    legacy._save_cache = lambda: None
    _run_legacy(legacy, argv, legacy_dir)

    proc = _run_new("check_predecessors", argv, new_dir, rules)
    legacy_rows = _read_json(legacy_dir / "raw" / "predecessors.json")
    new_rows = _read_json(new_dir / "raw" / "predecessors.json")
    if legacy_rows is None or new_rows is None:
        return _crash("check_predecessors", f"JSON не записан: legacy={legacy_rows is not None} new={proc.returncode}", proc)

    diffs = _cmp_rows_by_name(
        legacy_rows,
        new_rows,
        ("src", "ver", "date", "month", "week", "mcp", "verdict"),
        names_allow="--from fix_toml=True",
    )
    return _result("check_predecessors", diffs)


# --- сценарий: verify -----------------------------------------------------------


def _verify_gh_spec() -> dict:
    """gh для verify: поиск grafana/mcp и репозитории организации grafana — из фикстур."""
    search = _gh_json("search_grafana_mcp.json")
    orgs = _gh_json("org_grafana.json")
    return {
        "auth_ok": True,
        "search": [{"match": "grafana", "total": search["total_count"], "items": search["items"]}],
        "orgs": {"grafana": orgs},
    }


def _scenario_verify(work: Path) -> dict:
    legacy_dir, new_dir = _side_dirs(work)
    gh_spec = _verify_gh_spec()
    argv = ["grafana/grafana", "nowhere/nada"]

    legacy = _load_legacy("verify")
    _runner, legacy_run, _auth = make_gh(gh_spec_for_side(gh_spec, "legacy"))
    legacy.subprocess = types.SimpleNamespace(run=legacy_run)
    legacy.time = FakeTime(time)
    _run_legacy(legacy, argv, legacy_dir)

    proc = _run_new("verify", argv, new_dir, [], gh_spec)
    legacy_res = _read_json(legacy_dir / "raw" / "verify.json")
    new_res = _read_json(new_dir / "raw" / "verify.json")
    if legacy_res is None or new_res is None:
        return _crash("verify", f"JSON не записан: legacy={legacy_res is not None} new={proc.returncode}", proc)

    diffs: list[dict] = []
    if set(legacy_res) != set(new_res):
        diffs.append(_diff("targets", sorted(legacy_res), sorted(new_res)))
    for target in sorted(set(legacy_res) & set(new_res)):
        old, new = legacy_res[target], new_res[target]
        if old.get("verdict") != new.get("verdict"):
            diffs.append(_diff(f"{target}.verdict", old.get("verdict"), new.get("verdict")))
        old_best = old.get("best") if isinstance(old.get("best"), dict) else None
        new_best = new.get("best") if isinstance(new.get("best"), dict) else None
        for field in ("full_name", "stars"):
            old_value = old_best.get(field) if old_best else None
            new_value = new_best.get(field) if new_best else None
            if old_value != new_value:
                diffs.append(_diff(f"{target}.best.{field}", old_value, new_value))
    return _result("verify", diffs)


# --- сценарий: catalog_build ----------------------------------------------------


def _scenario_catalog_build(work: Path) -> dict:
    legacy_dir, new_dir = _side_dirs(work)
    phrase = "test impact analysis"
    gh_payload = _cat_json("gh_search_test_impact.json")
    gh_spec = {
        "auth_ok": True,
        "search": [
            {"match": "in:description,readme", "total": gh_payload["total_count"], "items": gh_payload["items"]}
        ],
        "orgs": {},
    }
    rules = [
        _rule("-/v1/search", {"objects": []}),
        _rule(
            "registry.modelcontextprotocol.io/v0/servers?search=",
            _mcp_json("servers_search_test_impact.json"),
        ),
    ]
    argv = [phrase, "--out", "raw/catalog_query.json"]

    legacy = _load_legacy("catalog_build")
    transport = MapTransport(http_rules_for_side(rules, "legacy"))
    legacy.get = _legacy_catalog_build_get(transport)
    _runner, legacy_run, _auth = make_gh(gh_spec_for_side(gh_spec, "legacy"))
    legacy.subprocess = types.SimpleNamespace(run=legacy_run)
    legacy.time = FakeTime(time)
    _run_legacy(legacy, argv, legacy_dir)

    proc = _run_new("catalog_build", argv, new_dir, rules, gh_spec)
    legacy_cat = _read_json(legacy_dir / "raw" / "catalog_query.json")
    new_cat = _read_json(new_dir / "raw" / "catalog_query.json")
    if legacy_cat is None or new_cat is None:
        return _crash("catalog_build", f"каталог не записан: legacy={legacy_cat is not None} new={proc.returncode}", proc)

    diffs: list[dict] = []
    if legacy_cat.get("queries") != new_cat.get("queries"):
        diffs.append(_diff("queries", legacy_cat.get("queries"), new_cat.get("queries")))
    legacy_items = legacy_cat.get("items") or []
    new_items = new_cat.get("items") or []
    if len(legacy_items) != len(new_items):
        diffs.append(_diff("len(items)", len(legacy_items), len(new_items)))
    legacy_ids = sorted(str(row.get("id")) for row in legacy_items)
    new_ids = sorted(str(row.get("id")) for row in new_items)
    if legacy_ids != new_ids:
        diffs.append(_diff("set(id)", legacy_ids, new_ids))
    old_total = (legacy_cat.get("stats") or {}).get("total")
    new_total = (new_cat.get("stats") or {}).get("total")
    if old_total != new_total:
        diffs.append(_diff("stats.total", old_total, new_total))
    return _result("catalog_build", diffs)


# --- сценарий: catalog_scan -----------------------------------------------------


def _scenario_catalog_scan(work: Path) -> dict:
    """Legacy у catalog_scan не запускается: скрипт исполняется на импорте и пишет
    рядом с собой. Эталон — legacy-экстракт тела цикла из паритетного гейта 1e,
    свежий прогон нового CLI идёт через CLI-входы с ``--base-url`` из фейка.
    """
    new_dir = work / "new"
    (new_dir / "raw").mkdir(parents=True)
    page1 = _mcp_json("servers_page1.json")
    page2 = _mcp_json("servers_page2.json")
    last_page = {"servers": page2["servers"], "metadata": {"count": len(page2["servers"])}}
    rules = [
        _rule("cursor=", last_page),  # страница по курсору — последняя
        _rule("fake.registry.test/v0/servers", page1),
    ]
    proc = _run_new(
        "catalog_scan",
        ["--base-url", FAKE_REGISTRY_URL, "--limit", "10", "--no-sleep",
         "--checkpoint", "ckpt.json", "--json-out", "scan.json"],
        new_dir,
        rules,
    )
    new_summary = _read_json(new_dir / "scan.json")
    if new_summary is None:
        return _crash("catalog_scan", f"scan.json не записан (rc={proc.returncode})", proc)

    from tests.test_core_scan_legacy_contract import _legacy_scan

    items = list(page1["servers"]) + list(page2["servers"])
    legacy = _legacy_scan(items)
    legacy["pages"] = 2
    legacy["finished"] = True
    # seconds (время) и top_namespaces (в legacy-экстракте нет ns-словаря) не сравниваются.
    fields = ("entries", "pages", "unique_names", "namespaces", "class_counts", "delivery_kinds", "finished")
    diffs = [ _diff(field, legacy.get(field), new_summary.get(field)) for field in fields if legacy.get(field) != new_summary.get(field)]
    return _result("catalog_scan", diffs)


# --- сценарий: collect -----------------------------------------------------------


def _collect_gh_spec() -> dict:
    """gh для collect: две темы с находкой, остальные запросы пусты — как в фейке тестов."""
    items = [
        {
            "full_name": "acme/mcp-thing",
            "stargazers_count": 1200,
            "description": "MCP server thing",
            "topics": ["mcp"],
            "language": "Python",
            "pushed_at": "2026-09-01T00:00:00Z",
            "created_at": "2025-01-01T00:00:00Z",
            "forks_count": 3,
            "archived": False,
            "html_url": "https://github.com/acme/mcp-thing",
        },
        {
            "full_name": "beta/mcp-other",
            "stargazers_count": 900,
            "description": "Other MCP server",
            "topics": [],
            "language": "Go",
            "pushed_at": "2026-08-01T00:00:00Z",
            "created_at": "2025-02-01T00:00:00Z",
            "forks_count": 1,
            "archived": False,
            "html_url": "https://github.com/beta/mcp-other",
        },
    ]
    return {
        "auth_ok": True,
        "search": [
            {"match": "topic:mcp-server", "total": len(items), "items": items},
            {"match": "topic:devops", "total": len(items), "items": items},
        ],
        "orgs": {},
    }


def _scenario_collect(work: Path) -> dict:
    legacy_dir, new_dir = _side_dirs(work)
    page1 = _mcp_json("servers_page1.json")
    page2 = _mcp_json("servers_page2.json")
    last_page = {"servers": page2["servers"], "metadata": {"count": len(page2["servers"])}}
    rules = [
        _rule("cursor=", last_page),
        _rule("registry.modelcontextprotocol.io/v0/servers?limit=", page1),
    ]
    gh_spec = _collect_gh_spec()

    legacy = _load_legacy("collect")
    transport = MapTransport(http_rules_for_side(rules, "legacy"))
    _runner, legacy_run, _auth = make_gh(gh_spec_for_side(gh_spec, "legacy"))
    legacy.subprocess = types.SimpleNamespace(run=legacy_run)
    legacy.time = FakeTime(time)
    legacy.urllib = types.SimpleNamespace(
        request=types.SimpleNamespace(urlopen=_legacy_urlopen(transport)),
        parse=urllib.parse,
    )
    _run_legacy(legacy, [], legacy_dir)

    proc = _run_new("collect", ["--keyword", "mcp", "--out-dir", "raw"], new_dir, rules, gh_spec)
    diffs: list[dict] = []
    # Каталог: legacy читает курсор по legacy-ключу next_cursor и берёт одну страницу
    # (allowlist "default cursor"); экосистема и популярные идут через gh и должны совпасть.
    legacy_catalog = _read_json(legacy_dir / "raw" / "catalog.json")
    new_catalog = _read_json(new_dir / "raw" / "catalog.json")
    if legacy_catalog is None or new_catalog is None:
        return _crash("collect", f"catalog.json не записан: legacy={legacy_catalog is not None} new={proc.returncode}", proc)
    if len(legacy_catalog) != len(new_catalog):
        diffs.append(_diff("catalog len", len(legacy_catalog), len(new_catalog), allow="default cursor"))
    legacy_names = sorted(_server_name(row) for row in legacy_catalog)
    new_names = sorted(_server_name(row) for row in new_catalog)
    if legacy_names != new_names:
        diffs.append(_diff("catalog set(name)", legacy_names, new_names, allow="default cursor"))

    for filename, label in (("ecosystem_repos.json", "ecosystem"), ("popular.json", "popular")):
        legacy_data = _read_json(legacy_dir / "raw" / filename)
        new_data = _read_json(new_dir / "raw" / filename)
        if legacy_data is None or new_data is None:
            return _crash("collect", f"{filename} не записан: legacy={legacy_data is not None} new={proc.returncode}", proc)
        if len(legacy_data) != len(new_data):
            diffs.append(_diff(f"{label} len", len(legacy_data), len(new_data)))
        if sorted(legacy_data) != sorted(new_data):
            diffs.append(_diff(f"{label} set(full_name)", sorted(legacy_data), sorted(new_data)))
    return _result("collect", diffs)


# --- сценарий: coverage -----------------------------------------------------------


def _coverage_inputs() -> tuple[list[dict], dict, dict]:
    """Вход coverage: кусок реальной страницы реестра + два репозитория (покрытый и пустой)."""
    catalog = _mcp_json("servers_page1.json")["servers"][:5]
    ecosystem = {
        "acme/mcp-flaky-tool": {
            "full_name": "acme/mcp-flaky-tool",
            "desc": "CLI server for flaky test triage",
            "topics": ["testing"],
        }
    }
    popular = {
        "acme/mcp-flaky-tool": {
            "full_name": "acme/mcp-flaky-tool",
            "stars": 5000,
            "desc": "CLI server for flaky test triage",
            "topics": ["testing"],
            "lang": "Python",
        },
        "totally/unrelated-project": {
            "full_name": "totally/unrelated-project",
            "stars": 1200,
            "desc": "",
            "topics": [],
            "lang": "Go",
        },
    }
    return catalog, ecosystem, popular


def _scenario_coverage(work: Path) -> dict:
    legacy_dir, new_dir = _side_dirs(work)
    catalog, ecosystem, popular = _coverage_inputs()
    for side_dir in (legacy_dir, new_dir):
        raw = side_dir / "raw"
        _write_json(raw / "catalog.json", catalog)
        _write_json(raw / "ecosystem_repos.json", ecosystem)
        _write_json(raw / "popular.json", popular)

    legacy = _load_legacy("coverage")
    _run_legacy(legacy, [], legacy_dir)

    proc = _run_new("coverage", ["--raw-dir", "raw", "--json-out", "summary.json"], new_dir, [])
    summary = _read_json(new_dir / "summary.json")
    legacy_gaps = _read_json(legacy_dir / "raw" / "gaps.json")
    legacy_tools = _read_json(legacy_dir / "raw" / "gap_tools.json")
    legacy_density = _read_json(legacy_dir / "raw" / "saturation.json")
    if None in (summary, legacy_gaps, legacy_tools, legacy_density):
        return _crash("coverage", f"файлы не записаны: summary={summary is not None} new={proc.returncode}", proc)

    legacy_values = {
        "gaps_n": len(legacy_gaps.get("gaps") or []),
        "covered_n": len(legacy_gaps.get("covered") or []),
        "tools_n": len(legacy_tools or []),
        "density": legacy_density,
    }
    diffs = [
        _diff(field, legacy_values[field], summary.get(field))
        for field in ("gaps_n", "covered_n", "tools_n", "density")
        if legacy_values[field] != summary.get(field)
    ]
    return _result("coverage", diffs)


# --- общий каркас запуска сторон --------------------------------------------------


def _load_legacy(name: str) -> Any:
    """Загрузить ``legacy/<name>.py`` через importlib; исполняется один раз, без сети.

    ``catalog_scan`` сюда не попадает: скрипт исполняется на импорте (сеть, файлы
    рядом с собой) — его эталон в L2 это legacy-экстракт из паритетного гейта.
    """
    path = ROOT / "legacy" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"legacy_compat_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"не удалось загрузить {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_legacy(module: Any, argv: list[str], cwd: Path) -> tuple[int, str]:
    """Запустить legacy ``main()`` in-process: свой cwd, свой sys.argv, пойманный stdout."""
    buffer = io.StringIO()
    old_argv, old_cwd = sys.argv, os.getcwd()
    try:
        sys.argv = [Path(module.__file__).name, *argv]
        os.chdir(cwd)
        with contextlib.redirect_stdout(buffer):
            code = module.main()
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)
    return (code if isinstance(code, int) else 0), buffer.getvalue()


def _run_new(
    name: str,
    argv: list[str],
    cwd: Path,
    http_rules: list[dict],
    gh_spec: dict | None = None,
) -> Any:
    """Запустить new CLI subprocess'ом ``python -m tools.compat_runner`` с той же подменой."""
    http_file = cwd / "compat_http.json"
    gh_file = cwd / "compat_gh.json"
    _write_json(http_file, http_rules)
    _write_json(gh_file, gh_spec or {})
    env = dict(os.environ)
    env["GAPAUDIT_COMPAT_HTTP"] = str(http_file)
    env["GAPAUDIT_COMPAT_GH"] = str(gh_file)
    env["GAPAUDIT_DATA_DIR"] = str(cwd / "data")
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "tools.compat_runner", name, *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env,
        timeout=120,
    )


def _legacy_http_get(transport: MapTransport) -> Any:
    """``get`` для legacy check_predecessors: та же сигнатура, тот же фейковый транспорт."""

    def get(url: str, timeout: float = 25, tries: int = 3, raw: bool = False) -> tuple[int | None, Any]:
        status, body = transport(url, {}, timeout)
        if status != 200:
            return status, None
        return status, body if raw else json.loads(body.decode("utf-8"))

    return get


def _legacy_catalog_build_get(transport: MapTransport) -> Any:
    """``get`` для legacy catalog_build: (status, parsed) с повторами без пауз."""

    def get(url: str, timeout: float = 30, tries: int = 3) -> Any:
        status, body = transport(url, {}, timeout)
        if status != 200:
            return None
        return json.loads(body.decode("utf-8"))

    return get


def _legacy_urlopen(transport: MapTransport) -> Any:
    """``urllib.request.urlopen`` для legacy collect: ответ из транспорта, сбой — исключение."""

    class _FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def urlopen(request: Any, timeout: float = 0, **kwargs: Any) -> "_FakeResponse":
        url = request.full_url if hasattr(request, "full_url") else str(request)
        status, body = transport(url, {}, timeout)
        if status != 200:
            raise OSError(f"HTTP {status} для {url}")
        return _FakeResponse(body)

    return urlopen


# --- сравнение и отчёт -------------------------------------------------------------


def _result(script: str, diffs: list[dict]) -> dict:
    """Разложить diff'ы на предупреждения (allowlist) и провалы; ok = нет провалов."""
    allowed = ALLOWLIST.get(script, {})
    warnings, failed = [], []
    for diff in diffs:
        key = diff.get("allow")
        if key is not None and key in allowed:
            warnings.append(f"{diff['field']}: legacy={diff['legacy']!r} new={diff['new']!r} [{key}]")
        else:
            failed.append(diff)
    return {"script": script, "ok": not failed, "warnings": warnings, "diffs": diffs, "failed": failed}


def _crash(script: str, reason: str, proc: Any = None) -> dict:
    detail = reason
    if proc is not None:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-3:]
        if tail:
            detail += " | " + " / ".join(tail)
    diff = _diff("сценарий", "", detail)
    return {"script": script, "ok": False, "warnings": [], "diffs": [diff], "failed": [diff]}


def _diff(field: str, legacy: Any, new: Any, *, allow: str | None = None) -> dict:
    return {"field": field, "legacy": legacy, "new": new, "allow": allow}


def _cmp_rows_by_name(
    legacy_rows: list[dict],
    new_rows: list[dict],
    fields: tuple[str, ...],
    *,
    names_allow: str | None = None,
) -> list[dict]:
    """Построчное сравнение по имени: набор имён + белый список полей."""
    diffs: list[dict] = []
    legacy_by = {row.get("name"): row for row in legacy_rows}
    new_by = {row.get("name"): row for row in new_rows}
    only_legacy = sorted(str(name) for name in set(legacy_by) - set(new_by))
    only_new = sorted(str(name) for name in set(new_by) - set(legacy_by))
    if only_legacy or only_new:
        diffs.append(_diff("имена", only_legacy, only_new, allow=names_allow))
    for name in sorted(set(legacy_by) & set(new_by), key=str):
        for field in fields:
            old_value, new_value = legacy_by[name].get(field), new_by[name].get(field)
            if old_value != new_value:
                diffs.append(_diff(f"{name}.{field}", old_value, new_value))
    return diffs


def _print_details(result: dict) -> None:
    """--verbose: расписать предупреждения и провалы одного сценария."""
    for line in result.get("warnings", []):
        print(f"    warning: {line}")
    for diff in result.get("failed", []):
        print(f"    FAIL: {diff['field']}: legacy={diff['legacy']!r} new={diff['new']!r}")


# --- мелкие помощники ---------------------------------------------------------------


def _side_dirs(work: Path) -> tuple[Path, Path]:
    """Каталоги сторон: в каждом свой ``raw/`` (legacy-скрипты пишут туда без makedirs)."""
    legacy_dir, new_dir = work / "legacy", work / "new"
    (legacy_dir / "raw").mkdir(parents=True, exist_ok=True)
    (new_dir / "raw").mkdir(parents=True, exist_ok=True)
    return legacy_dir, new_dir


def _rule(match: str, body: Any, status: int = 200, side: str | None = None) -> dict:
    rule: dict[str, Any] = {"match": match, "status": status, "body": body}
    if side is not None:
        rule["side"] = side
    return rule


def _fixture(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _http_json(name: str) -> Any:
    return _fixture(FIXTURES / "http" / name)


def _mcp_json(name: str) -> Any:
    return _fixture(FIXTURES / "mcp_registry" / name)


def _gh_json(name: str) -> Any:
    return _fixture(FIXTURES / "github" / name)


def _cat_json(name: str) -> Any:
    return _fixture(FIXTURES / "catalog" / name)


def _server_name(row: Any) -> str:
    """Имя сервера из сырых элементов страницы реестра (``{"server": {...}}``)."""
    if not isinstance(row, dict):
        return ""
    server = row.get("server") if isinstance(row.get("server"), dict) else row
    return str(server.get("name") or "")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    """Запись JSON во временное дерево гейта; ``newline="\\n"`` — gotcha 17."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


# --- точка входа ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compat_check.py",
        description="Гейт «метод не изменился незаметно»: sha256 legacy + L1 (паритетные тесты) + L2 (legacy vs new)",
    )
    parser.add_argument("--scripts", default="all", metavar="LIST",
                        help="какие скрипты гонять в L2: all или имена через запятую")
    parser.add_argument("--skip-l1", action="store_true", help="не гонять L1 (pytest-паритет)")
    parser.add_argument("--skip-l2", action="store_true", help="не гонять L2 (legacy vs new)")
    parser.add_argument("--verbose", action="store_true", help="печатать предупреждения и diff")
    parser.add_argument("--json-out", metavar="FILE", help="сводка JSON по всем уровням")
    args = parser.parse_args(argv)

    scripts: list[str] = (
        list(SCRIPTS) if args.scripts == "all" else [part.strip() for part in args.scripts.split(",") if part.strip()]
    )
    unknown = [name for name in scripts if name not in SCRIPTS]
    if unknown:
        print(f"[compat_check] неизвестные скрипты: {', '.join(unknown)}; есть: {', '.join(SCRIPTS)}", file=sys.stderr)
        return 2

    sha_problems = check_legacy_sha()
    l1_ok, l1_output = (True, "L1 пропущен (--skip-l1)") if args.skip_l1 else check_l1()
    l2_results = [] if args.skip_l2 else check_l2(scripts, verbose=args.verbose)

    _print_table(l1_ok if not args.skip_l1 else None, l2_results, sha_problems)
    if sha_problems:
        print("\nSHA256 legacy/ РАСХОДИТСЯ:")
        for problem in sha_problems:
            print(f"  {problem}")
    if not args.skip_l1:
        print(f"\nL1: {'зелёный' if l1_ok else 'КРАСНЫЙ'} — {l1_output.strip().splitlines()[-1] if l1_output.strip() else '(вывод пуст)'}")
        if not l1_ok:
            print(l1_output)
    for result in l2_results:
        if not result["ok"]:
            print(f"\nL2 FAIL [{result['script']}]:")
            for diff in result["failed"]:
                print(f"  {diff['field']}: legacy={diff['legacy']!r} new={diff['new']!r}")

    if args.json_out:
        _write_json(
            Path(args.json_out),
            {
                "sha_problems": sha_problems,
                "l1": {"ok": l1_ok, "skipped": bool(args.skip_l1), "output": l1_output},
                "l2": l2_results,
            },
        )

    if sha_problems:
        return 3
    if not args.skip_l1 and not l1_ok:
        return 2
    if any(not result["ok"] for result in l2_results):
        return 1
    return 0


def _print_table(l1_ok: bool | None, l2_results: list[dict], sha_problems: list[str]) -> None:
    """Таблица: script / L1 / L2 / sha256. L1 — общий гейт, колонка отражает его результат."""
    l1_text = "—" if l1_ok is None else ("OK" if l1_ok else "FAIL")
    bad_sha = {problem.split(":")[0] for problem in sha_problems}
    print(f"{'script':<20} {'L1':<5} {'L2':<12} sha256")
    for name in SCRIPTS:
        result = next((item for item in l2_results if item["script"] == name), None)
        if result is None:
            l2_text = "—"
        elif not result["ok"]:
            l2_text = f"FAIL ({len(result['failed'])}d)"
        elif result["warnings"]:
            l2_text = f"OK ({len(result['warnings'])}w)"
        else:
            l2_text = "OK"
        sha_text = "FAIL" if name in bad_sha else "OK"
        print(f"{name:<20} {l1_text:<5} {l2_text:<12} {sha_text}")


if __name__ == "__main__":
    sys.exit(main())
