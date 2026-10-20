"""``GET /api/matrix`` (шаг 2b): агрегат экрана «Сейчас решать».

Проверяется контракт квадрантов: точки берутся из ``Store.get_latest_observations``
(по одному наблюдению на имя), делятся порогом ``Settings.alive_weekly``, а
наблюдения без adoption в точки не попадают — на логарифмической оси им нет
места (``log10(0)``). Данные готовятся тем же API, которым пользуется шаг 2
(``Store.record_observations``); эндпоинт только читает.

Сеть — ноль, подпроцессы — ноль.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import app_state, auth


def observation(
    name: str, *, week: int | None = 1000, month: int = 4000, flag: int = 0
) -> dict[str, Any]:
    """Словарь вида ``core.packages.check()`` — столько нужно ``Store``."""
    return {
        "name": name,
        "src": "pypi",
        "ver": "1.2.3",
        "date": "2026-10-01",
        "summary": f"описание {name}",
        "home": f"https://example.invalid/{name}",
        "flag": flag,
        "month": month,
        "week": week,
        "verdict": "живой",
    }


def seed(client: TestClient, *observations: dict[str, Any]) -> None:
    store = app_state(client).store
    store.record_observations(
        list(observations), run_id="run-1", measured_at="2026-10-19T10:00:00Z"
    )


def matrix(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/matrix", headers=auth())
    assert response.status_code == 200
    return response.json()


def seed_at(
    client: TestClient, measured_at: str, *observations: dict[str, Any]
) -> None:
    """Батч наблюдений с явным временем замера — для истории momentum."""
    app_state(client).store.record_observations(
        list(observations), run_id=f"run-{measured_at}", measured_at=measured_at
    )


def momentum_point(client: TestClient, name: str) -> dict[str, Any]:
    points = matrix(client)["points"]
    return next(point for point in points if point["name"] == name)


def test_empty_store_gives_empty_quadrants(client: TestClient) -> None:
    """Пустая БД: ни точек, ни имён в квадрантах, счётчики по нулям."""
    payload = matrix(client)

    assert payload["points"] == []
    assert payload["quadrants"] == {
        "high_no_flag": [],
        "high_with_flag": [],
        "low_no_flag": [],
        "low_with_flag": [],
    }
    assert payload["counts"] == {"total": 0, "with_week": 0, "high_no_flag": 0}


def test_high_adoption_without_flag_is_the_candidate(client: TestClient) -> None:
    """Живой пакет без признака — в ``high_no_flag`` («ВОТ СЮДА»)."""
    seed(client, observation("ruff", week=12_345_678, flag=0))

    payload = matrix(client)

    assert [point["name"] for point in payload["points"]] == ["ruff"]
    assert payload["quadrants"]["high_no_flag"] == ["ruff"]
    point = payload["points"][0]
    assert point["adoption_week"] == 12_345_678
    assert point["adoption_month"] == 4000
    assert point["flag"] is False
    # «flag» в ответе — булево из flag_value, метка ключевого слова отдельно:
    # в схеме flag TEXT хранит "MCP", и bool("MCP") всегда истинно
    assert point["flag_label"] == "MCP"
    assert point["source"] == "pypi"

    assert point["verdict"] == "живой"
    assert point["measured_at"] == "2026-10-19T10:00:00Z"
    assert payload["counts"] == {
        "total": 1,
        "with_week": 1,
        "high_no_flag": 1,
    }


def test_low_adoption_without_flag_is_low_market(client: TestClient) -> None:
    """Adoption ниже порога без признака — ``low_no_flag`` («рынка нет»)."""
    seed(client, observation("dead", week=5, flag=0))

    payload = matrix(client)

    assert payload["quadrants"]["low_no_flag"] == ["dead"]
    assert payload["quadrants"]["high_no_flag"] == []
    assert payload["points"][0]["adoption_week"] == 5


def test_flag_splits_two_high_adoption_packages(client: TestClient) -> None:
    """Одинаково живые пакеты делятся признаком: занято / «ВОТ СЮДА»."""
    seed(
        client,
        observation("ruff", week=200_000, flag=0),
        observation("polars", week=300_000, flag=1),
    )

    payload = matrix(client)

    assert payload["quadrants"]["high_no_flag"] == ["ruff"]
    assert payload["quadrants"]["high_with_flag"] == ["polars"]
    assert payload["counts"]["with_week"] == 2
    assert payload["counts"]["high_no_flag"] == 1


def test_observation_without_adoption_is_counted_but_not_plotted(
    client: TestClient,
) -> None:
    """``week=None``: в ``counts.total`` есть, в точках — нет (log-ось)."""
    seed(
        client,
        observation("no-stats", week=None, month=0),
        observation("ruff", week=1000),
    )

    payload = matrix(client)

    assert [point["name"] for point in payload["points"]] == ["ruff"]
    assert payload["counts"]["total"] == 2
    assert payload["counts"]["with_week"] == 1


def test_threshold_comes_from_settings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порог — из ``Settings.alive_weekly``; 60 при пороге 50 уходит в «high».

    Настройки читаются на каждом запросе (``app.state.settings``), а не один
    раз при сборке приложения: экран настроек (2c) сможет менять порог без
    перезапуска.
    """
    settings = app_state(client).settings
    monkeypatch.setattr(settings, "alive_weekly", 50)
    seed(client, observation("sixty", week=60, flag=0))

    payload = matrix(client)

    assert payload["thresholds"] == {"alive_weekly": 50, "alive_monthly": 400}
    assert payload["quadrants"]["high_no_flag"] == ["sixty"]

    monkeypatch.setattr(settings, "alive_weekly", 61)
    assert matrix(client)["quadrants"]["low_no_flag"] == ["sixty"]


# --- momentum точки (шаг P4b) ------------------------------------------------


def days_ago_iso(days: int) -> str:
    """``measured_at`` на N дней назад от текущего момента (формат Store)."""
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_momentum_color_splits_growing_and_falling(client: TestClient) -> None:
    """Растущий пакет — зелёный, падающий — красный (§18.3).

    До P4b цвет точки означал признак ниши («MCP есть/нет») и читался как
    «хорошо/плохо»; теперь он про тренд, и это главный смысл шага.
    """
    # быстрый: 100 -> 140 за 29 дней (+40%)
    seed_at(client, days_ago_iso(29), observation("fast", week=100, flag=0))
    seed_at(client, days_ago_iso(0), observation("fast", week=140, flag=0))
    # медленный: 100 -> 90 за 5 дней (-10%)
    seed_at(client, days_ago_iso(5), observation("slow", week=100, flag=1))
    seed_at(client, days_ago_iso(0), observation("slow", week=90, flag=1))

    fast = momentum_point(client, "fast")
    slow = momentum_point(client, "slow")

    assert fast["momentum_30d"] == pytest.approx(40.0)
    assert fast["momentum_color"] == "green"
    assert slow["momentum_30d"] == pytest.approx(-10.0)
    assert slow["momentum_color"] == "red"


def test_single_measurement_has_gray_momentum(client: TestClient) -> None:
    """Один замер — ``momentum_30d = None`` и серый цвет, точка остаётся."""
    seed(client, observation("lonely", week=5000, flag=0))

    point = momentum_point(client, "lonely")

    assert point["momentum_30d"] is None
    assert point["momentum_color"] == "gray"


def test_momentum_samples_counts_history(client: TestClient) -> None:
    """``momentum_samples`` — сколько замеров в истории имени."""
    seed_at(client, days_ago_iso(50), observation("many", week=80))
    seed_at(client, days_ago_iso(20), observation("many", week=90))
    seed_at(client, days_ago_iso(0), observation("many", week=100))

    point = momentum_point(client, "many")

    assert point["momentum_samples"] == 3
    assert point["momentum_30d"] == pytest.approx(11.11)   # (100-90)/90
    assert point["momentum_color"] == "green"


def test_existing_point_fields_survive_momentum(client: TestClient) -> None:
    """Поля точки 2b на месте: momentum добавлен, а не заменил adoption/flag."""
    seed_at(client, days_ago_iso(0), observation("ruff", week=12_000, flag=1))

    point = momentum_point(client, "ruff")

    for key in (
        "name", "source", "version", "released_at", "adoption_week",
        "adoption_month", "flag", "flag_label", "verdict", "measured_at",
        "momentum_30d", "momentum_color", "momentum_samples",
    ):
        assert key in point, key
    assert point["adoption_week"] == 12_000
    assert point["flag"] is True


# --- Gap Score в точках матрицы (шаг P5e) ------------------------------------


def record_signal(
    client: TestClient, package: str, kind: str, *, value: float, **detail: Any
) -> None:
    """Наблюдение сигнала в том виде, в каком его пишет хук lifespan (§7).

    ``configured=True`` и ``error=None`` — «собран»; тесты с «ключ есть, но
    API не ответил» передают ``error="all requests failed"`` явно.
    """
    payload = {"configured": True, "error": None}
    payload.update(detail)
    app_state(client).store.record_signal_observation(
        package=package, signal_kind=kind, value=value, detail=payload
    )


def test_matrix_points_have_gap_score_and_seven_component_breakdown(
    client: TestClient,
) -> None:
    """Три точки с разными adoption: у каждой score и breakdown из 7 компонентов.

    Компоненты считаются по одним и тем же сигналам, поэтому проверяется их
    форма и то, что крайние adoption дают 0.0 и 1.0 (min-max по выборке).
    """
    seed(
        client,
        observation("low", week=100, flag=0),
        observation("mid", week=500, flag=0),
        observation("high", week=1000, flag=0),
    )

    points = {point["name"]: point for point in matrix(client)["points"]}

    for name in ("low", "mid", "high"):
        point = points[name]
        assert point["gap_score"] is not None, name
        assert 0.0 <= point["gap_score"] <= 100.0, name
        breakdown = point["gap_breakdown"]
        for key in (
            "adoption", "momentum", "no_flag", "funding", "patents",
            "jobs", "search", "weights_used", "components_available",
        ):
            assert key in breakdown, (name, key)
    assert points["low"]["gap_breakdown"]["adoption"] == pytest.approx(0.0)
    assert points["high"]["gap_breakdown"]["adoption"] == pytest.approx(1.0)


def test_point_without_signals_has_fewer_components(client: TestClient) -> None:
    """Точка без единого сигнала: score есть, компонентов меньше семи.

    В матрицу попадают только наблюдения с ``adoption_week > 0``, и признак
    ниши известен всегда — поэтому минимальный набор здесь два компонента
    (adoption + no_flag), а не ноль. «Нет ни одного компонента -> score =
    None» проверяется на чистой функции (``test_core_signals``): через API
    это состояние недостижимо, и тест на матрице изображал бы его подделкой.
    """
    seed(client, observation("bare", week=1000, flag=0))

    point = momentum_point(client, "bare")

    assert point["gap_score"] is not None
    assert point["gap_breakdown"]["components_available"] == 2
    assert set(point["gap_breakdown"]["weights_used"]) == {"adoption", "no_flag"}
    assert point["gap_breakdown"]["funding"] is None
    assert point["gap_breakdown"]["jobs"] is None


def test_gap_score_is_none_only_without_any_data(client: TestClient) -> None:
    """``gap_score`` в матрице не бывает ``None``: adoption и признак есть всегда.

    Сценарий шага «точка без adoption и без сигналов -> gap_score=None»
    недостижим по устройству матрицы: ``log10(0)`` не существует, и такие
    наблюдения в точки не попадают (они видны в ``counts.total``). Проверяем
    именно это: пакет без adoption не создаёт точку, то есть «пустого» score
    на экране не бывает, а не «пустой score = 0».
    """
    seed(client, observation("zero", week=0, flag=0))

    payload = matrix(client)

    assert payload["points"] == []
    assert payload["counts"]["total"] == 1


def test_flag_true_lowers_gap_score(client: TestClient) -> None:
    """Признак ниши (вес no_flag=2) опускает score при равных adoption/momentum."""
    seed(
        client,
        observation("free", week=1000, flag=0),
        observation("busy", week=1000, flag=1),
    )

    points = {point["name"]: point for point in matrix(client)["points"]}

    assert points["free"]["gap_breakdown"]["no_flag"] == pytest.approx(1.0)
    assert points["busy"]["gap_breakdown"]["no_flag"] == pytest.approx(0.0)
    assert points["free"]["gap_score"] > points["busy"]["gap_score"]


def test_funding_penalty_lowers_gap_score(client: TestClient) -> None:
    """``funding`` больше нуля уменьшает score: вес отрицательный (штраф)."""
    seed(
        client,
        observation("quiet", week=1000, flag=0),
        observation("funded", week=1000, flag=0),
    )
    record_signal(
        client, "quiet", "funding", value=0, total=0, trend_pct=None,
        by_quarter=[],
    )
    record_signal(
        client, "funded", "funding", value=40, total=40, trend_pct=10.0,
        by_quarter=[],
    )

    points = {point["name"]: point for point in matrix(client)["points"]}

    assert points["quiet"]["gap_breakdown"]["funding"] == pytest.approx(0.0)
    assert points["funded"]["gap_breakdown"]["funding"] == pytest.approx(1.0)
    assert points["funded"]["gap_score"] < points["quiet"]["gap_score"]
    assert points["quiet"]["gap_breakdown"]["weights_used"]["funding"] < 0


def test_failed_signal_does_not_penalise_score(client: TestClient) -> None:
    """Ключ есть, API не ответил (``error``) — компонент ``None``, а не штраф.

    Правило gotcha 66: «настроен, но нет данных» не должно понижать балл —
    иначе отсутствие ответа API выглядело бы как занятая ниша.
    """
    seed(
        client,
        observation("failed", week=1000, flag=0),
        observation("clean", week=1000, flag=0),
    )
    record_signal(
        client, "failed", "jobs", value=0, total=0,
        error="all requests failed", by_country=[],
    )

    points = {point["name"]: point for point in matrix(client)["points"]}

    assert points["failed"]["gap_breakdown"]["jobs"] is None
    assert points["failed"]["gap_score"] == pytest.approx(
        points["clean"]["gap_score"]
    )
