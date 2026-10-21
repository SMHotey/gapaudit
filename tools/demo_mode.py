"""Demo-режим GapAudit: данные из фикстур, сеть выключена (шаг P4d, DEVPLAN §11).

Активация — ``--demo`` у ``desktop.py``/CLI или ``GAPAUDIT_DEMO=1`` в окружении.
Назначение: разработка UI, тесты без сети, презентации. Ничего не пишется в
каталог данных пользователя: кэш в памяти, ``settings.json`` и
``api_keys.json`` не читаются, файлы задач не создаются.

Три части, каждая — отдельная точка входа:

* :func:`make_demo_settings` — :class:`core.settings.Settings` с дефолтами
  (файл настроек не читается: демо не должно зависеть от чужого состояния);
* :func:`make_demo_http_client` — :class:`core.http.HttpClient` с транспортом,
  который отвечает из ``tests/fixtures`` и никогда не открывает сокет;
* :func:`seed_demo_store` — наполняет :class:`app.store.Store` демо-данными
  (18 наблюдений трёх замеров, 8 событий, 2 каталога) теми же методами, что
  пишут хуки задач, поэтому матрица, momentum, таймлайн и каталоги работают
  без единой сетевой операции.

Честно о данных: ``requests`` — снимки настоящих ответов PyPI/npm/pypistats
(``tests/fixtures/http``), остальные пакеты демо-набора — синтетические числа
из таблицы :data:`DEMO_PACKAGES`, специально подобранные так, чтобы на матрице
были видны рост, спад и стабильность. Демо-режим не притворяется живым замером
и не подменяет результат настоящего прогона.

``gh`` в демо-режиме подменяется (:func:`install_demo_gh`): GitHub Search идёт
подпроцессом, а демо обязано работать без сети и без авторизации.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app.store import Store
from core.cache import DictCache
from core.http import HttpClient, Throttle, Transport
from core.mcp_registry import MCP_LIST_URL, MCP_SEARCH_URL
from core.registry import NPM_SEARCH_API, npm_url, pypi_url, pypistats_url
from core.settings import Settings

__all__ = [
    "DEMO_PACKAGES",
    "ENV_DEMO",
    "FIXTURES_DIR",
    "install_demo_gh",
    "make_demo_data_dir",
    "make_demo_http_client",
    "make_demo_settings",
    "make_demo_transport",
    "seed_demo_store",
]

#: Переменная окружения — тот же смысл, что ``--demo`` у CLI и desktop.py.
ENV_DEMO = "GAPAUDIT_DEMO"

#: Фикстуры лежат в репозитории; каталог считается от файла, не от cwd.
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

#: Демо-набор пакетов: имя, источник, версия, дата, adoption за неделю и
#: множители двух прошлых замеров (30 и 7 дней назад) — из них считается momentum.
#: ``requests`` берёт adoption из фикстуры pypistats (множители применяются к ней).
DEMO_PACKAGES: tuple[dict[str, Any], ...] = (
    {
        "name": "requests",
        "src": "pypi",
        "ver": "2.32.3",
        "date": "2025-01-15",
        "summary": "Python HTTP for Humans",
        "home": "https://requests.readthedocs.io",
        "flag": False,
        "week": None,  # из фикстуры pypistats_requests.json
        "old_30d": 0.90,
        "old_7d": 0.95,
    },
    {
        "name": "ruff",
        "src": "pypi",
        "ver": "0.14.2",
        "date": "2026-10-06",
        "summary": "An extremely fast Python linter and code formatter, written in Rust",
        "home": "https://docs.astral.sh/ruff",
        "flag": False,
        "week": 2_400_000,
        "old_30d": 0.72,
        "old_7d": 0.86,
    },
    {
        "name": "uv",
        "src": "pypi",
        "ver": "0.9.6",
        "date": "2026-10-08",
        "summary": "An extremely fast Python package and project manager, written in Rust",
        "home": "https://docs.astral.sh/uv",
        "flag": False,
        "week": 9_100_000,
        "old_30d": 0.80,
        "old_7d": 0.90,
    },
    {
        "name": "mypy",
        "src": "pypi",
        "ver": "1.20.2",
        "date": "2026-09-29",
        "summary": "Optional static typing for Python",
        "home": "https://www.mypy-lang.org",
        "flag": False,
        "week": 1_850_000,
        "old_30d": 1.10,
        "old_7d": 1.04,
    },
    {
        "name": "sqlalchemy",
        "src": "pypi",
        "ver": "2.0.44",
        "date": "2026-09-12",
        "summary": "Database Abstraction Library",
        "home": "https://www.sqlalchemy.org",
        "flag": False,
        "week": 4_300_000,
        "old_30d": 0.97,
        "old_7d": 0.99,
    },
    {
        "name": "fastmcp",
        "src": "pypi",
        "ver": "2.12.4",
        "date": "2026-10-15",
        "summary": "The fast, Pythonic way to build MCP servers and clients",
        "home": "https://gofastmcp.com",
        "flag": True,
        "week": 12_400,
        "old_30d": 0.55,
        "old_7d": 0.78,
    },
)

#: События демо-таймлайна: (сколько дней назад, вид, пакет, подробность).
DEMO_EVENTS: tuple[tuple[int, str, str, str], ...] = (
    (21, "release", "ruff", "0.14.0 → 0.14.2 (2026-10-06)"),
    (18, "adoption_change", "fastmcp", "+41% за неделю"),
    (15, "flag_appeared", "fastmcp", "найден MCP-сервер в README"),
    (12, "release", "uv", "0.9.4 → 0.9.6 (2026-10-08)"),
    (9, "adoption_change", "uv", "+12% за неделю"),
    (7, "verdict_change", "mypy", "ЖИВОЙ → ЗАСТОЙ"),
    (4, "release", "sqlalchemy", "2.0.43 → 2.0.44 (2026-09-12)"),
    (2, "missing", "requests", "пакет не найден в реестре MCP"),
)

#: Каталоги демо-режима: (id, фразы, сводка, строки).
DEMO_CATALOGS: tuple[tuple[str, list[str], dict[str, int], list[dict[str, Any]]], ...] = (
    (
        "demo-catalog-1",
        ["test impact analysis"],
        {"found": 126, "relevant": 87, "fresh": 41},
        [
            {
                "name": "tia-mcp",
                "source": "github",
                "url": "https://github.com/example/tia-mcp",
                "desc": "Test impact analysis server for MCP clients",
                "stars": 412,
                "lang": "Python",
                "pushed": "2026-10-11",
            },
            {
                "name": "impact-graph",
                "source": "github",
                "url": "https://github.com/example/impact-graph",
                "desc": "Build a graph of test impact from coverage reports",
                "stars": 268,
                "lang": "Python",
                "pushed": "2026-09-30",
            },
            {
                "name": "@example/impact-runner",
                "source": "npm",
                "url": "https://www.npmjs.com/package/@example/impact-runner",
                "desc": "Runs only the tests affected by a change",
                "stars": None,
                "lang": "TypeScript",
                "pushed": "2026-10-02",
            },
            {
                "name": "pytest-impact",
                "source": "pypi",
                "url": "https://pypi.org/project/pytest-impact/",
                "desc": "pytest plugin: order tests by change impact",
                "stars": None,
                "lang": "Python",
                "pushed": "2026-08-19",
            },
            {
                "name": "example/legacy-tia",
                "source": "github",
                "url": "https://github.com/example/legacy-tia",
                "desc": "Archived: impact analysis for a monorepo",
                "stars": 51,
                "lang": "Java",
                "pushed": "2024-02-01",
            },
        ],
    ),
    (
        "demo-catalog-2",
        ["mcp server testing"],
        {"found": 64, "relevant": 39, "fresh": 18},
        [
            {
                "name": "mcp-test-server",
                "source": "registry",
                "url": "",
                "desc": "Reference MCP server for client conformance tests",
                "stars": None,
                "lang": None,
                "pushed": "",
            },
            {
                "name": "mcp-client-harness",
                "source": "github",
                "url": "https://github.com/example/mcp-client-harness",
                "desc": "Harness for testing MCP clients and servers",
                "stars": 133,
                "lang": "Python",
                "pushed": "2026-10-09",
            },
        ],
    ),
)

#: Что вернуть на запрос к ``gh`` в демо-режиме (снимок GitHub Search).
GH_FIXTURE = FIXTURES_DIR / "catalog" / "gh_search_test_impact.json"

#: Маркер установки подмены ``gh``: второй вызов не должен ставить обёртку поверх.
_gh_installed = False


def make_demo_settings() -> Settings:
    """Настройки демо-режима: дефолты ``Settings()``, без чтения ``settings.json``.

    Ключевые слова тоже дефолтные (``mcp``, ``model context protocol``): демо
    не должно зависеть от того, что пользователь настроил у себя.
    """
    return Settings()


def make_demo_http_client(settings: Settings | None = None) -> HttpClient:
    """HTTP-клиент демо-режима: фикстуры вместо сети, кэш и троттлинг в памяти.

    Возвращаемый клиент физически не может сходить в сеть: транспорт —
    :func:`make_demo_transport` (словарь «URL → байты»), кэш — ``DictCache``,
    троттлинг выключен (``per_minute=0``), иначе демо тратило бы реальные
    секунды на паузы между ответами, которых нет.
    """
    active = settings if settings is not None else make_demo_settings()
    return HttpClient(
        cache=DictCache(active.cache_ttl_hours),
        throttle=Throttle(default_per_minute=0),
        transport=make_demo_transport(),
    )


def make_demo_transport() -> Transport:
    """Транспорт из фикстур: отвечает только на известные URL, иначе ``404``.

    Обслуживаются: PyPI/npm/pypistats для ``requests`` (снятые ответы), поиск
    npm, поиск и список реестра MCP. Неизвестный URL — ``(404, b"")``, как в
    тестовом бэкдоре ``GAPAUDIT_FAKE_HTTP``: «пакета нет» — валидный ответ
    метода, а не повод падать.
    """
    routes: dict[str, bytes] = {}

    def add(url: str, path: Path) -> None:
        routes[url] = path.read_bytes()

    add(pypi_url("requests"), FIXTURES_DIR / "http" / "pypi_requests.json")
    add(npm_url("requests"), FIXTURES_DIR / "http" / "npm_requests.json")
    add(pypistats_url("requests"), FIXTURES_DIR / "http" / "pypistats_requests.json")

    prefixes: tuple[tuple[str, bytes], ...] = (
        (NPM_SEARCH_API.split("{")[0], (FIXTURES_DIR / "http" / "npm_search_requests.json").read_bytes()),
        (MCP_SEARCH_URL.split("{")[0], (FIXTURES_DIR / "mcp_registry" / "servers_search_impact.json").read_bytes()),
        (MCP_LIST_URL.split("{")[0], (FIXTURES_DIR / "mcp_registry" / "servers_page1.json").read_bytes()),
    )

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        body = routes.get(url)
        if body is not None:
            return 200, body
        for prefix, payload in prefixes:
            if url.startswith(prefix):
                return 200, payload
        return 404, b""

    return transport


def install_demo_gh() -> Callable[[], None]:
    """Подменить ``gh`` в текущем процессе: GitHub Search отвечает фикстурой.

    ``core.github`` ходит в GitHub подпроцессом ``gh api``; в демо-режиме сеть
    запрещена, а ``gh`` может быть не авторизован. Обёртка перехватывает только
    запуск ``gh`` и только ``search/repositories`` (страница 1 — снимок из
    ``tests/fixtures/catalog``, дальше пустая страница: обход останавливается
    сам). Всё остальное — настоящий :func:`subprocess.run`.

    Возвращает функцию отмены (для тестов); повторная установка ничего не делает.
    """
    global _gh_installed
    if _gh_installed:
        return lambda: None

    original = subprocess.run
    snapshot = GH_FIXTURE.read_bytes()

    def fake_run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        command = list(argv) if isinstance(argv, (list, tuple)) else [str(argv)]
        if command and Path(str(command[0])).name.lower() in {"gh", "gh.exe"}:
            return _demo_gh_result(command[1:], snapshot)
        return original(argv, *args, **kwargs)

    subprocess.run = fake_run  # type: ignore[assignment]
    _gh_installed = True

    def undo() -> None:
        global _gh_installed
        subprocess.run = original  # type: ignore[assignment]
        _gh_installed = False

    return undo


def make_demo_data_dir() -> Path:
    """Временный каталог данных демо-режима (вне проекта и вне ``GapAudit-data``).

    Демо не должно ни читать, ни засорять настоящий каталог данных, поэтому
    ``Store`` открывается на файле во временном каталоге, а каталог удаляется
    при выходе (``desktop.py``). ``:memory:`` не годится: приложение открывает
    ``Store`` само, в ``lifespan``, и путь к БД приходит из ``data_dir``.
    """
    return Path(tempfile.mkdtemp(prefix="gapaudit-demo-"))


def seed_demo_store(
    store: Store,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Наполнить ``Store`` демо-данными; вернуть счётчики записанного.

    Пишет ровно теми методами, что и хуки завершения задач
    (``record_observations``/``record_events``/``record_catalog``), поэтому
    экраны видят обычные строки БД, а не особый «демо-формат».

    Три замера (30 и 7 дней назад, сейчас) дают momentum: окна 7 и 30 дней
    содержат по два и три замера. ``force=False`` — повторный вызов на уже
    наполненном Store ничего не пишет (идемпотентность для тестов и повторного
    старта).
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    counts = {"observations": 0, "events": 0, "catalogs": 0, "items": 0}
    if not force and store.count_observations() > 0:
        return counts

    shifts = (timedelta(days=30), timedelta(days=7), timedelta())
    for index, shift in enumerate(shifts):
        moment = _iso(current - shift)
        batch = [
            _observation(package, index=index)
            for package in DEMO_PACKAGES
        ]
        store.record_observations(
            batch, run_id=f"demo-run-{index + 1}", measured_at=moment, keyword_label="MCP"
        )
        counts["observations"] += len(batch)

    events = [
        {
            "kind": kind,
            "name": name,
            "detail": detail,
        }
        for days, kind, name, detail in DEMO_EVENTS
    ]
    at_values = [_iso(current - timedelta(days=days)) for days, _, _, _ in DEMO_EVENTS]
    # record_events пишет одну метку на батч, поэтому пишем по одному событию —
    # иначе таймлайн склеился бы в один момент и перестал быть таймлайном.
    for event, at in zip(events, at_values):
        store.record_event(
            run_id="demo-events",
            kind=event["kind"],
            name=event["name"],
            detail=event["detail"],
            at=at,
        )
        counts["events"] += 1

    for catalog_id, queries, stats, items in DEMO_CATALOGS:
        store.record_catalog(catalog_id=catalog_id, queries=list(queries), stats=dict(stats))
        counts["items"] += store.record_catalog_items(catalog_id, items)
        counts["catalogs"] += 1
    return counts


# --- внутреннее ----------------------------------------------------------------


def _requests_week() -> int:
    """Adoption ``requests`` из снимка pypistats (``last_week``) — без сети."""
    payload = json.loads(
        (FIXTURES_DIR / "http" / "pypistats_requests.json").read_text(encoding="utf-8")
    )
    return int(payload["data"]["last_week"])


def _observation(package: dict[str, Any], *, index: int) -> dict[str, Any]:
    """Строка наблюдения для замера ``index`` (0 — 30 дней назад, 2 — сейчас).

    Ключи — контракт ``core.packages.check()`` (``tests/test_store.observation``),
    значения — таблица :data:`DEMO_PACKAGES`: текущий замер как есть, прошлые —
    с множителями ``old_30d``/``old_7d``.
    """
    week = int(package["week"]) if package["week"] is not None else _requests_week()
    if index == 0:
        week = int(week * float(package["old_30d"]))
    elif index == 1:
        week = int(week * float(package["old_7d"]))
    return {
        "name": package["name"],
        "src": package["src"],
        "ver": package["ver"],
        "date": package["date"],
        # age_days в демо не пересчитывается по календарю: дата релиза в
        # таблице — снимок, а не «сегодня минус дата».
        "age_days": 12,
        "summary": package["summary"],
        "home": package["home"],
        "flag": bool(package["flag"]),
        "mcp": bool(package["flag"]),
        "month": int(week * 4.3),
        "week": week,
        "verdict": "ЖИВОЙ",
    }


def _iso(moment: datetime) -> str:
    """ISO 8601 с ``Z`` — формат ``measured_at``/``at`` в БД (как ``utcnow_iso``)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _demo_gh_result(argv: list[str], snapshot: bytes) -> "subprocess.CompletedProcess[str]":
    """Ответ демо-``gh``: снимок поиска на странице 1, пустая страница дальше."""
    argv = [str(item) for item in argv]
    endpoint = next((item for item in argv if "/" in item and not item.startswith("-")), "")
    page = 1
    for index, item in enumerate(argv):
        if item in ("-f", "--raw-field") and index + 1 < len(argv):
            key, _, value = argv[index + 1].partition("=")
            if key == "page" and value.isdigit():
                page = int(value)
    if endpoint == "search/repositories" and page == 1:
        return subprocess.CompletedProcess(argv, 0, snapshot.decode("utf-8"), "")
    if endpoint == "search/repositories":
        return subprocess.CompletedProcess(argv, 0, '{"total_count": 21, "items": []}', "")
    # `gh auth status` и прочее: «всё в порядке», сеть не нужна.
    return subprocess.CompletedProcess(argv, 0, "", "")
