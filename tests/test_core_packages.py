"""Тесты core/packages.py — на фикстурах, без сети (шаг 1c).

Транспорт фейковый и раздаёт разные фикстуры на разные URL (сопоставление по подстроке),
поэтому один клиент обслуживает и PyPI, и npm, и статистику скачиваний.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import subprocess
import sys

import pytest

from core.cache import DictCache
from core.diffing import VERDICT_ALIVE, VERDICT_PRESENT, VERDICT_STALE
from core.http import HttpClient, Throttle
from core.packages import (
    POPULAR_URL,
    PackageCheckConfig,
    check,
    check_many,
    lev,
    names_from_file,
    popular_names,
    similar_names,
)
from core.registry import (
    RegistryClient,
    npm_downloads_url,
    npm_search_url,
    npm_url,
    pypi_url,
    pypistats_url,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "http"
ROOT = pathlib.Path(__file__).resolve().parents[1]

PYPI_REQUESTS = pypi_url("requests")
NPM_REQUESTS = npm_url("requests")
PYPISTATS_REQUESTS = pypistats_url("requests")
NPM_DOWNLOADS_REQUESTS = npm_downloads_url("requests")
SEARCH_REQEUSTS = npm_search_url("reqeusts", 25)

PYPI_PAYLOAD = json.loads((FIXTURES / "pypi_requests.json").read_text(encoding="utf-8"))
NPM_PAYLOAD = json.loads((FIXTURES / "npm_requests.json").read_text(encoding="utf-8"))
PYPISTATS_PAYLOAD = json.loads((FIXTURES / "pypistats_requests.json").read_text(encoding="utf-8"))


# --- обвязка -------------------------------------------------------------------------------


class FixtureTransport:
    """Отдаёт ответ по подстроке URL; неизвестный URL — 404 «пакета нет» (как живой PyPI)."""

    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = dict(mapping)
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        for fragment, response in self.mapping.items():
            if fragment in url:
                return response
        return 404, b'{"message": "Not Found"}'


def make_client(mapping: dict[str, tuple[int, bytes]] | None = None) -> tuple[RegistryClient, FixtureTransport]:
    transport = FixtureTransport(mapping or {})
    http = HttpClient(
        cache=DictCache(),
        throttle=Throttle(
            per_minute={
                "pypi.org": 6000,
                "registry.npmjs.org": 6000,
                "pypistats.org": 6000,
                "api.npmjs.org": 6000,
                "hugovk.github.io": 6000,
            }
        ),
        transport=transport,
        sleep=lambda _seconds: None,
    )
    return RegistryClient(http), transport


def body(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def pypi_body(upload_time: str = "2026-05-14T19:25:26", summary: str = "Python HTTP for Humans.") -> bytes:
    """Фикстура PyPI с подменённой датой релиза или описанием (для «ЗАСТОЯ» и признака ниши)."""
    payload = json.loads(json.dumps(PYPI_PAYLOAD))
    version = payload["info"]["version"]
    payload["info"]["summary"] = summary
    payload["releases"][version][0]["upload_time"] = upload_time
    return body(payload)


def npm_body(latest_time: str) -> bytes:
    """Фикстура npm с подменённой датой релиза (чтобы вердикт не упирался в «ЗАСТОЙ»)."""
    payload = json.loads(json.dumps(NPM_PAYLOAD))
    latest = payload["dist-tags"]["latest"]
    payload["time"][latest] = latest_time
    return body(payload)


def today_iso() -> str:
    from datetime import date

    return date.today().strftime("%Y-%m-%dT00:00:00.000Z")


class SyncExecutor:
    """Синхронный двойник ThreadPoolExecutor: тот же map, но без потоков."""

    def map(self, fn, iterable):  # noqa: ANN001, ANN201 — контракт как у ThreadPoolExecutor
        return [fn(item) for item in iterable]


# --- check() -------------------------------------------------------------------------------


def test_check_live_package_with_adoption() -> None:
    client, transport = make_client(
        {
            PYPI_REQUESTS: (200, pypi_body()),
            PYPISTATS_REQUESTS: (200, body(PYPISTATS_PAYLOAD)),
        }
    )

    row = check("requests", client)

    assert row["name"] == "requests"
    assert row["src"] == "pypi"
    assert row["ver"] == "2.34.2"
    assert row["date"] == "2026-05-14"
    assert row["summary"] == "Python HTTP for Humans."
    assert row["home"] == ""
    assert row["flag"] is False and row["mcp"] is False
    assert row["month"] == 1202777296 and row["week"] == 317314822
    assert row["verdict"] == VERDICT_ALIVE
    assert isinstance(row["age_days"], int) and row["age_days"] >= 0
    assert PYPI_REQUESTS in transport.calls and PYPISTATS_REQUESTS in transport.calls


def test_check_missing_package() -> None:
    client, _ = make_client()

    row = check("requests-nonexistent-fake-package", client)

    assert row["verdict"] == "ОТСУТСТВУЕТ"
    assert row["src"] == "-"
    assert row["flag"] is False and row["mcp"] is False
    assert row["month"] is None and row["week"] is None
    assert row["ver"] is None and row["date"] is None and row["age_days"] is None


def test_check_flags_niche_keyword_in_summary() -> None:
    client, _ = make_client(
        {
            PYPI_REQUESTS: (200, pypi_body(summary="An MCP server for PostgreSQL")),
            PYPISTATS_REQUESTS: (200, body(PYPISTATS_PAYLOAD)),
        }
    )

    row = check("requests", client)

    assert row["flag"] is True and row["mcp"] is True
    assert row["summary"] == "An MCP server for PostgreSQL"


def test_check_stale_when_release_is_old() -> None:
    """Скачивания есть, релизов нет с 2020 года -> «ЗАСТОЙ» вместо «ЖИВО»."""
    client, _ = make_client(
        {
            PYPI_REQUESTS: (200, pypi_body(upload_time="2020-01-01T00:00:00")),
            PYPISTATS_REQUESTS: (200, body(PYPISTATS_PAYLOAD)),
        }
    )

    row = check("requests", client)

    assert row["date"] == "2020-01-01"
    assert row["week"] == 317314822
    assert row["verdict"] == VERDICT_STALE
    assert row["age_days"] is not None and row["age_days"] > 540


def test_check_published_but_adoption_not_measured() -> None:
    client, _ = make_client({PYPI_REQUESTS: (200, pypi_body())})

    row = check("requests", client)

    assert row["month"] is None and row["week"] is None
    assert row["verdict"] == VERDICT_PRESENT


def test_check_npm_only_uses_npm_card_and_downloads() -> None:
    client, _ = make_client(
        {
            NPM_REQUESTS: (200, npm_body(today_iso())),
            NPM_DOWNLOADS_REQUESTS: (200, body({"downloads": 500})),
        }
    )

    row = check("requests", client)

    assert row["src"] == "npm"
    assert row["ver"] == "0.3.0"
    assert row["week"] == 500 and row["month"] == 2000
    assert row["verdict"] == VERDICT_ALIVE


def test_check_mcp_in_npm_metadata() -> None:
    payload = json.loads(json.dumps(NPM_PAYLOAD))
    latest = payload["dist-tags"]["latest"]
    payload["versions"][latest]["keywords"] = ["mcp-server"]
    client, _ = make_client({NPM_REQUESTS: (200, body(payload))})

    row = check("requests", client)

    assert row["src"] == "npm" and row["mcp"] is True


# --- check_many() --------------------------------------------------------------------------


def test_check_many_keeps_order_with_injected_executor() -> None:
    client, _ = make_client(
        {
            PYPI_REQUESTS: (200, pypi_body()),
            PYPISTATS_REQUESTS: (200, body(PYPISTATS_PAYLOAD)),
        }
    )
    executor = SyncExecutor()

    rows = check_many(["requests", "requests-fake", "requests"], client, executor=executor)

    assert [row["name"] for row in rows] == ["requests", "requests-fake", "requests"]
    assert rows[0]["verdict"] == VERDICT_ALIVE
    assert rows[1]["verdict"] == "ОТСУТСТВУЕТ"


def test_check_many_default_pool_gives_same_result() -> None:
    client, _ = make_client(
        {
            PYPI_REQUESTS: (200, pypi_body()),
            PYPISTATS_REQUESTS: (200, body(PYPISTATS_PAYLOAD)),
        }
    )

    rows = check_many(["requests", "requests-fake"], client)

    assert [row["name"] for row in rows] == ["requests", "requests-fake"]
    assert rows[0]["verdict"] == VERDICT_ALIVE
    assert rows[1]["verdict"] == "ОТСУТСТВУЕТ"


def test_check_many_empty_list() -> None:
    client, _ = make_client()

    assert check_many([], client) == []


# --- names_from_file() ---------------------------------------------------------------------


def legacy_names_from_file(path: str) -> list[str]:
    """Эталонный разбор из legacy/check_predecessors.py (импорт локальный, без сети)."""
    module = importlib.import_module("legacy.check_predecessors")
    return module.names_from_file(path)


def test_names_from_file_requirements(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "# комментарий\n"
        "requests>=2.31\n"
        "flask==3.0.0  # inline-комментарий\n"
        "-r other.txt\n"
        "httpx[http2]>=0.27\n"
        "Django~=4.2\n"
        "\n"
        "requests\n",
        encoding="utf-8",
    )

    assert names_from_file(str(path)) == ["Django", "flask", "httpx", "requests"]
    assert names_from_file(str(path)) == legacy_names_from_file(str(path))


def test_names_from_file_package_json(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "package.json"
    path.write_text(
        json.dumps(
            {
                "dependencies": {"zod": "^3.22.0", "axios": "1.6.0"},
                "devDependencies": {"@types/node": "^20.0.0", "jest": "^29.0.0"},
            }
        ),
        encoding="utf-8",
    )

    result = names_from_file(str(path))

    assert result == ["axios", "jest", "zod"]
    # legacy отдаёт порядок файла и без дедупликации; в CLI он всё равно сортируется
    # (`sorted(dict.fromkeys(names))`), поэтому сравниваем по множеству.
    assert result == sorted(set(legacy_names_from_file(str(path))))


def test_names_from_file_pyproject_toml(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        "[project]\n"
        'name = "gapsample"\n'
        "dependencies = [\n"
        '  "fastapi>=0.100",\n'
        '  "uvicorn[standard]>=0.20",\n'
        "]\n",
        encoding="utf-8",
    )

    result = names_from_file(str(path))

    # «dependencies» и «name» — причуда построчного разбора legacy; сохранена намеренно.
    assert result == ["dependencies", "fastapi", "name", "uvicorn"]
    assert result == legacy_names_from_file(str(path))


def test_names_from_file_missing_or_broken(tmp_path: pathlib.Path) -> None:
    broken_json = tmp_path / "package.json"
    broken_json.write_text("{не json", encoding="utf-8")

    assert names_from_file(str(tmp_path / "нет-такого.txt")) == []
    assert names_from_file(str(broken_json)) == []


# --- lev() ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "cap", "expected"),
    [
        ("", "", 3, 0),
        ("abc", "abc", 3, 0),
        ("kitten", "sitting", 3, 3),
        ("kitten", "sitting", 2, 3),
        ("reqeusts", "requests", 2, 2),
        ("abc", "abcdefgh", 3, 4),
    ],
)
def test_lev(a: str, b: str, cap: int, expected: int) -> None:
    assert lev(a, b, cap) == expected
    assert lev(b, a, cap) == expected


# --- popular_names() -----------------------------------------------------------------------


def test_popular_names_from_dataset() -> None:
    rows = {"rows": [{"project": "requests"}, {"project": "flask"}, {"нет": "project"}, "мусор"]}
    client, transport = make_client({POPULAR_URL: (200, body(rows))})

    assert popular_names(client) == ["requests", "flask"]
    assert transport.calls == [POPULAR_URL]


def test_popular_names_empty_on_network_error() -> None:
    def dead_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        raise TimeoutError("timed out")

    http = HttpClient(
        cache=DictCache(),
        throttle=Throttle(per_minute={"hugovk.github.io": 6000}),
        transport=dead_transport,
        sleep=lambda _seconds: None,
    )

    assert popular_names(RegistryClient(http)) == []


def test_popular_names_empty_on_500_after_two_tries() -> None:
    client, transport = make_client({POPULAR_URL: (500, b"oops")})

    assert popular_names(client) == []
    assert len(transport.calls) == 2  # POPULAR_TRIES = 2, как в legacy


def test_popular_names_empty_on_unexpected_payload() -> None:
    client, _ = make_client({POPULAR_URL: (200, body({"rows": "нет"}))})

    assert popular_names(client) == []


# --- similar_names() -----------------------------------------------------------------------


def test_similar_names_finds_popular_typosquat() -> None:
    client, _ = make_client({SEARCH_REQEUSTS: (200, (FIXTURES / "npm_search_requests.json").read_bytes())})

    hits = similar_names("reqeusts", client, popular=["requests", "flask"])

    assert hits == [("requests", 2)]


def test_similar_names_includes_npm_search_results() -> None:
    search = {"objects": [{"package": {"name": "reqeusts2"}}]}
    client, _ = make_client({SEARCH_REQEUSTS: (200, body(search))})

    hits = similar_names("reqeusts", client, popular=["requests", "flask"])

    assert hits == [("reqeusts2", 1), ("requests", 2)]


def test_similar_names_sorts_by_distance_then_name() -> None:
    client, _ = make_client({SEARCH_REQEUSTS: (200, body({"objects": []}))})

    hits = similar_names("reqeusts", client, popular=["reqeusts2", "reqeusts1"])

    assert hits == [("reqeusts1", 1), ("reqeusts2", 1)]


def test_similar_names_respects_cap_and_skips_self() -> None:
    client, _ = make_client({SEARCH_REQEUSTS: (200, body({"objects": []}))})

    assert similar_names("reqeusts", client, cap=1, popular=["requests", "reqeusts"]) == []


def test_similar_names_limits_to_fifteen() -> None:
    popular = [f"reqeusts{i:02d}" for i in range(40)]
    client, _ = make_client({SEARCH_REQEUSTS: (200, body({"objects": []}))})

    assert len(similar_names("reqeusts", client, popular=popular)) == 15


# --- чистота импорта -----------------------------------------------------------------------


def test_packages_module_has_no_mutable_globals() -> None:
    """Никаких глобалов-накопителей: состояние живёт в клиенте и конфиге, а не в модуле."""
    import core.diffing as diffing
    import core.packages as packages

    for module in (packages, diffing):
        mutable = [
            name
            for name in vars(module)
            if not name.startswith("__")
            and isinstance(vars(module)[name], (dict, list, set, bytearray))
        ]
        assert mutable == [], f"{module.__name__}: изменяемые глобалы {mutable}"


def test_import_has_no_network_and_no_file_writes() -> None:
    """Импорт core.packages/core.diffing не ходит в сеть и не пишет файлов (критерий DEVPLAN §4)."""
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

import core.diffing, core.packages

print("ok", core.packages.check.__name__, core.diffing.diff_observations.__name__)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ok check diff_observations")


def test_check_config_is_frozen_with_legacy_defaults() -> None:
    cfg = PackageCheckConfig()

    assert cfg.keyword_label == "MCP"
    assert cfg.workers == 6
    assert (cfg.verdict.alive_weekly, cfg.verdict.stale_days) == (100, 540)

    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.workers = 1  # type: ignore[misc]
