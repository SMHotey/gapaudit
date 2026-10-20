"""``core/collectors/funding.py`` (шаг P5c): раунды формы D из SEC EDGAR.

Сеть — ноль: транспорт — фейк (счётчик на каждый вызов, статус ошибки по
запросу), сон — фейк (копит запрошенные задержки). User-Agent приходит
параметром, ``api_keys.json`` модуль не читает (его читает ``cli/signals.py``).
Фикстура ``tests/fixtures/edgar/search_form_d.json`` синтетическая в формате
реального ответа (``hits.total.value`` — проверено живым запросом 20.10.2026;
снять реальную — follow-up, не блокер).

Формула: ``trend = (count(последний квартал) / count(первый квартал) − 1) · 100``
по окну последних **завершённых** кварталов. Нет базы (0 в первом квартале),
не собран один из концов окна или меньше двух успешных кварталов — ``None``.
Три состояния graceful degradation не смешиваются: нет User-Agent —
``configured=False`` без единого запроса; User-Agent есть и упали все
кварталы — ``error='all requests failed'``.
"""

from __future__ import annotations

import importlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from core.collectors import funding as funding_module
from core.collectors.funding import (
    ALL_FAILED,
    EDGAR_RATE_LIMIT_SEC,
    EDGAR_URL,
    FundingSignal,
    _build_url,
    _parse_hits_total,
    _quarter_dates,
    _quarter_window,
    compute_funding,
)

FIXTURE = Path(__file__).parent / "fixtures" / "edgar" / "search_form_d.json"
UA = "GapAudit test@example.com"


class FakeTransport:
    """Транспорт-фейк: счётчик на вызов (``None`` — статус ошибки).

    Копит URL и заголовки: тесты проверяют, что запрос ушёл на EDGAR с
    ``User-Agent`` и нужными датами квартала, не выходя в сеть.
    """

    def __init__(self, counts: list[int | None], status: int = 200) -> None:
        self.counts = counts
        self.status = status
        self.calls: list[str] = []
        self.headers: list[dict] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        self.headers.append(dict(headers))
        self.timeouts.append(timeout)
        index = len(self.calls) - 1
        count = self.counts[index] if index < len(self.counts) else None
        if count is None:
            return self.status if self.status != 200 else 500, b""
        body = {"hits": {"total": {"value": count, "relation": "eq"}, "hits": []}}
        return 200, json.dumps(body).encode("utf-8")


class FakeSleep:
    """Фейковый ``time.sleep``: копит запрошенные задержки, не ждёт."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def run(
    counts: list[int | None],
    *,
    user_agent: str | None = UA,
    quarters_back: int = 4,
    on_progress: Any = None,
) -> tuple[FundingSignal, FakeTransport, FakeSleep]:
    """Собрать сигнал на фейках: (результат, транспорт, сон)."""
    transport = FakeTransport(counts)
    sleep = FakeSleep()
    signal = compute_funding(
        "rust",
        user_agent=user_agent,
        transport=transport,
        quarters_back=quarters_back,
        sleep=sleep,
        on_progress=on_progress,
    )
    return signal, transport, sleep


# --- graceful degradation: нет User-Agent -------------------------------------


def test_no_user_agent_is_not_configured_and_transport_is_not_called() -> None:
    """Без User-Agent — ``configured=False``, пусто, ни одного запроса (§0.7)."""
    signal, transport, sleep = run([100, 100, 100, 100], user_agent=None)

    assert signal.configured is False
    assert signal.error is None
    assert signal.total == 0
    assert signal.by_quarter == ()
    assert signal.trend_pct is None
    assert transport.calls == []
    assert sleep.delays == []


def test_empty_string_user_agent_is_also_not_configured() -> None:
    """Пустая строка и пробелы — тот же «не настроен», а не пустой заголовок."""
    for value in ("", "   "):
        signal, transport, _sleep = run([100], user_agent=value)

        assert signal.configured is False
        assert signal.by_quarter == ()
        assert transport.calls == []


# --- тренд по кварталам -------------------------------------------------------


def test_equal_counts_give_zero_trend_and_four_buckets() -> None:
    """100 во всех четырёх кварталах: total=400, тренд 0 %."""
    signal, transport, _sleep = run([100, 100, 100, 100])

    assert signal.configured is True
    assert signal.error is None
    assert signal.total == 400
    assert signal.trend_pct == pytest.approx(0.0)
    assert len(signal.by_quarter) == 4
    assert len(transport.calls) == 4


def test_rising_counts_give_trend_300_pct() -> None:
    """50 -> 200 за окно: (200/50 − 1) · 100 = 300 %."""
    signal, _transport, _sleep = run([50, 100, 150, 200])

    assert signal.total == 500
    assert signal.trend_pct == pytest.approx(300.0)
    assert [row.count for row in signal.by_quarter] == [50, 100, 150, 200]


def test_zero_base_quarter_gives_no_trend() -> None:
    """База 0 — делить не на что: ``trend_pct=None``, данные сохранены."""
    signal, _transport, _sleep = run([0, 50, 100, 200])

    assert signal.total == 350
    assert signal.trend_pct is None
    assert [row.count for row in signal.by_quarter] == [0, 50, 100, 200]


def test_single_successful_quarter_gives_no_trend() -> None:
    """Один успешный квартал: ``total`` по нему, ``error=None``, тренда нет."""
    signal, _transport, _sleep = run([500, None, None, None])

    assert signal.configured is True
    assert signal.error is None
    assert signal.total == 500
    assert len(signal.by_quarter) == 1
    assert signal.trend_pct is None


def test_failed_last_quarter_keeps_data_without_trend() -> None:
    """Последний квартал окна не собрался — тренда нет, остальное на месте."""
    signal, _transport, _sleep = run([50, 100, 150, None])

    assert signal.error is None
    assert signal.total == 300
    assert signal.trend_pct is None


def test_all_quarters_failed_is_error_not_not_configured() -> None:
    """User-Agent есть, данных нет — «ошибка», а не «не настроен»."""
    signal, transport, _sleep = run([None, None, None, None])

    assert signal.configured is True
    assert signal.error == ALL_FAILED
    assert signal.total == 0
    assert signal.by_quarter == ()
    assert len(transport.calls) == 4


def test_transport_exception_on_one_quarter_is_skipped() -> None:
    """Исключение транспорта на квартале — пропуск, а не падение сигнала."""
    calls: list[int] = []

    def flaky(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("сеть отвалилась")
        return 200, json.dumps({"hits": {"total": {"value": 10}}}).encode("utf-8")

    signal = compute_funding("rust", user_agent=UA, transport=flaky, sleep=FakeSleep())

    assert signal.error is None
    assert signal.total == 30
    assert len(signal.by_quarter) == 3


def test_empty_body_quarter_is_skipped() -> None:
    """`200` с ``{}`` — счётчика нет, квартал пропускается."""
    def empty(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        return 200, b"{}"

    signal = compute_funding("rust", user_agent=UA, transport=empty, sleep=FakeSleep())

    assert signal.total == 0
    assert signal.by_quarter == ()
    assert signal.error == ALL_FAILED


# --- прогресс, троттлинг, заголовки ------------------------------------------


def test_on_progress_reports_each_quarter_and_finishes_at_one() -> None:
    """Прогресс — ``quarters_back`` раз, последний 1.0, имена кварталов."""
    seen: list[tuple[float, str]] = []

    run([100, 100, 100, 100], on_progress=lambda value, message: seen.append((value, message)))

    assert len(seen) == 4
    assert seen[-1][0] == pytest.approx(1.0)
    assert all(message.startswith("funding Q") for _value, message in seen)


def test_sleep_is_called_between_requests_only() -> None:
    """Сон — между запросами: ``quarters_back - 1`` раз, 0.11 с (§14: 10/сек)."""
    _signal, transport, sleep = run([1, 2, 3, 4])

    assert len(transport.calls) == 4
    assert sleep.delays == [EDGAR_RATE_LIMIT_SEC] * 3


def test_request_carries_user_agent_and_quarter_dates() -> None:
    """Запрос уходит на EDGAR с ``User-Agent``, ``forms=D`` и датами квартала."""
    _signal, transport, _sleep = run([1, 2, 3, 4])
    quarter = _quarter_window(4)[0]
    start, end = _quarter_dates(*quarter)

    url = transport.calls[0]
    assert url.startswith(f"{EDGAR_URL}?")
    assert "forms=D" in url
    assert "dateRange=custom" in url
    assert f"startdt={start}" in url
    assert f"enddt={end}" in url
    assert "q=rust" in url
    assert transport.headers[0]["User-Agent"] == UA
    assert transport.headers[0]["Accept"] == "application/json"


# --- окно кварталов ----------------------------------------------------------


def test_window_is_last_completed_quarters_in_order() -> None:
    """Окно — завершённые кварталы по возрастанию, текущий не входит.

    Текущий квартал неполон: счётчик filing'ов в нём ниже, и тренд «последний
    к первому» показывал бы ложный спад (то же правило, что у окна лет в
    ``patents.py``, DEVPLAN §18.3).
    """
    # 15 февраля 2026 — идёт Q1 2026; последний завершённый — Q4 2025.
    window = _quarter_window(4, today=date(2026, 2, 15))

    assert window == [(2025, 1), (2025, 2), (2025, 3), (2025, 4)]


def test_window_crosses_year_boundary_and_handles_zero() -> None:
    """Граница года в арифметике не ломается; ``quarters_back<=0`` — пусто."""
    assert _quarter_window(2, today=date(2026, 1, 10)) == [(2025, 3), (2025, 4)]
    assert _quarter_window(0, today=date(2026, 1, 10)) == []


def test_quarter_dates_cover_whole_quarter() -> None:
    """Границы квартала включительно: первое и последнее число (високосный Q1)."""
    assert _quarter_dates(2024, 1) == ("2024-01-01", "2024-03-31")
    assert _quarter_dates(2026, 2) == ("2026-04-01", "2026-06-30")
    assert _quarter_dates(2026, 4) == ("2026-10-01", "2026-12-31")


def test_build_url_fills_query_form_and_quarter_range() -> None:
    """URL собирается целиком: q (URL-encoded), forms=D, обе даты квартала."""
    url = _build_url("rust lang", 2026, 3)

    assert url.startswith(f"{EDGAR_URL}?")
    assert "q=rust+lang" in url
    assert "forms=D" in url
    assert "dateRange=custom" in url
    assert "startdt=2026-07-01" in url
    assert "enddt=2026-09-30" in url


# --- парсер ответа -----------------------------------------------------------


def test_parse_hits_total_reads_elastic_total_value() -> None:
    """``hits.total.value`` — фактическая форма ответа EDGAR (проверено живым)."""
    assert _parse_hits_total(200, b'{"hits":{"total":{"value":42}}}') == 42


def test_parse_hits_total_accepts_plain_integer_total() -> None:
    """``hits.total`` целым числом — форма из спецификации шага P5c."""
    assert _parse_hits_total(200, b'{"hits":{"total":42}}') == 42


def test_parse_hits_total_rejects_empty_bool_and_garbage() -> None:
    """Пустое тело, ``bool``, отсутствующий ``hits``, битый JSON — ``None``."""
    assert _parse_hits_total(200, b"{}") is None
    assert _parse_hits_total(200, b'{"hits":{"total":{"value":true}}}') is None
    assert _parse_hits_total(200, b'{"hits":[]}') is None
    assert _parse_hits_total(200, b'{"hits":{"total":42}') is None
    assert _parse_hits_total(200, b"") is None


def test_parse_hits_total_rejects_error_statuses() -> None:
    """403 (нет User-Agent), 429 (лимит 10/сек), 5xx — ``None``, не ноль."""
    assert _parse_hits_total(403, b"") is None
    assert _parse_hits_total(429, b'{"hits":{"total":{"value":7}}}') is None
    assert _parse_hits_total(500, b"{}") is None


def test_fixture_response_parses_to_42() -> None:
    """Фикстура (формат реального ответа) читается тем же парсером."""
    body = FIXTURE.read_bytes()

    assert _parse_hits_total(200, body) == 42


# --- побочные эффекты импорта -------------------------------------------------


def test_import_creates_no_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Повторный импорт модуля не пишет файлов и не читает каталогов данных."""
    monkeypatch.chdir(tmp_path)

    importlib.reload(funding_module)

    assert list(tmp_path.iterdir()) == []
