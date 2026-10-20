"""Gap Score — чистые функции (шаг P5e, §18.3).

Проверяется формула, а не экраны: нормализация min-max, знаки весов,
поведение «данных нет» (``None`` вместо нуля) и границы 0..100. Модуль
чистый — ни сети, ни файлов, ни SQLite, поэтому тесты работают на словарях.

Ключевой приём: там, где важен масштаб, ожидание записано ТОЧНЫМ числом, а
не диапазоном. «score в 0..100» пропустил бы потерю деления на Σ|w| (score
уехал бы в сотни и усечение вернуло бы 100) — точное число такое не
пропускает.
"""

from __future__ import annotations

import pathlib

import pytest

import core.signals as signals_module
from core.settings import Settings
from core.signals import (
    COMPONENT_KEYS,
    GapComponents,
    GapWeights,
    Normalizer,
    compute_gap_score,
    compute_gap_scores_batch,
    default_weights_from_settings,
)


def normalizer(**ranges) -> Normalizer:
    """Нормализатор с заданными диапазонами (без ``fit`` — как в матрице «на 3 пакетах»)."""
    return Normalizer(**ranges)


def score(**kwargs) -> "signals_module.GapScore":
    """``compute_gap_score`` с дефолтными входами: тест задаёт только нужное."""
    base = {
        "name": "pkg",
        "adoption_week": None,
        "momentum_30d": None,
        "flag": None,
        "funding_total": None,
        "patents_impulse_pct": None,
        "jobs_total": None,
        "search_interest_trend": None,
        "weights": GapWeights(),
        "normalizer": Normalizer(),
    }
    base.update(kwargs)
    return compute_gap_score(**base)  # type: ignore[arg-type]


# --- Normalizer ---------------------------------------------------------------


def test_fit_takes_min_max_of_sample() -> None:
    """``fit`` берёт минимум и максимум по выборке, а не расширяет давние."""
    norm = Normalizer()
    norm.fit(
        [
            {"name": "a", "adoption_week": 100},
            {"name": "b", "adoption_week": 500},
            {"name": "c", "adoption_week": 1000},
        ]
    )

    assert norm.adoption_min == 100
    assert norm.adoption_max == 1000


def test_fit_skips_none_values() -> None:
    """Пакет без значения не участвует: иначе минимум стал бы ``None``."""
    norm = Normalizer()
    norm.fit(
        [
            {"name": "a", "adoption_week": None},
            {"name": "b", "adoption_week": 40},
            {"name": "c", "jobs_total": None},
        ]
    )

    assert norm.adoption_min == 40
    assert norm.adoption_max == 40


def test_norm_adoption_is_linear_min_max() -> None:
    """Середина диапазона по арифметике — 0.444, края — 0.0 и 1.0.

    Внимание: в тексте шага P5e ожидалось «norm_adoption(500) -> 0.5» для
    выборки [100, 500, 1000]. Min-max линеен, и 500 — медиана, а не середина
    отрезка 100..1000: (500−100)/900 = 0.444. Формула §18.3 («min-max по всем
    пакетам») оставлена как есть, ожидание приведено к ней (отклонение в
    отчёте шага): 0.5 получилось бы только у ранговой нормировки.
    """
    norm = normalizer(adoption_min=100, adoption_max=1000)

    assert norm.norm_adoption(550) == pytest.approx(0.5)
    assert norm.norm_adoption(500) == pytest.approx(4 / 9)
    assert norm.norm_adoption(100) == pytest.approx(0.0)
    assert norm.norm_adoption(1000) == pytest.approx(1.0)


def test_norm_returns_none_for_none() -> None:
    """Нет значения — нет компонента (не ноль: ноль был бы штрафом)."""
    norm = normalizer(adoption_min=100, adoption_max=1000)

    assert norm.norm_adoption(None) is None
    assert norm.norm_momentum(None) is None
    assert norm.norm_funding(None) is None


def test_norm_momentum_negative_range() -> None:
    """Momentum нормируется и на отрицательном диапазоне: -50 из -100..100 = 0.25."""
    norm = normalizer(momentum_min=-100, momentum_max=100)

    assert norm.norm_momentum(-50) == pytest.approx(0.25)


def test_norm_clamps_values_outside_range() -> None:
    """Значение вне диапазона усекается: norm < 0 перевернул бы знак веса."""
    norm = normalizer(adoption_min=100, adoption_max=1000)

    assert norm.norm_adoption(10) == pytest.approx(0.0)
    assert norm.norm_adoption(5000) == pytest.approx(1.0)


def test_norm_degenerate_range_is_half() -> None:
    """Одно значение в выборке (min == max) — 0.5, а не деление на ноль."""
    norm = normalizer(adoption_min=500, adoption_max=500)

    assert norm.norm_adoption(500) == pytest.approx(0.5)


# --- compute_gap_score: значения и границы -----------------------------------


def test_all_seven_components_give_score_in_range() -> None:
    """Все семь компонентов: score в 0..100, ``components_available`` = 7."""
    norm = normalizer(
        adoption_min=0, adoption_max=1000,
        momentum_min=-50, momentum_max=50,
        funding_min=0, funding_max=10,
        patents_min=-50, patents_max=50,
        jobs_min=0, jobs_max=100,
        search_min=-50, search_max=50,
    )

    result = score(
        adoption_week=500,
        momentum_30d=25,
        flag=False,
        funding_total=5,
        patents_impulse_pct=0,
        jobs_total=50,
        search_interest_trend=0,
        normalizer=norm,
    )

    assert 0.0 <= result.score <= 100.0
    assert result.components_available == 7


def test_score_is_normalized_by_used_weights_not_raw_sum() -> None:
    """Точное значение score: 100·(1·0.5 + 2·1.0)/(1+2) = 83.33.

    Без деления на Σ|w| вышло бы 250 (и усечение вернуло бы 100) — поэтому
    ожидание точное, а не «в диапазоне».
    """
    norm = normalizer(adoption_min=0, adoption_max=1000)

    result = score(adoption_week=500, flag=False, normalizer=norm)

    assert result.components.adoption == pytest.approx(0.5)
    assert result.components.no_flag == pytest.approx(1.0)
    assert result.score == pytest.approx(100 * 2.5 / 3)
    assert result.components_available == 2


def test_partial_components_do_not_change_scale() -> None:
    """Два компонента дают score по сумме их весов, а не по сумме всех семи.

    Иначе «пакет без сигналов» получал бы заведомо низкий балл просто потому,
    что ключей нет (0.5+2.0 из 5.6).
    """
    norm = normalizer(adoption_min=0, adoption_max=1000)

    result = score(adoption_week=500, flag=False, normalizer=norm)

    assert set(result.weights_used) == {"adoption", "no_flag"}
    assert result.score == pytest.approx(100 * 2.5 / 3)


def test_all_components_none_gives_none_score() -> None:
    """Нет ни одного компонента — ``score=None``, а не 0."""
    result = score()

    assert result.score is None
    assert result.components_available == 0
    assert result.components_total == 7
    assert result.weights_used == {}


def test_flag_false_true_none_map_to_no_flag_component() -> None:
    """Признак: нет MCP -> 1.0, есть -> 0.0, неизвестен -> ``None``."""
    assert score(flag=False).components.no_flag == pytest.approx(1.0)
    assert score(flag=True).components.no_flag == pytest.approx(0.0)
    assert score(flag=None).components.no_flag is None


def test_no_flag_component_uses_binary_value_not_normalization() -> None:
    """``no_flag`` не нормируется: 1.0 остаётся 1.0 при любом диапазоне выборки."""
    norm = normalizer(adoption_min=1000, adoption_max=1000, jobs_min=0, jobs_max=1000)

    result = score(flag=False, adoption_week=1000, normalizer=norm)

    assert result.components.no_flag == pytest.approx(1.0)
    # adoption при вырожденном диапазоне — 0.5, «середина»
    assert result.components.adoption == pytest.approx(0.5)


# --- compute_gap_score: знаки весов ------------------------------------------


def test_funding_penalty_lowers_score() -> None:
    """``funding`` с отрицательным весом уменьшает score при прочих равных."""
    norm = normalizer(adoption_min=0, adoption_max=1000, funding_min=0, funding_max=1000)
    common = dict(adoption_week=500, flag=False, normalizer=norm)

    without = score(**common, funding_total=0)
    with_funding = score(**common, funding_total=1000)

    assert with_funding.score < without.score
    assert with_funding.score == pytest.approx(100 * (1 * 0.5 + 2 * 1.0 - 0.5 * 1.0) / 3.5)


def test_patents_penalty_lowers_score() -> None:
    """``patents`` с отрицательным весом — тот же штраф, что и funding."""
    norm = normalizer(
        adoption_min=0, adoption_max=1000, patents_min=-100, patents_max=100
    )
    common = dict(adoption_week=500, flag=False, normalizer=norm)

    low = score(**common, patents_impulse_pct=-100)
    high = score(**common, patents_impulse_pct=100)

    assert high.score < low.score


def test_no_flag_weight_dominates_adoption() -> None:
    """Вес no_flag=2 сильнее adoption=1: «нет MCP» важнее размера adoption.

    Пакет без признака при минимальном adoption обгоняет пакет с признаком при
    максимальном: 100·2/3 = 66.7 против 100·1/3 = 33.3.
    """
    norm = normalizer(adoption_min=0, adoption_max=1000)

    no_flag_low_adoption = score(adoption_week=0, flag=False, normalizer=norm)
    with_flag_high_adoption = score(adoption_week=1000, flag=True, normalizer=norm)

    assert no_flag_low_adoption.score == pytest.approx(100 * 2.0 / 3)
    assert with_flag_high_adoption.score == pytest.approx(100 * 1.0 / 3)
    assert no_flag_low_adoption.score > with_flag_high_adoption.score


def test_score_is_clamped_to_zero() -> None:
    """Отрицательная сумма при штрафных весах — ноль баллов, не минус.

    §18.3 обещает шкалу 0..100; «ниша занята по всем штрафным компонентам»
    выражается нулём, а не отрицательным баллом.
    """
    norm = normalizer(
        adoption_min=1000, adoption_max=2000,
        funding_min=0, funding_max=1000,
        patents_min=0, patents_max=100,
    )
    # Все положительные компоненты нулевые (adoption на минимуме, MCP есть),
    # штрафные — максимальные: 100·(0 + 0 - 0.5 - 0.3)/3.8 ≈ -21 -> 0.
    result = score(
        adoption_week=1000,
        flag=True,
        funding_total=1000,
        patents_impulse_pct=100,
        normalizer=norm,
    )

    assert result.score == pytest.approx(0.0)


def test_zero_weights_give_zero_score_not_division_error() -> None:
    """Все применённые веса нулевые — 0.0, а не деление на ноль."""
    weights = GapWeights(
        adoption=0, momentum=0, no_flag=0, funding=0, patents=0, jobs=0, search=0
    )

    result = score(adoption_week=500, flag=False, weights=weights)

    assert result.score == pytest.approx(0.0)
    assert result.components_available == 2


# --- compute_gap_scores_batch -------------------------------------------------


def test_batch_fits_one_normalizer_for_all_packages() -> None:
    """Три пакета -> три score; нормализация общая (лучший adoption = norm 1.0)."""
    packages = [
        {"name": "a", "adoption_week": 100, "flag": False},
        {"name": "b", "adoption_week": 500, "flag": False},
        {"name": "c", "adoption_week": 1000, "flag": False},
    ]

    scores = compute_gap_scores_batch(packages, weights=GapWeights())

    assert len(scores) == 3
    assert scores["c"].components.adoption == pytest.approx(1.0)
    assert scores["a"].components.adoption == pytest.approx(0.0)
    # 500 из 100..1000 — 4/9 (линейный min-max; см. тест выше про ожидание
    # из текста шага, где стояло 0.5)
    assert scores["b"].components.adoption == pytest.approx(4 / 9)
    assert all(item.score is not None for item in scores.values())


def test_batch_weights_used_contains_only_applied_components() -> None:
    """``weights_used`` — применённые веса, а не весь набор из семи."""
    packages = [
        {"name": "a", "adoption_week": 100, "flag": False},
        {"name": "b", "adoption_week": 200, "flag": False, "jobs_total": 10},
    ]

    scores = compute_gap_scores_batch(packages, weights=GapWeights())

    assert set(scores["a"].weights_used) == {"adoption", "no_flag"}
    assert set(scores["b"].weights_used) == {"adoption", "no_flag", "jobs"}
    assert scores["b"].weights_used["jobs"] == pytest.approx(0.5)


def test_batch_skips_unknown_keys_and_none_signals() -> None:
    """Лишние ключи игнорируются, ``None``-сигналы в сумму не входят."""
    packages = [
        {"name": "a", "adoption_week": 100, "flag": False, "jobs_total": None,
         "search_interest_trend": None, "funding_total": None, "unknown": "x"},
    ]

    scores = compute_gap_scores_batch(packages, weights=GapWeights())

    assert scores["a"].components_available == 2
    assert scores["a"].components.jobs is None


def test_gap_components_as_dict_lists_all_seven_keys() -> None:
    """``GapComponents.as_dict`` — семь ключей в порядке контракта UI."""
    payload = GapComponents(adoption=0.5).as_dict()

    assert tuple(payload) == COMPONENT_KEYS
    assert payload["adoption"] == pytest.approx(0.5)
    assert payload["funding"] is None


# --- веса из Settings ---------------------------------------------------------


def test_default_weights_match_settings_defaults() -> None:
    """Дефолты ``GapWeights`` и ``Settings.gap_weight_*`` совпадают (§18.3)."""
    from_settings = default_weights_from_settings(Settings())

    assert from_settings == GapWeights()
    assert from_settings.as_dict() == {
        "adoption": 1.0,
        "momentum": 1.0,
        "no_flag": 2.0,
        "funding": -0.5,
        "patents": -0.3,
        "jobs": 0.5,
        "search": 0.3,
    }


def test_weights_from_settings_are_used() -> None:
    """Значение из настроек побеждает дефолт (adoption=0.5)."""
    weights = default_weights_from_settings(Settings(gap_weight_adoption=0.5))

    assert weights.adoption == pytest.approx(0.5)
    assert weights.no_flag == pytest.approx(2.0)


def test_weights_from_settings_fall_back_on_garbage() -> None:
    """Мусор в поле настроек (строка/NaN) — дефолт, а не падение расчёта."""
    settings = Settings()
    settings.gap_weight_adoption = "много"          # type: ignore[assignment]
    settings.gap_weight_momentum = float("nan")     # type: ignore[assignment]

    weights = default_weights_from_settings(settings)

    assert weights.adoption == pytest.approx(1.0)
    assert weights.momentum == pytest.approx(1.0)


# --- чистота импорта ----------------------------------------------------------


def test_import_has_no_side_effects(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Импорт модуля не создаёт файлов и не читает окружение/сеть."""
    import importlib

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "env-dir"))

    importlib.reload(signals_module)

    assert list(tmp_path.iterdir()) == []
    assert signals_module.Normalizer().adoption_min == 0
