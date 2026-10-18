"""Проверка кандидатов: ``check()``, ``check_many()``, ``similar_names()`` (шаг 1c).

Порт legacy ``check_predecessors.py``: те же эндпоинты и те же вердикты, но без ``print``,
без глобалов и без путей от cwd. Сеть и кэш — в :class:`core.registry.RegistryClient`
(он получает :class:`core.http.HttpClient` снаружи), пороги — в
:class:`core.diffing.VerdictConfig`, поэтому «ЖИВО» в таблице и «ЖИВО» в событии
сравнения прогонов считаются одинаково.

Что печатает legacy и чего здесь нет: таблицу и итоги печатает CLI-обёртка (шаг 1e),
а имена из файла возвращаются списком — с теми же правилами разбора.

Флаг «есть MCP-поверхность» считается по :data:`core.registry.PRED_KEYWORDS`. Метка
:attr:`PackageCheckConfig.keyword_label` на разбор не влияет: она нужна CLI для заголовка
колонки (legacy ``--keyword``), а ключ результата остаётся ``mcp``.
"""

from __future__ import annotations

import json
import pathlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from core.diffing import VerdictConfig, apply_stale, days_since, verdict
from core.registry import RegistryClient

__all__ = [
    "POPULAR_URL",
    "PackageCheckConfig",
    "VerdictConfig",
    "check",
    "check_many",
    "lev",
    "names_from_file",
    "popular_names",
    "similar_names",
    "verdict",
]

#: Топ PyPI по скачиваниям (hugovk) — источник «популярных имён» для similar_names().
POPULAR_URL = (
    "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.min.json"
)

#: Сколько первых совпадений отдаёт similar_names() — как в legacy.
SIMILAR_LIMIT = 15
#: Размер выборки npm search для similar_names() — как в legacy.
SIMILAR_NPM_SIZE = 25
#: Таймаут и число попыток для датасета hugovk — как в legacy (файл большой, 429 часты).
POPULAR_TIMEOUT = 40.0
POPULAR_TRIES = 2

#: Разбор строк файла зависимостей: имя пакета в начале строки (legacy-регекспы).
_TOML_NAME_RE = re.compile(r'^\s*["\']?([A-Za-z0-9_.\-]+)["\']?\s*[=>~!\[]')
_REQ_NAME_RE = re.compile(r"^([A-Za-z0-9_.\-]+)")


class Executor(Protocol):
    """Минимум от ``ThreadPoolExecutor``, нужный ``check_many()`` (в тестах — синхронный)."""

    def map(self, fn: Any, iterable: Iterable[str]) -> Iterable[dict]:  # noqa: A003
        ...


@dataclass(frozen=True)
class PackageCheckConfig:
    """Настройки проверки: пороги вердикта, метка признака и параллелизм."""

    verdict: VerdictConfig = VerdictConfig()
    #: Метка признака для колонки CLI (legacy ``--keyword``); разбор от неё не зависит.
    keyword_label: str = "MCP"
    #: Сколько имён проверять параллельно в check_many() (legacy WORKERS = 6).
    workers: int = 6


def check(
    name: str,
    client: RegistryClient,
    *,
    cfg: PackageCheckConfig = PackageCheckConfig(),
) -> dict:
    """Проверить пакет в PyPI/npm и вернуть наблюдение — порт legacy ``check()``.

    Поля: ``name``, ``src`` (``pypi``/``npm``/``-``), ``ver``, ``date``, ``age_days``,
    ``summary``, ``home``, ``flag`` (= ``mcp``: нашёлся ли признак ниши), ``mcp``,
    ``month``, ``week``, ``verdict``. Вердикт «ЗАСТОЙ» накладывается здесь же: он требует
    и adoption, и даты релиза, поэтому в :func:`core.diffing.verdict` его нет.

    Ничего не печатает и не пишет: файлы — забота CLI, БД — шага 2.
    """
    pypi_card = client.pypi(name)
    npm_card = client.npm(name)
    month, week = client.adoption(name)

    info = pypi_card or npm_card or {}
    age = days_since(info.get("date"))
    niche_flag = bool(
        (pypi_card or {}).get("has_mcp") or (npm_card or {}).get("has_mcp")
    )
    base_verdict = verdict(
        pypi_card is not None,
        npm_card is not None,
        week,
        month,
        cfg=cfg.verdict,
    )

    return {
        "name": name,
        "src": "pypi" if pypi_card else ("npm" if npm_card else "-"),
        "ver": info.get("ver"),
        "date": info.get("date"),
        "age_days": age,
        "summary": info.get("summary"),
        "home": info.get("home"),
        "flag": niche_flag,
        "mcp": niche_flag,
        "month": month,
        "week": week,
        "verdict": apply_stale(base_verdict, age, cfg=cfg.verdict),
    }


def check_many(
    names: list[str],
    client: RegistryClient,
    *,
    cfg: PackageCheckConfig = PackageCheckConfig(),
    executor: Executor | None = None,
) -> list[dict]:
    """Проверить много имён параллельно, порядок результатов — как у ``names``.

    ``executor`` инжектируется для тестов (синхронная реализация ``map``); без него
    создаётся :class:`ThreadPoolExecutor` на ``cfg.workers`` потоков и закрывается здесь же.
    Потоки безопасны: ``client`` не хранит состояния задачи, запись идёт в общий HTTP-кэш.
    """
    tasks = list(names)

    def run(item: str) -> dict:
        return check(item, client, cfg=cfg)

    if executor is None:
        with ThreadPoolExecutor(max_workers=max(1, int(cfg.workers))) as pool:
            return list(pool.map(run, tasks))
    return list(executor.map(run, tasks))


def names_from_file(path: str) -> list[str]:
    """Имена зависимостей из ``requirements.txt`` / ``package.json`` / ``*.toml``.

    Порт legacy ``names_from_file()``: без внешних парсеров, разбор построчный.
    Отличия: ошибка чтения не печатается (возврат ``[]`` — печатает CLI), а для
    ``package.json`` список дополнительно приводится к «уникальные и отсортированные»
    (legacy отдавал порядок файла; в CLI это компенсировалось общим
    ``sorted(dict.fromkeys(names))``, так что итог тот же).

    Legacy-причуды сохранены намеренно: в ``*.toml`` строки вида ``dependencies = [``
    и ``name = "x"`` тоже дают «имена» — иначе паритетный тест с legacy не сойдётся.
    """
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except Exception:
        return []

    if str(path).endswith("package.json"):
        try:
            data = json.loads(text)
            merged = (data.get("dependencies") or {}) | (data.get("devDependencies") or {})
            return sorted({n for n in merged if not str(n).startswith("@types/")})
        except Exception:
            return []

    if str(path).endswith(".toml"):
        found = []
        for line in text.splitlines():
            stripped = line.strip()
            match = _TOML_NAME_RE.match(stripped)
            if match and not stripped.startswith("#"):
                found.append(match.group(1))
        return sorted(set(found))

    found = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ_NAME_RE.match(line)
        if match:
            found.append(match.group(1))
    return sorted(set(found))


def lev(a: str, b: str, cap: int = 3) -> int:
    """Расстояние Левенштейна с обрывом: больше ``cap`` неинтересно -> ``cap + 1``.

    Обрыв по длине и по минимуму строки делает проверку десятков тысяч имён дешёвой:
    точное расстояние нужно только для совсем близких опечаток.
    """
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        cur = [i]
        for j, char_b in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (char_a != char_b)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def popular_names(client: RegistryClient) -> list[str]:
    """Топ PyPI по скачиваниям (датасет hugovk); нет сети — пустой список.

    Кэшируется обычным HTTP-кэшем клиента (12 ч), поэтому повторный поиск похожих
    имён не тянет файл повторно.
    """
    status, data = client.http.get(POPULAR_URL, timeout=POPULAR_TIMEOUT, tries=POPULAR_TRIES)
    if status != 200 or not isinstance(data, dict):
        return []
    rows = data.get("rows")
    if not isinstance(rows, list):
        return []
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        project = row.get("project")
        if isinstance(project, str) and project:
            names.append(project)
    return names


def similar_names(
    name: str,
    client: RegistryClient,
    *,
    cap: int = 2,
    popular: list[str] | None = None,
) -> list[tuple[str, int]]:
    """Похожие имена среди популярных (PyPI) и в поиске npm: ``[(имя, расстояние), ...]``.

    Защита от типосквоттинга: имя, отличающееся на 1–2 символа от популярного пакета,
    почти наверняка ловушка. ``popular`` инжектируется тестами — без него список берётся
    из :func:`popular_names` (сеть через клиента). Сортировка: сначала меньшее расстояние,
    при равенстве — по имени; отдаём первые :data:`SIMILAR_LIMIT` совпадений.
    """
    candidates = popular if popular is not None else popular_names(client)
    lowered = name.lower()
    hits: dict[str, int] = {}

    def remember(candidate: str) -> None:
        if not candidate or candidate.lower() == lowered:
            return
        distance = lev(lowered, candidate.lower(), cap)
        if distance <= cap:
            hits[candidate] = min(distance, hits.get(candidate, 9))

    for candidate in candidates:
        remember(candidate)
    for candidate in client.npm_search(name, size=SIMILAR_NPM_SIZE):
        remember(candidate)

    return sorted(hits.items(), key=lambda item: (item[1], item[0]))[:SIMILAR_LIMIT]
