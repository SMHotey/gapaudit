"""``core/collectors.jobs`` (шаг P5a): клиент Adzuna с инжектируемым транспортом.

Три состояния graceful degradation — главный контракт модуля (§18.3):
«не настроен» (нет ключа, транспорт не дёргается), «собран» (часть стран
может упасть — итог по успешным) и «ошибка» (ключ есть, упали все страны).

Сеть — ноль, реальных ключей нет: транспорт фейковый, фикстура
``tests/fixtures/adzuna/search_germany.json`` синтетическая (снять
реальную после получения ключа — follow-up шага, не блокер).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from core.collectors import jobs as jobs_module
from core.collectors.jobs import (
    ADZUNA_COUNTRIES,
    ALL_FAILED,
    COUNTRY_LABELS,
    Transport,
    _parse_count,
    compute_jobs,
)

FIXTURE = Path(__file__).parent / "fixtures" / "adzuna" / "search_germany.json"
FIXTURE_BODY = FIXTURE.read_bytes()


def body(count: int) -> bytes:
    """Ответ Adzuna с заданным ``count`` (синтетический, как фикстура)."""
    return json.dumps({"count": count}).encode("utf-8")


def fixture_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    """Транспорт, отдающий фикстуру Adzuna на любой URL."""
    return 200, FIXTURE_BODY


def by_country_transport(counts: dict[str, tuple[int, bytes]]) -> Transport:
    """Транспорт по кодам стран: ``{'de': (200, body(200)), ...}``; прочее — 404."""

    def transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        for country, answer in counts.items():
            if f"/jobs/{country}/" in url:
                return answer
        return 404, b""

    return transport


def exploding_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    raise OSError("сеть недоступна")


# --- graceful degradation: три состояния --------------------------------------


def test_no_key_is_not_configured_and_never_calls_transport() -> None:
    """Нет app_id/app_key — configured=False, пусто, и НИ ОДНОГО запроса.

    Это и есть graceful degradation: «не настроен» — не ошибка, и ключ,
    которого нет, не должен приводить к обращению к Adzuna (мутационная
    проверка шага: убрана проверка configured — тест краснеет).
    """
    def must_not_be_called(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        raise AssertionError("транспорт не должен дёргаться без ключа")

    for app_id, app_key in ((None, None), ("", "k"), ("id", None), ("id", "")):
        signal = compute_jobs(
            "requests",
            app_id=app_id or None,
            app_key=app_key or None,
            transport=must_not_be_called,
        )
        assert signal.configured is False
        assert signal.total == 0
        assert signal.by_country == ()
        assert signal.error is None


def test_all_countries_ok_total_and_top5() -> None:
    """11 стран по 100 вакансий: total=1100, top-5, подписи стран из COUNTRY_LABELS."""
    transport = by_country_transport(
        {country: (200, body(100)) for country in ADZUNA_COUNTRIES}
    )

    signal = compute_jobs("rust", app_id="id", app_key="key", transport=transport)

    assert signal.configured is True
    assert signal.error is None
    assert signal.total == 100 * len(ADZUNA_COUNTRIES) == 1100
    assert len(signal.by_country) == 5
    assert all(row.count == 100 for row in signal.by_country)
    # tie-break по коду страны: при равных count порядок детерминирован
    assert [row.country for row in signal.by_country] == ["au", "br", "ca", "de", "es"]
    assert all(row.label == COUNTRY_LABELS[row.country] for row in signal.by_country)


def test_top_sorted_by_count_desc() -> None:
    """de=200, us=100, it=50 — по убыванию; упавшие страны в топ не попадают."""
    transport = by_country_transport(
        {"de": (200, body(200)), "us": (200, body(100)), "it": (200, body(50))}
    )

    signal = compute_jobs("rust", app_id="id", app_key="key", transport=transport)

    assert [(row.country, row.count) for row in signal.by_country] == [
        ("de", 200),
        ("us", 100),
        ("it", 50),
    ]
    assert signal.total == 350


def test_single_429_does_not_fail_the_signal() -> None:
    """Одна страна 429 (лимит 25/мин), остальные 200 — error=None, total по успешным."""
    counts = {country: (200, body(10)) for country in ADZUNA_COUNTRIES}
    counts["de"] = (429, b"")

    signal = compute_jobs("rust", app_id="id", app_key="key",
                          transport=by_country_transport(counts))

    assert signal.error is None
    assert signal.total == 10 * (len(ADZUNA_COUNTRIES) - 1)
    assert "de" not in {row.country for row in signal.by_country}


def test_all_countries_failed_is_error_not_not_configured() -> None:
    """Ключ есть, все страны 5xx — error='all requests failed', configured=True."""
    counts = {country: (503, b"") for country in ADZUNA_COUNTRIES}

    signal = compute_jobs("rust", app_id="id", app_key="key",
                          transport=by_country_transport(counts))

    assert signal.configured is True
    assert signal.error == ALL_FAILED
    assert signal.total == 0
    assert signal.by_country == ()


def test_all_transport_exceptions_is_error() -> None:
    """Сеть упала совсем (исключение на каждой стране) — та же «ошибка»."""
    signal = compute_jobs("rust", app_id="id", app_key="key",
                          transport=exploding_transport)

    assert signal.configured is True
    assert signal.error == ALL_FAILED
    assert signal.total == 0


# --- разбор ответа ------------------------------------------------------------


def test_empty_200_body_is_skipped_not_zero() -> None:
    """``200 {}`` без count — пропуск (не 0): пустой ответ не значит «нет вакансий»."""
    counts = {country: (200, b"{}") for country in ADZUNA_COUNTRIES}

    signal = compute_jobs("rust", app_id="id", app_key="key",
                          transport=by_country_transport(counts))

    # ни одного успешного count — это «все упали», а не «total=0»
    assert signal.error == ALL_FAILED
    assert signal.total == 0


def test_zero_count_country_excluded_from_top_but_not_an_error() -> None:
    """Страна с count=0 — успех (в топ не попадает, в total входит как 0)."""
    transport = by_country_transport({"de": (200, body(0)), "us": (200, body(7))})

    signal = compute_jobs("rust", app_id="id", app_key="key", transport=transport)

    assert signal.error is None
    assert signal.total == 7
    assert [row.country for row in signal.by_country] == ["us"]


def test_top_n_is_respected() -> None:
    """top_n=2 — в by_country ровно две первые страны."""
    transport = by_country_transport(
        {"de": (200, body(300)), "us": (200, body(200)), "it": (200, body(100))}
    )

    signal = compute_jobs("rust", app_id="id", app_key="key",
                          transport=transport, top_n=2)

    assert [row.country for row in signal.by_country] == ["de", "us"]


def test_unknown_country_uses_code_as_label() -> None:
    """Незнакомый код страны подписывается кодом, а не падает KeyError."""
    transport = by_country_transport({"xx": (200, body(5))})

    signal = compute_jobs("rust", app_id="id", app_key="key",
                          transport=transport, countries=("xx",))

    assert signal.by_country[0].label == "xx"


def test_progress_is_reported_per_country() -> None:
    """on_progress: по одному вызову на страну, последний прогресс — ровно 1.0."""
    calls: list[tuple[float, str]] = []
    transport = by_country_transport(
        {country: (200, body(1)) for country in ("gb", "us", "de")}
    )

    compute_jobs(
        "rust",
        app_id="id",
        app_key="key",
        transport=transport,
        countries=("gb", "us", "de"),
        on_progress=lambda progress, message: calls.append((progress, message)),
    )

    assert len(calls) == 3
    assert calls[-1][0] == 1.0
    assert calls[-1][1] == "jobs de"
    assert [round(progress, 3) for progress, _ in calls] == [0.333, 0.667, 1.0]


# --- _parse_count и фикстура --------------------------------------------------


def test_parse_count_accepts_200_with_int() -> None:
    assert _parse_count(200, b'{"count": 42}') == 42


def test_parse_count_rejects_missing_non_int_and_non_200() -> None:
    assert _parse_count(200, b"{}") is None
    assert _parse_count(200, b'{"count": "42"}') is None
    assert _parse_count(200, b'{"count": true}') is None
    assert _parse_count(200, b"[1, 2]") is None
    assert _parse_count(200, b"not json \xd0\xbd\xd0\xb5") is None
    assert _parse_count(429, b"") is None
    assert _parse_count(500, b"") is None


def test_country_labels_cover_every_supported_country() -> None:
    """Каждая страна из ADZUNA_COUNTRIES подписана — иначе в UI мелькнёт «de»."""
    assert set(COUNTRY_LABELS) >= set(ADZUNA_COUNTRIES)


def test_fixture_transport_gives_germany_count() -> None:
    """Фикстура adzuna читается через транспорт: count=42, подпись Germany."""
    signal = compute_jobs(
        "requests", app_id="id", app_key="key",
        transport=fixture_transport, countries=("de",),
    )

    assert signal.total == 42
    assert signal.by_country[0].label == "Germany"


def test_as_dict_is_json_ready() -> None:
    """as_dict — плоский JSON: total/by_country/configured/error без датаклассов."""
    transport = by_country_transport({"de": (200, body(3))})

    payload = compute_jobs("rust", app_id="id", app_key="key",
                           transport=transport, countries=("de",)).as_dict()

    assert payload == {
        "total": 3,
        "by_country": [{"country": "de", "count": 3, "label": "Germany"}],
        "configured": True,
        "error": None,
    }
    json.dumps(payload)  # сериализуется без неожиданностей


def test_import_creates_no_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Повторный импорт модуля не пишет файлов и не читает каталогов данных."""
    monkeypatch.chdir(tmp_path)

    importlib.reload(jobs_module)

    assert list(tmp_path.iterdir()) == []
