"""Тесты core/diffing.py — чистые функции, без сети и без файлов (шаг 1c)."""

from __future__ import annotations

import dataclasses

import pytest

from core.diffing import (
    VERDICT_ABSENT,
    VERDICT_ALIVE,
    VERDICT_DEAD,
    VERDICT_PRESENT,
    VERDICT_STALE,
    VERDICT_WEAK,
    VerdictConfig,
    apply_stale,
    days_since,
    diff_observations,
    verdict,
)

CFG = VerdictConfig()


def kinds(events: list[dict]) -> list[str]:
    return [event["kind"] for event in events]


# --- days_since ----------------------------------------------------------------------------


def test_days_since_positive_for_past_date() -> None:
    result = days_since("2024-01-01")

    assert result is not None and result > 0


def test_days_since_accepts_iso_with_time() -> None:
    """PyPI отдаёт дату с временем — берём первые 10 символов, как legacy."""
    assert days_since("2024-01-01T10:00:00") == days_since("2024-01-01")


@pytest.mark.parametrize("value", [None, "", "?", "не дата", "2024-13-45", 0])
def test_days_since_none_for_empty_and_garbage(value: object) -> None:
    assert days_since(value) is None  # type: ignore[arg-type]


def test_days_since_today_is_zero_and_never_negative() -> None:
    from datetime import date

    today = date.today().strftime("%Y-%m-%d")
    assert days_since(today) == 0
    # Дата из будущего не должна выглядеть старше сегодняшней.
    assert days_since("2099-01-01") == 0


# --- verdict -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("has_pypi", "has_npm", "week", "month", "expected"),
    [
        (True, True, 500, 2000, VERDICT_ALIVE),
        (True, True, 5, 20, VERDICT_DEAD),
        (True, True, 50, 200, VERDICT_WEAK),
        (False, False, None, None, VERDICT_ABSENT),
        (True, False, None, None, VERDICT_PRESENT),
        (False, True, None, None, VERDICT_PRESENT),
    ],
)
def test_verdict_matrix(
    has_pypi: bool, has_npm: bool, week: int | None, month: int | None, expected: str
) -> None:
    assert verdict(has_pypi, has_npm, week, month, cfg=CFG) == expected


def test_verdict_absent_beats_adoption() -> None:
    """Нет пакета — вердикт «отсутствует», даже если статистика отдала числа."""
    assert verdict(False, False, 99999, 99999, cfg=CFG) == VERDICT_ABSENT


@pytest.mark.parametrize(
    ("week", "expected"),
    [(100, VERDICT_ALIVE), (99, VERDICT_WEAK), (10, VERDICT_WEAK), (9, VERDICT_DEAD), (0, VERDICT_DEAD)],
)
def test_verdict_weekly_thresholds_exactly_on_the_edge(week: int, expected: str) -> None:
    assert verdict(True, True, week, None, cfg=CFG) == expected


@pytest.mark.parametrize(
    ("month", "expected"),
    [(400, VERDICT_ALIVE), (399, VERDICT_WEAK), (40, VERDICT_WEAK), (39, VERDICT_DEAD)],
)
def test_verdict_monthly_thresholds_when_week_is_missing(month: int, expected: str) -> None:
    assert verdict(True, True, None, month, cfg=CFG) == expected


def test_verdict_week_wins_over_month() -> None:
    """Недельные скачивания измерены — месячные не смотрим (как legacy)."""
    assert verdict(True, True, 9, 99999, cfg=CFG) == VERDICT_DEAD


def test_verdict_respects_custom_thresholds() -> None:
    loose = VerdictConfig(alive_weekly=10, dead_weekly=5)
    assert verdict(True, False, 10, None, cfg=loose) == VERDICT_ALIVE
    assert verdict(True, False, 4, None, cfg=loose) == VERDICT_DEAD


# --- apply_stale ---------------------------------------------------------------------------


def test_apply_stale_turns_alive_into_stale() -> None:
    assert apply_stale(VERDICT_ALIVE, 600, cfg=CFG) == VERDICT_STALE


def test_apply_stale_keeps_alive_on_the_edge() -> None:
    """Ровно stale_days — ещё не застой: условие строгое (legacy)."""
    assert apply_stale(VERDICT_ALIVE, 540, cfg=CFG) == VERDICT_ALIVE


@pytest.mark.parametrize("base", [VERDICT_DEAD, VERDICT_WEAK, VERDICT_ABSENT, VERDICT_PRESENT])
def test_apply_stale_only_touches_alive(base: str) -> None:
    assert apply_stale(base, 6000, cfg=CFG) == base


def test_apply_stale_without_age_keeps_verdict() -> None:
    assert apply_stale(VERDICT_ALIVE, None, cfg=CFG) == VERDICT_ALIVE


# --- diff_observations ---------------------------------------------------------------------


def observation(**overrides: object) -> dict:
    """Наблюдение со всеми полями схемы; отдельные поля можно не задавать."""
    row: dict = {
        "name": "requests",
        "source": "pypi",
        "version": "2.34.2",
        "released_at": "2026-05-14",
        "adoption_week": 317314822,
        "adoption_month": 1202777296,
        "flag": False,
        "verdict": VERDICT_ALIVE,
        "measured_at": "2026-10-19T00:00:00",
    }
    row.update(overrides)
    return row


def test_diff_empty_lists() -> None:
    assert diff_observations([], [], cfg=CFG) == []
    assert diff_observations([observation()], [observation()], cfg=CFG) == []


def test_diff_new_and_missing_come_first() -> None:
    prev = [observation(name="a"), observation(name="b")]
    cur = [observation(name="b"), observation(name="c")]

    events = diff_observations(prev, cur, cfg=CFG)

    assert kinds(events) == ["new", "missing"]
    assert events[0]["name"] == "c" and events[0]["old"] is None
    assert events[0]["detail"] == "не было в прошлом прогоне"
    assert events[1]["name"] == "a" and events[1]["new"] is None
    assert events[1]["detail"] == "нет в текущем прогоне"


def test_diff_release_event() -> None:
    prev = [observation(version="2.34.1", released_at="2026-04-01")]
    cur = [observation(version="2.34.2", released_at="2026-05-14")]

    (event,) = diff_observations(prev, cur, cfg=CFG)

    assert event == {
        "kind": "release",
        "name": "requests",
        "detail": "2.34.1 → 2.34.2 (2026-05-14)",
        "old": "2.34.1",
        "new": "2.34.2",
    }


def test_diff_release_without_date_does_not_crash() -> None:
    prev = [observation(version="1.0", released_at=None)]
    cur = [observation(version="2.0", released_at=None)]

    (event,) = diff_observations(prev, cur, cfg=CFG)

    assert event["detail"] == "1.0 → 2.0 (?)"


def test_diff_flag_appeared_mentions_niche() -> None:
    prev = [observation(flag=False)]
    cur = [observation(flag=True)]

    (event,) = diff_observations(prev, cur, cfg=CFG)

    assert event["kind"] == "flag_appeared"
    assert event["old"] is False and event["new"] is True
    assert "нишу" in event["detail"] and "вендор" in event["detail"]


def test_diff_flag_removed() -> None:
    prev = [observation(flag=True)]
    cur = [observation(flag=False)]

    (event,) = diff_observations(prev, cur, cfg=CFG)

    assert event["kind"] == "flag_removed"
    assert event["detail"] == "проверить вручную"


def test_diff_adoption_change_fires_at_thirty_percent() -> None:
    prev = [observation(adoption_week=100)]
    cur = [observation(adoption_week=130)]

    (event,) = diff_observations(prev, cur, cfg=CFG)

    assert event["kind"] == "adoption_change"
    assert event["old"] == 100 and event["new"] == 130
    assert event["detail"] == "100 → 130 за нед (+30%)"


def test_diff_adoption_change_silent_at_ten_percent() -> None:
    prev = [observation(adoption_week=100)]
    cur = [observation(adoption_week=110)]

    assert diff_observations(prev, cur, cfg=CFG) == []


def test_diff_adoption_change_on_drop_uses_absolute_delta() -> None:
    prev = [observation(adoption_week=1000)]
    cur = [observation(adoption_week=700)]

    (event,) = diff_observations(prev, cur, cfg=CFG)

    assert event["kind"] == "adoption_change"
    assert event["detail"] == "1000 → 700 за нед (-30%)"


def test_diff_adoption_change_missing_week_is_not_an_event() -> None:
    prev = [observation(adoption_week=None)]
    cur = [observation(adoption_week=500)]

    assert diff_observations(prev, cur, cfg=CFG) == []


def test_diff_adoption_change_reacts_to_zero() -> None:
    """Отличие от legacy: падение скачиваний до нуля — событие, а не тишина."""
    prev = [observation(adoption_week=1000)]
    cur = [observation(adoption_week=0)]

    (event,) = diff_observations(prev, cur, cfg=CFG)

    assert event["kind"] == "adoption_change" and event["new"] == 0


def test_diff_verdict_change() -> None:
    prev = [observation(verdict=VERDICT_ALIVE)]
    cur = [observation(verdict=VERDICT_STALE)]

    (event,) = diff_observations(prev, cur, cfg=CFG)

    assert event["kind"] == "verdict_change"
    assert event["old"] == VERDICT_ALIVE and event["new"] == VERDICT_STALE
    assert event["detail"] == f"{VERDICT_ALIVE} → {VERDICT_STALE}"


def test_diff_events_of_one_name_follow_fixed_order() -> None:
    prev = [observation(version="1.0", flag=False, adoption_week=100, verdict=VERDICT_ALIVE)]
    cur = [
        observation(
            version="2.0",
            released_at="2026-10-01",
            flag=True,
            adoption_week=200,
            verdict=VERDICT_STALE,
        )
    ]

    events = diff_observations(prev, cur, cfg=CFG)

    assert kinds(events) == ["release", "flag_appeared", "adoption_change", "verdict_change"]
    assert all(event["name"] == "requests" for event in events)


def test_diff_partial_observations_do_not_crash() -> None:
    prev = [{"name": "x"}, {"name": "y", "version": "1.0"}]
    cur = [{"name": "x", "flag": True}, {"name": "y"}]

    events = diff_observations(prev, cur, cfg=CFG)

    # У x появился флаг, у y пропала версия (1.0 -> None), у обоих нет adoption.
    assert kinds(events) == ["flag_appeared", "release"]
    assert events[0]["name"] == "x"
    assert events[1]["name"] == "y" and events[1]["old"] == "1.0" and events[1]["new"] is None


def test_diff_skips_rows_without_name() -> None:
    prev = [observation(), {"source": "pypi"}]
    cur = [observation(), {"source": "pypi"}]

    assert diff_observations(prev, cur, cfg=CFG) == []


# --- VerdictConfig -------------------------------------------------------------------------


def test_verdict_config_defaults_match_legacy() -> None:
    assert (CFG.alive_weekly, CFG.alive_monthly) == (100, 400)
    assert (CFG.dead_weekly, CFG.dead_monthly) == (10, 40)
    assert CFG.stale_days == 540
    assert CFG.adoption_delta_pct == 25.0


def test_verdict_config_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        CFG.alive_weekly = 1  # type: ignore[misc]
