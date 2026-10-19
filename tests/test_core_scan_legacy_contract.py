"""Гейт шага 1e: счётчики ``core.scan`` один-в-один с legacy ``catalog_scan``.

Импортировать ``legacy/catalog_scan.py`` нельзя: скрипт выполняет обход на импорте
(сеть, ``raw/``, чекпоинт). Поэтому здесь:

* таблица классов вытаскивается разбором исходника (``ast``) — так проверяется, что
  :data:`core.scan.DEFAULT_CLASSES` совпадает с legacy дословно, включая порядок;
* тело цикла подсчёта скопировано в :func:`_legacy_scan` как есть (шаг 1e), и обход
  ``core.scan.scan()`` на тех же данных сверяется с ним по ``entries``,
  ``unique_names``, ``class_counts`` и ``delivery_kinds`` — четырём полям, ради
  которых модуль и существует.

Сеть не нужна: страница подаётся в обход через фиктивный транспорт.
"""

from __future__ import annotations

import ast
import json
import pathlib

from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.mcp_registry import MCP_HOST, MCP_LIST_URL, next_url
from core.scan import DEFAULT_CLASSES, ScanConfig, ScanState, scan, update_state

LEGACY_PATH = pathlib.Path(__file__).resolve().parent.parent / "legacy" / "catalog_scan.py"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "mcp_registry"
LIMIT = 10


def legacy_classes() -> list[tuple[str, str]]:
    """``CLASSES`` из legacy-скрипта, вытащенные разбором текста, а не импортом."""
    tree = ast.parse(LEGACY_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "CLASSES" for target in node.targets
        ):
            return [(str(needle), str(label)) for needle, label in ast.literal_eval(node.value)]
    raise AssertionError("CLASSES не найден в legacy/catalog_scan.py")


#: Тот же список, но в форме, которую понимают ``core.scan`` и ``core.mcp_registry``.
LEGACY_CLASSES: tuple[tuple[str, str], ...] = tuple(legacy_classes())


def _legacy_scan(items_list: list[dict]) -> dict:
    """Тело цикла legacy-скрипта: только подсчёт, без сети, печати и файлов.

    Скопировано из ``legacy/catalog_scan.py`` строка в строку (``for item in d["servers"]``
    разворачивается в тот же цикл по готовому списку); ``entries``/``names``/``counts``
    (с нулями по всем классам, как в legacy-инициализации)/``kinds``/``ns`` считаются
    ровно теми же выражениями.
    """
    entries, pages = 0, 0
    names, counts, kinds = set(), {label: 0 for _, label in LEGACY_CLASSES}, {}
    kinds = {"remote": 0, "package": 0, "both": 0, "none": 0}
    ns: dict[str, int] = {}

    for item in items_list:
        s = item.get("server") or {}
        entries += 1
        nm = s.get("name") or ""
        names.add(nm)
        if "/" in nm:
            nsp = nm.split("/")[0]
            ns[nsp] = ns.get(nsp, 0) + 1
        hay = " ".join([nm, s.get("title") or "", s.get("description") or ""]).lower()
        for needle, label in LEGACY_CLASSES:
            if needle in hay:
                counts[label] += 1
        has_r = bool(s.get("remotes"))
        has_p = bool(s.get("packages"))
        if has_r and has_p:
            kinds["both"] += 1
        elif has_r:
            kinds["remote"] += 1
        elif has_p:
            kinds["package"] += 1
        else:
            kinds["none"] += 1

    pages += 1
    return {
        "entries": entries,
        "pages": pages,
        "unique_names": len(names),
        "namespaces": len(ns),
        "class_counts": counts,
        "delivery_kinds": kinds,
    }


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_client(mapping: dict[str, tuple[int, bytes]]) -> HttpClient:
    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        return mapping.get(url, (404, b'{"message": "Not Found"}'))

    return HttpClient(
        cache=DictCache(),
        throttle=Throttle(per_minute={MCP_HOST: 60000}),
        transport=transport,
        sleep=lambda _seconds: None,
    )


def scan_one_page(items: list[dict], classes: tuple[tuple[str, str], ...]) -> ScanState:
    """Прогнать одну страницу (последнюю в каталоге) через настоящий ``scan()``."""
    url = next_url(MCP_LIST_URL.format(limit=LIMIT), None, LIMIT)
    payload = {"servers": items, "metadata": {"count": len(items)}}
    http = make_client({url: (200, json.dumps(payload).encode("utf-8"))})
    return scan(http, config=ScanConfig(limit=LIMIT, classes=classes, sleep_between_pages=0.0))


def test_default_classes_are_verbatim_from_legacy() -> None:
    """Список классов переписан из legacy без правок — включая порядок."""
    assert DEFAULT_CLASSES == LEGACY_CLASSES
    assert len(DEFAULT_CLASSES) == len(LEGACY_CLASSES) == 41


def test_counters_match_legacy_on_real_page() -> None:
    items = fixture("servers_page1.json")["servers"]

    state = scan_one_page(items, LEGACY_CLASSES)

    summary = state.summary()
    legacy = _legacy_scan(items)

    assert summary["entries"] == legacy["entries"] == len(items) == 10
    assert summary["unique_names"] == legacy["unique_names"]
    assert summary["class_counts"] == legacy["class_counts"]
    assert summary["delivery_kinds"] == legacy["delivery_kinds"]
    assert summary["namespaces"] == legacy["namespaces"]
    assert summary["pages"] == legacy["pages"] == 1


def test_counters_match_legacy_on_inconvenient_page() -> None:
    """Неудобная страница: без имени, только пакет, только remotes, ни того ни другого.

    Запись с ``server: null`` (мусор в ответе реестра) сюда входит намеренно, но её
    отсекает ``core.mcp_registry._servers`` до подсчёта — это отклонение от legacy,
    зафиксированное на шаге 1d (``tests/test_core_mcp_registry.py::test_list_servers_skips_broken_items``).
    Поэтому для legacy-сверки берутся только те записи, которые реально дошли до
    счётчиков, а разница показывается явно.
    """
    items = [
        {"server": {"name": "owner/kube-mcp", "title": "Kubernetes", "remotes": [{"url": "x"}]}},
        {"server": {"name": "owner/pdf", "description": "PDF и office", "packages": [{"registry": "npm"}]}},
        {"server": {"name": "owner/all", "remotes": [{}], "packages": [{}]}},
        {"server": {"name": "noslash", "description": "SQL/аналитика для монорепы"}},
        {"server": {"description": "сервер без имени"}},
        {"server": None},
    ]
    usable = [item for item in items if isinstance(item.get("server"), dict)]

    state = scan_one_page(items, LEGACY_CLASSES)

    summary = state.summary()
    legacy = _legacy_scan(usable)

    assert len(usable) == 5 and len(items) == 6
    assert _legacy_scan(items)["entries"] == 6  # legacy посчитал бы и мусор
    assert summary["entries"] == legacy["entries"] == 5
    # Пустое имя попадает в уникальные (причуда legacy) — паритет важнее «красоты».
    assert summary["unique_names"] == legacy["unique_names"] == 5
    assert summary["class_counts"] == legacy["class_counts"]
    assert summary["delivery_kinds"] == legacy["delivery_kinds"]
    assert summary["class_counts"]["SQL/аналитика"] == legacy["class_counts"]["SQL/аналитика"] >= 1


def test_update_state_matches_legacy_counting_step() -> None:
    """``update_state`` (без сеяния нулей) считает ровно те же попадания, что legacy."""
    items = [
        {"server": {"name": "a/b", "description": "PubMed и генерация тестов"}},
        {"server": {"name": "a/c", "description": "Flaky tests", "packages": [{}]}},
        {"server": {"name": "d", "title": "Observability", "remotes": [{}]}},
    ]
    state = ScanState(base_url="https://example.test")
    for _, label in LEGACY_CLASSES:
        state.counts.setdefault(label, 0)

    update_state(state, [item["server"] for item in items], LEGACY_CLASSES)

    legacy = _legacy_scan(items)
    assert state.counts == legacy["class_counts"]
    assert state.kinds == legacy["delivery_kinds"]
    assert state.namespaces == {"a": 2}
    assert state.entries == legacy["entries"]
