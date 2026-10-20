"""``core/collectors/patents.py`` (шаг P5b): патентный импульс PatentsView.

Сеть — ноль: транспорт — фейк, год читает из тела запроса, счётчик берёт
из своих данных (или отдаёт статус ошибки). Ключ приходит параметром,
``api_keys.json`` не читается. Фикстура ``tests/fixtures/patentsview/
search_q1.json`` синтетическая в формате реального ответа
(``error``/``count``/``total_hits``); снять реальную после получения ключа —
follow-up, не блокер.

Формула: ``impulse = (count(последний год) / count(базовый год) − 1) · 100``;
окно завершённых лет ``[current − years_back, current − 1]``. Нет базы (0 в
базовом году), не собран один из концов окна или меньше двух успешных лет —
``None``. Три состояния graceful degradation не смешиваются: нет ключа —
``configured=False`` без единого запроса; ключ есть и упали все годы —
``error='all requests failed'``.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from core.collectors import patents as patents_module
from core.collectors.patents import (
    ALL_FAILED,
    PATENTSVIEW_URL,
    PatentsSignal,
    PatentsYearBucket,
    _build_query,
    _parse_patents_count,
    compute_patents,
)

FIXTURE = Path(__file__).parent / "fixtures" / "patentsview" / "search_q1.json"
KEY = "test-key-123"
CURRENT_YEAR = datetime.now(timezone.utc).year


def year_from_body(body: bytes | None) -> int:
    """Год из тела запроса: второй элемент ``_and`` — нижняя граница дат."""
    query = json.loads((body or b"{}").decode("utf-8"))
    return int(query["q"]["_and"][1]["_gte"]["patent_date"][:4])


class FakeTransport:
    """Фейковый ``PostTransport``: ``counts[год]`` -> ``total_hits`` в ответе.

    ``statuses[год]`` — HTTP-статус (по умолчанию 200), ``empty`` — годы, на
    которые отвечаем пустым объектом ``{}`` (счётчика нет — год пропускается),
    ``fixture_body`` — тело-фикстура для всех лет. Все вызовы пишутся в
    ``calls`` — по ним проверяются URL, заголовки и тело.
    """

    def __init__(
        self,
        counts: dict[int, int] | None = None,
        statuses: dict[int, int] | None = None,
        empty: tuple[int, ...] = (),
        fixture_body: bytes | None = None,
    ) -> None:
        self.counts = dict(counts or {})
        self.statuses = dict(statuses or {})
        self.empty = tuple(empty)
        self.fixture_body = fixture_body
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, headers: dict, timeout: float, body: bytes | None = None
    ) -> tuple[int, bytes]:
        query = json.loads((body or b"{}").decode("utf-8"))
        year = year_from_body(body)
        self.calls.append(
            {"url": url, "headers": dict(headers), "timeout": timeout,
             "year": year, "body": query}
        )
        if self.fixture_body is not None:
            return 200, self.fixture_body
        status = self.statuses.get(year, 200)
        if status != 200:
            return status, b""
        if year in self.empty:
            return 200, b"{}"
        payload = {
            "error": False,
            "count": 1,
            "total_hits": self.counts.get(year, 0),
            "patents": [],
        }
        return 200, json.dumps(payload).encode("utf-8")


def exploding_transport(url, headers, timeout, body=None):  # noqa: ANN001
    raise AssertionError("транспорт без ключа не должен дёргаться")


# --- graceful degradation -----------------------------------------------------


def test_no_key_is_not_configured_and_transport_is_not_called() -> None:
    """Нет ключа — configured=False, пустой результат, ни одного запроса."""
    signal = compute_patents("requests", api_key=None, transport=exploding_transport)

    assert signal.configured is False
    assert signal.total == 0
    assert signal.by_year == ()
    assert signal.impulse_pct is None
    assert signal.error is None  # «не настроен» — не ошибка


def test_empty_string_key_is_also_not_configured() -> None:
    """Пустая строка ключа — тоже «не настроен», а не запрос с пустым ключом."""
    signal = compute_patents("requests", api_key="", transport=exploding_transport)

    assert signal.configured is False


# --- формула импульса ---------------------------------------------------------


def test_equal_counts_give_zero_impulse_and_three_buckets() -> None:
    """100/100/100: total=300, импульс 0.0, три строки по возрастанию."""
    transport = FakeTransport(
        {CURRENT_YEAR - 3: 100, CURRENT_YEAR - 2: 100, CURRENT_YEAR - 1: 100}
    )

    signal = compute_patents("requests", api_key=KEY, transport=transport)

    assert signal.configured is True
    assert signal.error is None
    assert signal.total == 300
    assert signal.impulse_pct == 0.0
    assert [row.year for row in signal.by_year] == [
        CURRENT_YEAR - 3, CURRENT_YEAR - 2, CURRENT_YEAR - 1,
    ]
    assert all(isinstance(row, PatentsYearBucket) for row in signal.by_year)


def test_rising_counts_give_impulse_200_pct() -> None:
    """50 -> 100 -> 150: (150/50 − 1) · 100 = 200%."""
    transport = FakeTransport(
        {CURRENT_YEAR - 3: 50, CURRENT_YEAR - 2: 100, CURRENT_YEAR - 1: 150}
    )

    signal = compute_patents("requests", api_key=KEY, transport=transport)

    assert signal.impulse_pct == pytest.approx(200.0)
    assert signal.total == 300


def test_zero_base_year_gives_no_impulse() -> None:
    """База 0 патентов — делить не на что: импульс None, данные есть."""
    transport = FakeTransport(
        {CURRENT_YEAR - 3: 0, CURRENT_YEAR - 2: 50, CURRENT_YEAR - 1: 100}
    )

    signal = compute_patents("requests", api_key=KEY, transport=transport)

    assert signal.impulse_pct is None
    assert signal.total == 150
    assert signal.error is None


def test_one_year_window_gives_no_impulse() -> None:
    """years_back=1: один успешный год (< 2) — данных мало, импульса нет."""
    transport = FakeTransport({CURRENT_YEAR - 1: 42})

    signal = compute_patents(
        "requests", api_key=KEY, transport=transport, years_back=1
    )

    assert signal.total == 42
    assert signal.impulse_pct is None
    assert signal.error is None


# --- частичные сбои -----------------------------------------------------------


def test_failed_base_year_keeps_partial_data_without_impulse() -> None:
    """Базовый год ответил 500: error=None, total по успешным, импульса нет."""
    transport = FakeTransport(
        counts={CURRENT_YEAR - 2: 200, CURRENT_YEAR - 1: 200},
        statuses={CURRENT_YEAR - 3: 500},
    )

    signal = compute_patents("requests", api_key=KEY, transport=transport)

    assert signal.error is None
    assert signal.total == 400
    assert [row.year for row in signal.by_year] == [
        CURRENT_YEAR - 2, CURRENT_YEAR - 1,
    ]
    assert signal.impulse_pct is None  # базы нет


def test_failed_middle_year_does_not_break_impulse() -> None:
    """Средний год упал: оба конца окна собраны — импульс считается."""
    transport = FakeTransport(
        counts={CURRENT_YEAR - 3: 50, CURRENT_YEAR - 1: 150},
        statuses={CURRENT_YEAR - 2: 500},
    )

    signal = compute_patents("requests", api_key=KEY, transport=transport)

    assert signal.error is None
    assert signal.total == 200
    assert len(signal.by_year) == 2
    assert signal.impulse_pct == pytest.approx(200.0)


def test_all_years_failed_is_error_not_not_configured() -> None:
    """Ключ есть, все годы упали: error='all requests failed', configured=True."""
    transport = FakeTransport(
        statuses={
            CURRENT_YEAR - 3: 429, CURRENT_YEAR - 2: 500, CURRENT_YEAR - 1: 0,
        }
    )

    signal = compute_patents("requests", api_key=KEY, transport=transport)

    assert signal.configured is True
    assert signal.error == ALL_FAILED
    assert signal.total == 0
    assert signal.by_year == ()


def test_empty_body_year_is_skipped() -> None:
    """200 с пустым телом — счётчика нет, год пропускается, не падает."""
    transport = FakeTransport(
        counts={CURRENT_YEAR - 3: 100, CURRENT_YEAR - 1: 100},
        empty=(CURRENT_YEAR - 2,),
    )

    signal = compute_patents("requests", api_key=KEY, transport=transport)

    assert signal.error is None
    assert [row.year for row in signal.by_year] == [CURRENT_YEAR - 3, CURRENT_YEAR - 1]
    assert signal.total == 200


def test_all_empty_bodies_is_error() -> None:
    """Ни один год не дал счётчика — та же «все запросы упали», что и 5xx."""
    transport = FakeTransport(
        empty=(CURRENT_YEAR - 3, CURRENT_YEAR - 2, CURRENT_YEAR - 1)
    )

    signal = compute_patents("requests", api_key=KEY, transport=transport)

    assert signal.error == ALL_FAILED
    assert signal.configured is True


# --- контракт запроса ---------------------------------------------------------


def test_request_goes_to_search_patentsview_with_header_key() -> None:
    """POST на endpoint, ключ — заголовком X-Api-Key, в URL ключа нет."""
    transport = FakeTransport({CURRENT_YEAR - 1: 1})

    compute_patents("requests", api_key=KEY, transport=transport)

    assert len(transport.calls) == 3
    call = transport.calls[0]
    assert call["url"] == PATENTSVIEW_URL
    assert call["headers"]["X-Api-Key"] == KEY
    assert call["headers"]["Content-Type"] == "application/json"
    assert KEY not in call["url"]
    assert call["body"]["o"] == {"size": 1}


def test_window_covers_three_completed_years_in_order() -> None:
    """Окно [current−3, current−1]: ровно три запроса, годы по возрастанию."""
    transport = FakeTransport({})

    compute_patents("requests", api_key=KEY, transport=transport)

    assert [call["year"] for call in transport.calls] == [
        CURRENT_YEAR - 3, CURRENT_YEAR - 2, CURRENT_YEAR - 1,
    ]


def test_fixture_response_parses_to_42_per_year() -> None:
    """Фикстура PatentsView (реальный формат) читается транспортом: 42·3."""
    transport = FakeTransport(fixture_body=FIXTURE.read_bytes())

    signal = compute_patents("requests", api_key=KEY, transport=transport)

    assert signal.error is None
    assert signal.total == 126
    assert signal.impulse_pct == 0.0


def test_on_progress_reports_each_year_and_finishes_at_one() -> None:
    """on_progress — по строке на год, последний прогресс ровно 1.0."""
    seen: list[tuple[float, str]] = []
    transport = FakeTransport({})

    compute_patents(
        "requests",
        api_key=KEY,
        transport=transport,
        on_progress=lambda progress, message: seen.append((progress, message)),
    )

    assert len(seen) == 3
    assert seen[-1] == (1.0, f"patents {CURRENT_YEAR - 1}")
    assert seen[0][0] == pytest.approx(1 / 3)


# --- разбор ответа -------------------------------------------------------------


def test_parse_count_accepts_spec_and_real_field_names() -> None:
    """total_patent_count (спецификация шага) и total_hits (реальный API)."""
    assert _parse_patents_count(200, b'{"total_patent_count": 42}') == 42
    assert (
        _parse_patents_count(
            200, b'{"error": false, "count": 1, "total_hits": 7, "patents": []}'
        )
        == 7
    )


def test_parse_count_ignores_page_count_and_bool() -> None:
    """``count`` — размер страницы, не итог; ``true`` числом не считается."""
    assert _parse_patents_count(200, b'{"count": 1}') is None
    assert _parse_patents_count(200, b'{"total_hits": true}') is None


def test_parse_count_rejects_non_200_empty_and_garbage() -> None:
    """429/пусто/битое тело — None: непригодный ответ не должен стать нулём."""
    assert _parse_patents_count(429, b"") is None
    assert _parse_patents_count(500, b"{}") is None
    assert _parse_patents_count(200, b"") is None
    assert _parse_patents_count(200, b"not json") is None
    assert _parse_patents_count(200, b'{"total_hits": "7"}') is None
    assert _parse_patents_count(200, b"[1, 2]") is None


# --- тело запроса --------------------------------------------------------------


def test_build_query_fills_year_range_title_and_single_page() -> None:
    """Диапазон дат года, текстовый поиск по заголовку, одна запись ответа."""
    query = _build_query("requests", 2024)

    assert set(query) == {"q", "f", "o"}  # верхний уровень q — один ключ
    clauses = query["q"]["_and"]
    assert clauses[0] == {"_text_any": {"patent_title": "requests"}}
    assert clauses[1] == {"_gte": {"patent_date": "2024-01-01"}}
    assert clauses[2] == {"_lte": {"patent_date": "2024-12-31"}}
    assert query["f"] == ["patent_id"]
    # size — опция search.patentsview.org; per_page старого API сервер не читает
    assert query["o"]["size"] == 1


# --- побочные эффекты импорта ---------------------------------------------------


def test_import_creates_no_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Повторный импорт модуля не пишет файлов и не читает каталогов данных."""
    monkeypatch.chdir(tmp_path)

    importlib.reload(patents_module)

    assert list(tmp_path.iterdir()) == []
