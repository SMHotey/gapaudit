"""Тесты core/scan.py — обход реестра по курсору: чекпоинт, отмена, продолжение (шаг 1e).

Сети нет: транспорт подменён фейком по карте URL -> (статус, тело), пауза между
страницами — счётчик вместо ``time.sleep``. Все страницы взяты из фикстур 1d
(``servers_page1.json``/``servers_page2.json``); третьей страницы нет намеренно —
недостающая страница и есть «сеть отвалилась».
"""

from __future__ import annotations

import json
import pathlib
import threading
from collections import Counter

from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.mcp_registry import MCP_HOST, MCP_LIST_URL, next_url
from core.scan import (
    DEFAULT_BASE_URL,
    DEFAULT_CLASSES,
    SCAN_KINDS,
    ScanConfig,
    ScanState,
    scan,
    update_state,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "mcp_registry"
LIMIT = 10


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


PAGE1 = fixture("servers_page1.json")
PAGE2 = fixture("servers_page2.json")
PAGE1_CURSOR = PAGE1["metadata"]["nextCursor"]
PAGE2_ITEMS = PAGE2["servers"]
PAGE1_NAMES = [item["server"].get("name") for item in PAGE1["servers"]]
PAGE2_NAMES = [item["server"].get("name") for item in PAGE2_ITEMS]


def body(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def url(cursor: str | None) -> str:
    return next_url(MCP_LIST_URL.format(limit=LIMIT), cursor, LIMIT)


def last_page() -> dict:
    """Последняя страница каталога: серверы как в фикстуре, но курсора уже нет."""
    return {"servers": PAGE2_ITEMS, "metadata": {"count": len(PAGE2_ITEMS)}}


class FakeTransport:
    """Транспорт по карте {url: (статус, тело)}; неизвестный URL — 404."""

    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url_: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url_)
        return self.mapping.get(url_, (404, b'{"message": "Not Found"}'))


def make_client(mapping: dict[str, tuple[int, bytes]] | None = None) -> tuple[HttpClient, FakeTransport]:
    transport = FakeTransport(mapping or {})
    http = HttpClient(
        cache=DictCache(),
        throttle=Throttle(per_minute={MCP_HOST: 60000}),
        transport=transport,
        sleep=lambda _seconds: None,
    )
    return http, transport


class CountingSleep:
    """Заглушка паузы: помнит, сколько раз и с каким интервалом её просили."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def config(**kwargs) -> ScanConfig:
    params = {"limit": LIMIT, "sleep_between_pages": 0.0}
    params.update(kwargs)
    return ScanConfig(**params)


# --- обход: две страницы -------------------------------------------------------------------


def test_scan_two_pages_finishes_and_counts_everything() -> None:
    http, transport = make_client({url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(last_page()))})
    progress: list[int] = []

    state = scan(http, config=config(), on_progress=lambda s: progress.append(s.pages))

    assert state.pages == 2
    assert state.finished is True
    assert state.cursor is None
    assert state.entries == len(PAGE1["servers"]) + len(PAGE2_ITEMS) == 20
    assert state.names == set(PAGE1_NAMES) | set(PAGE2_NAMES)
    assert len(state.names) == len(set(PAGE1_NAMES + PAGE2_NAMES))
    assert transport.calls == [url(None), url(PAGE1_CURSOR)]
    assert progress == [1, 2]  # прогресс — после каждой страницы
    # Неймспейсы — префикс до первого слэша, как в legacy.
    assert state.namespaces == dict(Counter(name.split("/")[0] for name in PAGE1_NAMES + PAGE2_NAMES))
    # Нули по всем классам на месте: в сводке перечислены все ярлыки, а не только найденные.
    assert set(state.counts) == {label for _, label in DEFAULT_CLASSES}
    assert all(count >= 0 for count in state.counts.values())
    # В фикстурах у всех серверов есть remotes и нет packages: поставка — HTTP-сервер.
    assert state.kinds == {"remote": 20, "package": 0, "both": 0, "none": 0}
    assert state.elapsed >= 0.0 and state.started_at > 0.0


def test_scan_empty_catalog_finishes() -> None:
    """Пустой каталог: одна пустая страница без курсора — обход закончен, записей нет."""
    empty = {"servers": [], "metadata": {"count": 0}}
    http, _ = make_client({url(None): (200, body(empty))})

    state = scan(http, config=config())

    assert state.pages == 1
    assert state.entries == 0
    assert state.finished is True
    assert state.names == set()
    assert state.kinds == {kind: 0 for kind in SCAN_KINDS}


def test_scan_stops_at_max_pages() -> None:
    http, _ = make_client({url(None): (200, body(PAGE1))})

    state = scan(http, config=config(max_pages=1))

    assert state.pages == 1
    assert state.finished is False
    assert state.cursor == PAGE1_CURSOR


def test_scan_uses_config_base_url() -> None:
    """``base_url`` из ScanConfig доходит до запроса — ``--base-url`` больше не декоративный (шаг 1g)."""
    custom = "https://x.example/y?limit={limit}"
    first = "https://x.example/y?limit=10"
    second = next_url(first, PAGE1_CURSOR, LIMIT)
    http, transport = make_client({first: (200, body(PAGE1)), second: (200, body(last_page()))})

    state = scan(http, config=config(base_url=custom))

    assert state.finished is True
    assert state.entries == 20
    assert transport.calls == [first, second]  # обе страницы ушли на кастомный URL


# --- отмена --------------------------------------------------------------------------------


def test_scan_cancelled_before_first_page_does_not_touch_http() -> None:
    http, transport = make_client({url(None): (200, body(PAGE1))})
    cancel = threading.Event()
    cancel.set()

    state = scan(http, config=config(), cancel=cancel)

    assert state.pages == 0
    assert state.finished is False
    assert transport.calls == []  # ни одного запроса


def test_scan_cancelled_after_first_page_keeps_cursor() -> None:
    http, transport = make_client({url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(last_page()))})
    cancel = threading.Event()

    def stop_after_first_page(state: ScanState) -> None:
        if state.pages == 1:
            cancel.set()

    state = scan(http, config=config(), on_progress=stop_after_first_page, cancel=cancel)

    assert state.pages == 1
    assert state.finished is False
    assert state.cursor == PAGE1_CURSOR
    assert transport.calls == [url(None)]  # вторую страницу не запрашивали


# --- чекпоинт ------------------------------------------------------------------------------


def test_scan_checkpoints_every_page_when_asked() -> None:
    """checkpoint_every=1 — чекпоинт после каждой страницы, включая последнюю.

    Порядок как в legacy: чекпоинт снимается до проверки «курсор кончился», поэтому
    последняя страница тоже даёт чекпоинт — уже с ``cursor=None``.
    """
    http, _ = make_client({url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(last_page()))})
    checkpoints: list[dict] = []

    state = scan(http, config=config(checkpoint_every=1), on_checkpoint=lambda s: checkpoints.append(s.as_checkpoint()))

    assert len(checkpoints) == 2
    assert [item["pages"] for item in checkpoints] == [1, 2]
    assert checkpoints[0]["cursor"] == PAGE1_CURSOR
    assert checkpoints[1]["cursor"] is None
    assert state.finished is True


def test_scan_checkpoints_every_n_pages() -> None:
    http, _ = make_client({url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(last_page()))})
    every_two: list[int] = []
    never: list[int] = []

    scan(http, config=config(checkpoint_every=2), on_checkpoint=lambda s: every_two.append(s.pages))
    scan(http, config=config(checkpoint_every=25), on_checkpoint=lambda s: never.append(s.pages))

    assert every_two == [2]  # 25 страниц по умолчанию: на двух страницах чекпоинта нет
    assert never == []


def test_checkpoint_round_trip_through_json() -> None:
    http, _ = make_client({url(None): (200, body(PAGE1))})
    state = scan(http, config=config(max_pages=1))
    state.counts["kubernetes"] = 3

    restored = ScanState.from_checkpoint(json.loads(json.dumps(state.as_checkpoint())))

    assert restored.base_url == state.base_url == DEFAULT_BASE_URL
    assert restored.cursor == PAGE1_CURSOR
    assert restored.pages == 1
    assert restored.entries == len(PAGE1_NAMES)
    assert restored.names == set(PAGE1_NAMES)
    assert isinstance(restored.names, set)
    assert restored.counts["kubernetes"] == 3
    assert restored.kinds == state.kinds
    assert restored.namespaces == state.namespaces
    assert restored.finished is False
    assert restored.elapsed == state.elapsed


def test_from_checkpoint_accepts_legacy_shape_and_garbage() -> None:
    """Старый чекпоинт legacy (``ns`` вместо ``namespaces``) читается; мусор — дефолты."""
    legacy_shaped = {
        "cursor": "abc",
        "entries": 5,
        "pages": 2,
        "names": ["a/b"],
        "counts": {"kubernetes": 1},
        "kinds": {"remote": 5},
        "ns": {"a": 4},
    }

    restored = ScanState.from_checkpoint(legacy_shaped)

    assert restored.cursor == "abc"
    assert restored.entries == 5 and restored.pages == 2
    assert restored.names == {"a/b"}
    assert restored.namespaces == {"a": 4}
    assert restored.kinds["remote"] == 5 and restored.kinds["none"] == 0
    assert restored.base_url == DEFAULT_BASE_URL
    assert ScanState.from_checkpoint(None) == ScanState(base_url=DEFAULT_BASE_URL)
    assert ScanState.from_checkpoint({"names": "мусор", "counts": 5}).names == set()


# --- продолжение обхода ---------------------------------------------------------------------


def test_scan_stops_on_network_error_and_keeps_cursor() -> None:
    """Второй страницы нет (404) — обход встаёт, курсор первой страницы сохранён."""
    http, _ = make_client({url(None): (200, body(PAGE1))})

    state = scan(http, config=config())

    assert state.pages == 1
    assert state.entries == len(PAGE1_NAMES)
    assert state.cursor == PAGE1_CURSOR  # с этого курсора продолжим
    assert state.finished is False


def test_scan_continues_from_saved_state_without_losing_data() -> None:
    """Прерванный обход продолжается тем же состоянием: счётчики накапливаются."""
    first, _ = make_client({url(None): (200, body(PAGE1))})
    state = scan(first, config=config())
    names_after_first = set(state.names)

    second, second_transport = make_client({url(PAGE1_CURSOR): (200, body(last_page()))})
    resumed = scan(second, config=config(), state=state)

    assert resumed is state  # состояние обновляется на месте
    assert resumed.pages == 2
    assert resumed.entries == 20
    assert resumed.names == names_after_first | set(PAGE2_NAMES)
    assert resumed.finished is True
    assert second_transport.calls == [url(PAGE1_CURSOR)]  # первая страница не перезапрашивалась


def test_scan_resumes_from_restored_checkpoint() -> None:
    first, _ = make_client({url(None): (200, body(PAGE1))})
    saved = json.loads(json.dumps(scan(first, config=config()).as_checkpoint()))

    second, _ = make_client({url(PAGE1_CURSOR): (200, body(last_page()))})
    resumed = scan(second, config=config(), state=ScanState.from_checkpoint(saved))

    assert resumed.entries == 20
    assert resumed.pages == 2
    assert resumed.finished is True


# --- пауза, колбэки, чистота ----------------------------------------------------------------


def test_sleep_is_not_called_when_zero() -> None:
    sleeper = CountingSleep()
    http, _ = make_client({url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(last_page()))})

    scan(http, config=config(sleep_between_pages=0.0), sleep=sleeper)

    assert sleeper.delays == []


def test_sleep_is_called_between_pages_only() -> None:
    """Пауза — только между страницами: после последней спать нечего."""
    sleeper = CountingSleep()
    http, _ = make_client({url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(last_page()))})

    scan(http, config=config(sleep_between_pages=0.25), sleep=sleeper)

    assert sleeper.delays == [0.25]


def test_scan_without_callbacks_and_without_sleep_function() -> None:
    http, _ = make_client({url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(last_page()))})

    state = scan(http, config=config(), sleep=None, on_progress=None, on_checkpoint=None)

    assert state.finished is True and state.entries == 20


def test_scan_writes_no_files(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Обход ничего не пишет сам: чекпоинт и JSON — дело CLI/UI."""
    monkeypatch.chdir(tmp_path)
    http, _ = make_client({url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(last_page()))})

    scan(http, config=config(checkpoint_every=1), on_checkpoint=lambda _state: None)

    assert list(tmp_path.iterdir()) == []


# --- сводка ---------------------------------------------------------------------------------


def test_summary_has_legacy_final_keys() -> None:
    """Ключей девять, как в legacy-финале ``registry_full_scan.json``.

    В задании шага сказано «8 ключей», но legacy пишет именно девять
    (``entries``, ``pages``, ``unique_names``, ``namespaces``, ``top_namespaces``,
    ``class_counts``, ``delivery_kinds``, ``seconds``, ``finished``) — сохранён legacy.
    """
    http, _ = make_client({url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(last_page()))})
    state = scan(http, config=config())

    summary = state.summary()

    assert set(summary) == {
        "entries",
        "pages",
        "unique_names",
        "namespaces",
        "top_namespaces",
        "class_counts",
        "delivery_kinds",
        "seconds",
        "finished",
    }
    assert summary["entries"] == 20
    assert summary["pages"] == 2
    assert summary["unique_names"] == len(state.names)
    assert summary["namespaces"] == len(state.namespaces)
    assert summary["finished"] is True
    assert isinstance(summary["seconds"], int)
    assert summary["top_namespaces"] == sorted(
        state.namespaces.items(), key=lambda item: -item[1]
    )[:20]
    assert json.dumps(summary)  # сводка сериализуется в JSON как есть


def test_summary_top_namespaces_limited_to_twenty() -> None:
    state = ScanState(base_url=DEFAULT_BASE_URL, namespaces={f"ns{index}": index + 1 for index in range(30)})

    top = state.summary()["top_namespaces"]

    assert len(top) == 20
    assert top[0] == ("ns29", 30)


# --- update_state --------------------------------------------------------------------------


def test_update_state_counts_classes_namespaces_and_kinds() -> None:
    state = ScanState(base_url=DEFAULT_BASE_URL)
    servers = [
        {"name": "owner/kube-agent", "description": "Runs Kubernetes jobs", "remotes": [{"url": "http://x"}]},
        {"name": "owner/pdf-tool", "title": "PDF", "packages": [{"registry": "npm"}]},
        {"name": "owner/both", "remotes": [{"url": "x"}], "packages": [{"registry": "npm"}]},
        {"name": "noplace", "description": "ничего знакомого"},
    ]

    update_state(state, servers, DEFAULT_CLASSES)

    assert state.entries == 4
    assert state.names == {"owner/kube-agent", "owner/pdf-tool", "owner/both", "noplace"}
    assert state.namespaces == {"owner": 3}
    assert state.counts["kubernetes"] == 1
    assert state.counts["PDF/office"] == 1
    assert state.counts.get("memory", 0) == 0  # нули по классам ставит scan(), не update_state
    assert state.kinds == {"remote": 1, "package": 1, "both": 1, "none": 1}


def test_update_state_matches_substring_case_insensitively_in_any_field() -> None:
    """Подстрока ищется по имени, заголовку и описанию в нижнем регистре (как в legacy)."""
    state = ScanState(base_url=DEFAULT_BASE_URL)
    servers = [
        {"name": "a/b", "description": "POSTMORTEM templates"},
        {"name": "a/c", "title": "Flaky test retrier"},
        {"name": "sql-server-client", "description": ""},
    ]

    update_state(state, servers, DEFAULT_CLASSES)

    assert state.counts["постмортемы"] == 1
    assert state.counts["флаки-тесты"] == 1
    assert state.counts["SQL/аналитика"] == 1  # подстрока в самом имени
    assert state.namespaces == {"a": 2}  # имя без слэша неймспейса не даёт


def test_update_state_accepts_response_shape_and_garbage() -> None:
    """Принимаются и объекты сервера, и записи в форме ответа; мусор не роняет."""
    state = ScanState(base_url=DEFAULT_BASE_URL)
    items = [
        {"server": {"name": "a/one", "remotes": [1]}},
        "мусор",
        {},
    ]

    update_state(state, items, DEFAULT_CLASSES)

    assert state.entries == 3
    assert state.names == {"a/one", ""}  # причуда legacy: сервер без имени даёт пустую строку
    assert state.kinds == {"remote": 1, "package": 0, "both": 0, "none": 2}
    update_state(state, None, DEFAULT_CLASSES)
    assert state.entries == 3
