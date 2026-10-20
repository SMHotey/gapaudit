"""``core/collectors/search_interest.py`` (шаг P5d): два источника в одном сигнале.

Сеть — ноль: транспорт — фейк, отвечающий телом из теста или фикстуры.
Ключи приходят параметрами, ``api_keys.json`` модуль не читает (его читает
``cli/signals.py``). Фикстуры синтетические в формате API:

* ``tests/fixtures/wordstat/top_requests.json`` — ``{"topRequests":
  [{"phrase", "count"}], "totalCount": N}`` (форма ответа ``/v1/topRequests``
  по документации Яндекса);
* ``tests/fixtures/apify/trends_dataset.json`` — датасет актора Apify
  ``[{"term", "interestOverTime", "interestByRegion"}]``.

Обе синтетические: реальный ответ — follow-up после получения ключей
(Wordstat — по заявке; Apify — платный), не блокер шага.

Проверяется: три состояния graceful degradation по каждому источнику
(нет ключа — ни одного запроса; часть упала — по успешным; всё упало —
``error='all requests failed'``), порядок точек, деление прогресса в
обёртке, парсеры и тренд по ряду.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from core.collectors import search_interest as si_module
from core.collectors.search_interest import (
    ALL_FAILED,
    APIFY_URL_TEMPLATE,
    WORDSTAT_REGIONS,
    WORDSTAT_URL,
    SearchInterestPoint,
    SearchInterestSource,
    _parse_apify,
    _parse_wordstat,
    _trend_from_series,
    compute_google_trends,
    compute_search_interest,
    compute_wordstat,
)

FIXTURES = Path(__file__).parent / "fixtures"
WORDSTAT_FIXTURE = FIXTURES / "wordstat" / "top_requests.json"
APIFY_FIXTURE = FIXTURES / "apify" / "trends_dataset.json"

TOKEN = "wordstat-token"
CLIENT_ID = "client-id"
APIFY_TOKEN = "apify-token"
ACTOR_ID = "apify/google-trends-scraper"


class FakeTransport:
    """Транспорт-фейк: ответ по номеру вызова, копит URL/заголовки/тела.

    ``responses`` — список ``(status, body)``; исчерпался — последний ответ
    повторяется. Это удобно для Wordstat (пять регионов одним телом) и для
    Apify (один запуск актора).
    """

    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.headers: list[dict] = []
        self.bodies: list[Any] = []
        self.timeouts: list[float] = []

    def __call__(
        self, url: str, headers: dict, timeout: float, body: bytes | None = None
    ) -> tuple[int, bytes]:
        self.calls.append(url)
        self.headers.append(dict(headers))
        self.timeouts.append(timeout)
        self.bodies.append(json.loads(body.decode("utf-8")) if body else None)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def wordstat_body(total: int = 1000) -> bytes:
    """Ответ ``/v1/topRequests`` с заданным ``totalCount``."""
    return json.dumps(
        {"topRequests": [{"phrase": "rust", "count": total}], "totalCount": total}
    ).encode("utf-8")


def run_wordstat(
    responses: list[tuple[int, bytes]] | None = None,
    *,
    token: str | None = TOKEN,
    client_id: str | None = CLIENT_ID,
    top_n: int = 5,
    regions: tuple[str, ...] | None = None,
    on_progress: Any = None,
) -> tuple[SearchInterestSource, FakeTransport]:
    """Собрать Wordstat на фейке: (источник, транспорт)."""
    transport = FakeTransport(responses or [(200, wordstat_body())])
    kwargs: dict[str, Any] = {}
    if regions is not None:
        kwargs["regions"] = regions
    source = compute_wordstat(
        "rust",
        token=token,
        client_id=client_id,
        transport=transport,
        top_n=top_n,
        on_progress=on_progress,
        **kwargs,
    )
    return source, transport


def run_google(
    responses: list[tuple[int, bytes]] | None = None,
    *,
    api_token: str | None = APIFY_TOKEN,
    actor_id: str | None = ACTOR_ID,
    on_progress: Any = None,
) -> tuple[SearchInterestSource, FakeTransport]:
    """Собрать Google Trends на фейке: (источник, транспорт)."""
    transport = FakeTransport(responses or [(200, APIFY_FIXTURE.read_bytes())])
    source = compute_google_trends(
        "rust",
        api_token=api_token,
        actor_id=actor_id,
        transport=transport,
        on_progress=on_progress,
    )
    return source, transport


# --- Wordstat: graceful degradation -------------------------------------------


def test_wordstat_without_credentials_is_not_configured_and_never_requests() -> None:
    """Без токена — ``configured=False``, пусто, транспорт не дёргается ни разу."""
    source, transport = run_wordstat(token=None)

    assert source.configured is False
    assert source.error is None
    assert source.points == ()
    assert source.total is None
    assert transport.calls == []


@pytest.mark.parametrize(
    ("token", "client_id"),
    [(TOKEN, None), (None, CLIENT_ID), (TOKEN, "   "), ("", CLIENT_ID)],
)
def test_wordstat_requires_both_token_and_client_id(
    token: str | None, client_id: str | None
) -> None:
    """Половина учётных данных — «не настроен», как у остальных провайдеров."""
    source, transport = run_wordstat(token=token, client_id=client_id)

    assert source.configured is False
    assert transport.calls == []


# --- Wordstat: сбор -----------------------------------------------------------


def test_wordstat_sums_total_count_over_regions() -> None:
    """Пять регионов по 1000 → total 5000 и пять точек (top_n=5)."""
    source, transport = run_wordstat([(200, wordstat_body(1000))])

    assert source.configured is True
    assert source.error is None
    assert source.total == 5000
    assert len(source.points) == 5
    assert len(transport.calls) == 5
    assert {point.value for point in source.points} == {1000}
    # имена регионов — человекочитаемые, а не идентификаторы Яндекс.Гео
    assert "Россия" in {point.region for point in source.points}


def test_wordstat_points_sorted_desc_and_cut_to_top_n() -> None:
    """``points`` — top-N по убыванию, ``total`` — по всем успешным регионам."""
    source, _ = run_wordstat(
        [
            (200, wordstat_body(100)),
            (200, wordstat_body(500)),
            (200, wordstat_body(300)),
        ],
        regions=("225", "1", "2"),
        top_n=2,
    )

    assert [point.value for point in source.points] == [500, 300]
    assert source.total == 900


def test_wordstat_partial_failure_keeps_successful_regions() -> None:
    """Один регион 429 — пропуск: ``error=None``, ``total`` по успешным (4000)."""
    source, _ = run_wordstat(
        [(200, wordstat_body(1000)), (200, wordstat_body(1000)),
         (429, b""), (200, wordstat_body(1000)), (200, wordstat_body(1000))]
    )

    assert source.configured is True
    assert source.error is None
    assert source.total == 4000
    assert len(source.points) == 4


def test_wordstat_all_failures_is_all_failed() -> None:
    """Все регионы 5xx — «ошибка», а не «не настроен»: ``configured=True``."""
    source, transport = run_wordstat([(500, b"")])

    assert source.configured is True
    assert source.error == ALL_FAILED
    assert source.total == 0
    assert source.points == ()
    assert len(transport.calls) == 5


def test_wordstat_empty_body_is_skipped() -> None:
    """``200 b'{}'`` — регион пропущен (данных нет), остальные считаются."""
    source, _ = run_wordstat(
        [(200, b"{}"), (200, wordstat_body(300))],
        regions=("225", "1"),
    )

    assert source.error is None
    assert source.total == 300
    assert [point.value for point in source.points] == [300]


def test_wordstat_zero_count_is_a_valid_point() -> None:
    """Явный ``totalCount: 0`` — точка с нулём, а не «регион пропущен»."""
    source, _ = run_wordstat([(200, wordstat_body(0))], regions=("225",))

    assert source.configured is True
    assert source.total == 0
    assert [point.value for point in source.points] == [0]


def test_wordstat_progress_per_region_and_ends_at_one() -> None:
    """``on_progress`` — по вызову на регион, последний 1.0."""
    seen: list[tuple[float, str]] = []
    source, _ = run_wordstat(
        [(200, wordstat_body())],
        on_progress=lambda fraction, message: seen.append((fraction, message)),
    )

    assert source.configured is True
    assert len(seen) == len(WORDSTAT_REGIONS)
    assert seen[-1][0] == pytest.approx(1.0)
    assert seen[-1][1].startswith("wordstat ")
    fractions = [fraction for fraction, _ in seen]
    assert fractions == sorted(fractions)


def test_wordstat_request_carries_bearer_token_and_regions_list() -> None:
    """Токен — заголовком, регион — СПИСКОМ (``regions``), как в документации."""
    _, transport = run_wordstat()

    assert transport.calls[0] == WORDSTAT_URL
    headers = transport.headers[0]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"
    body = transport.bodies[0]
    assert body["phrase"] == "rust"
    assert body["regions"] == [225]  # список чисел, а не строка
    assert body["devices"] == ["all"]


# --- Wordstat: парсер ---------------------------------------------------------


def test_parse_wordstat_reads_total_count() -> None:
    """``200`` с ``totalCount`` → словарь с числом и списком."""
    parsed = _parse_wordstat(
        200, b'{"totalCount": 42, "topRequests": [{"phrase": "test", "count": 42}]}'
    )

    assert parsed is not None
    assert parsed["totalCount"] == 42
    assert parsed["topRequests"] == [{"phrase": "test", "count": 42}]


def test_parse_wordstat_without_total_count_sums_top_requests() -> None:
    """Форма ответа без ``totalCount`` — считаем сумму ``count``."""
    parsed = _parse_wordstat(
        200, b'{"topRequests": [{"phrase": "a", "count": 2}, {"phrase": "b", "count": 3}]}'
    )

    assert parsed is not None
    assert parsed["totalCount"] == 5


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, b""),
        (403, b'{"totalCount": 7}'),
        (429, b'{"totalCount": 7}'),
        (500, b"{}"),
        (200, b"not json"),
        (200, b"{}"),
        (200, b"[]"),
        (200, b'{"totalCount": true}'),
    ],
)
def test_parse_wordstat_rejects_unusable_answers(status: int, body: bytes) -> None:
    """Всё, что не 200 с числом, — ``None`` (регион пропускается)."""
    assert _parse_wordstat(status, body) is None


def test_wordstat_fixture_parses() -> None:
    """Фикстура (формат ответа API) читается тем же парсером."""
    parsed = _parse_wordstat(200, WORDSTAT_FIXTURE.read_bytes())

    assert parsed is not None
    assert parsed["totalCount"] == 42


# --- Google Trends: graceful degradation --------------------------------------


@pytest.mark.parametrize(
    ("api_token", "actor_id"),
    [(None, ACTOR_ID), (APIFY_TOKEN, None), ("  ", ACTOR_ID)],
)
def test_google_trends_without_credentials_is_not_configured(
    api_token: str | None, actor_id: str | None
) -> None:
    """Нет токена или ``actor_id`` — «не настроен», запросов нет."""
    source, transport = run_google(api_token=api_token, actor_id=actor_id)

    assert source.configured is False
    assert source.error is None
    assert source.points == ()
    assert transport.calls == []


def test_google_trends_parses_dataset_points_total_and_trend() -> None:
    """Датасет актора: точки по регионам, сумма ряда и тренд за 12 мес."""
    source, transport = run_google()

    assert source.configured is True
    assert source.error is None
    assert source.source == "google_trends"
    # ряд фикстуры: 50 … 75 -> тренд +50.0%
    assert source.trend_pct == pytest.approx(50.0)
    assert [point.region for point in source.points][:3] == ["RU", "US", "DE"]
    assert [point.value for point in source.points][:3] == [100, 45, 20]
    # сумма ряда interestOverTime фикстуры (50+55+60+58+62+66+64+70+68+72+74+75)
    assert source.total == 774
    assert len(transport.calls) == 1


def test_google_trends_item_without_regions_has_empty_points() -> None:
    """Элемент без ``interestByRegion`` — точек нет, ряд всё равно читается."""
    payload = json.dumps(
        [{"term": "rust", "interestOverTime": [{"date": "2025-10", "value": 40},
                                               {"date": "2026-10", "value": 60}]}]
    ).encode("utf-8")
    source, _ = run_google([(200, payload)])

    assert source.configured is True
    assert source.error is None
    assert source.points == ()
    assert source.trend_pct == pytest.approx(50.0)
    assert source.total == 100


def test_google_trends_progress_is_called_once_with_one() -> None:
    """Запуск актора неделим: один вызов прогресса, 1.0."""
    seen: list[tuple[float, str]] = []
    run_google(on_progress=lambda fraction, message: seen.append((fraction, message)))

    assert seen == [(1.0, "google_trends")]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, b"[]"),
        (401, b""),
        (500, b""),
        (200, b'{"error": {"type": "invalid-input"}}'),
        (200, b"not json"),
    ],
)
def test_google_trends_unusable_answer_is_all_failed(status: int, body: bytes) -> None:
    """Пустой датасет/ошибка — ``error='all requests failed'``, ``configured=True``."""
    source, _ = run_google([(status, body)])

    assert source.configured is True
    assert source.error == ALL_FAILED
    assert source.points == ()
    assert source.trend_pct is None


def test_google_trends_url_has_actor_path_and_token() -> None:
    """URL: ``/acts/{actor_id}/run-sync-get-dataset-items?token=...``."""
    _, transport = run_google()

    url = transport.calls[0]
    assert url.startswith("https://api.apify.com/v2/acts/apify/google-trends-scraper/")
    assert url.endswith("run-sync-get-dataset-items?token=apify-token")
    assert APIFY_URL_TEMPLATE.split("{")[0] in url
    body = transport.bodies[0]
    assert body == {"searchTerms": ["rust"], "geo": "", "timeRange": "today 12-m"}


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, b""),
        (429, b"[]"),
        (200, b"{}"),
        (200, b'{"error": "nope"}'),
        (200, b"not json"),
    ],
)
def test_parse_apify_accepts_only_200_list(status: int, body: bytes) -> None:
    """Парсер датасета: только список на 200; объект с ошибкой — ``None``."""
    assert _parse_apify(status, body) is None


def test_apify_fixture_parses() -> None:
    """Фикстура (датасет актора) — список элементов."""
    items = _parse_apify(200, APIFY_FIXTURE.read_bytes())

    assert isinstance(items, list)
    assert items and items[0]["term"] == "test"


# --- тренд по ряду ------------------------------------------------------------


@pytest.mark.parametrize(
    ("series", "expected"),
    [
        ([100, 80], -20.0),
        ([100, 120], 20.0),
        ([50, 50], 0.0),
        ([0, 50], None),
        ([100], None),
        ([], None),
    ],
)
def test_trend_from_series(series: list[int | float], expected: float | None) -> None:
    """``(last − first) / first · 100``; < 2 точек или ноль в базе — ``None``."""
    assert _trend_from_series(series) == expected


# --- обёртка на два источника -------------------------------------------------


def test_compute_search_interest_collects_both_sources() -> None:
    """Оба набора ключей — собраны оба источника (не «или», а «и»)."""
    transport = FakeTransport([(200, wordstat_body(100)), (200, APIFY_FIXTURE.read_bytes())])

    signal = compute_search_interest(
        "rust",
        wordstat_token=TOKEN,
        wordstat_client_id=CLIENT_ID,
        apify_token=APIFY_TOKEN,
        apify_actor_id=ACTOR_ID,
        transport=transport,
    )
    payload = signal.as_dict()

    assert payload["query"] == "rust"
    assert payload["wordstat"]["configured"] is True
    assert payload["google_trends"]["configured"] is True
    assert payload["configured"] is True
    assert payload["total"] == payload["wordstat"]["total"] + payload["google_trends"]["total"]
    assert len(transport.calls) == len(WORDSTAT_REGIONS) + 1


def test_compute_search_interest_neither_configured() -> None:
    """Ни одного ключа — оба источника ``configured=False``, запросов нет."""
    transport = FakeTransport([(200, b"")])

    signal = compute_search_interest("rust", transport=transport)
    payload = signal.as_dict()

    assert payload["wordstat"]["configured"] is False
    assert payload["google_trends"]["configured"] is False
    # настройки нет ни у одного источника — общий configured тоже False
    assert payload["configured"] is False
    assert payload["total"] == 0
    assert transport.calls == []


def test_compute_search_interest_progress_split_between_sources() -> None:
    """Прогресс: Wordstat до 0.5, Google Trends — вторая половина до 1.0."""
    seen: list[float] = []
    transport = FakeTransport([(200, wordstat_body(100)), (200, APIFY_FIXTURE.read_bytes())])

    compute_search_interest(
        "rust",
        wordstat_token=TOKEN,
        wordstat_client_id=CLIENT_ID,
        apify_token=APIFY_TOKEN,
        apify_actor_id=ACTOR_ID,
        transport=transport,
        on_progress=lambda fraction, message: seen.append(fraction),
    )

    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(1.0)
    assert max(seen[: len(WORDSTAT_REGIONS)]) == pytest.approx(0.5)


def test_signal_dict_round_trip_has_both_blocks() -> None:
    """``as_dict`` источника: ровно те поля, которые читает web/UI (P5d)."""
    source, _ = run_wordstat([(200, wordstat_body(10))], regions=("225",))
    payload = source.as_dict()

    assert set(payload) == {
        "source", "points", "total", "trend_pct", "configured", "error",
    }
    assert payload["source"] == "wordstat"
    assert payload["points"] == [
        {"region": "Россия", "value": 10, "trend_pct": None}
    ]


def test_points_dataclass_carries_region_value_trend() -> None:
    """Точка — неизменяемая запись «регион → значение → тренд»."""
    point = SearchInterestPoint(region="RU", value=None, trend_pct=None)

    assert point.as_dict() == {"region": "RU", "value": None, "trend_pct": None}
    with pytest.raises(Exception):
        point.region = "US"  # type: ignore[misc]


# --- побочные эффекты импорта -------------------------------------------------


def test_import_creates_no_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Повторный импорт модуля не пишет файлов и не читает каталогов данных."""
    monkeypatch.chdir(tmp_path)

    importlib.reload(si_module)

    assert list(tmp_path.iterdir()) == []
