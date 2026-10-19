"""``core.collectors.momentum`` (шаг P4b): Δ adoption за 7/30/90 дней.

Чистая функция: ни БД, ни сети, ни файлов. Время инжектируется (``now``), иначе
тест «30 дней» зависел бы от дня прогона.

Что здесь доказывается (DEVPLAN §18.3):

* окно — «самый старый замер не старше N дней», а не «предыдущий замер»: между
  замерами бывают пропуски, и дельта «за 30 дней» по вчерашнему замеру была бы
  подписью не к тому интервалу;
* меньше двух замеров в окне — ``None`` и ``window_*_ok = False``: честнее
  показать «мало данных», чем 0%;
* база 0 — ``None`` (деление на ноль), а не ``inf``;
* цвет и подпись считаются здесь же, чтобы матрица и карточка не разошлись.
"""

from __future__ import annotations

import ast
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.collectors import momentum as module
from core.collectors.momentum import (
    MOMENTUM_DOWN_PCT,
    MOMENTUM_UP_PCT,
    Momentum,
    color_for,
    compute_many,
    compute_momentum,
)

#: Опорный момент: тесты не зависят от календаря машины.
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def at(days_ago: int, *, hour: int = 12) -> str:
    """``measured_at`` в формате Store (``utcnow_iso``: ISO с ``Z``)."""
    moment = NOW - timedelta(days=days_ago, hours=hour - 12)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def obs(week: int | None, days_ago: int, *, name: str = "requests") -> dict[str, Any]:
    """Строка observations, как её отдаёт ``get_observations_history``."""
    return {"name": name, "adoption_week": week, "measured_at": at(days_ago)}


# --- окна -------------------------------------------------------------------


def test_two_measurements_within_week_fill_all_three_windows() -> None:
    """100 -> 110 за 3 дня: все окна считаются от того же старого замера."""
    result = compute_momentum([obs(110, 0), obs(100, 3)], now=NOW)

    assert result.samples == 2
    assert result.latest_week == 110
    assert result.delta_7d == pytest.approx(10.0)
    assert result.delta_30d == pytest.approx(10.0)
    assert result.delta_90d == pytest.approx(10.0)
    assert result.window_7d_ok and result.window_30d_ok and result.window_90d_ok


def test_three_measurements_over_month_use_oldest_in_window() -> None:
    """100 (25 дней назад) -> 110 -> 121: Δ30 = +21%, Δ7 = None (в окне один)."""
    result = compute_momentum([obs(121, 0), obs(110, 20), obs(100, 25)], now=NOW)

    assert result.samples == 3
    assert result.delta_30d == pytest.approx(21.0)
    assert result.delta_7d is None
    assert result.window_7d_ok is False
    assert result.window_30d_ok is True


def test_single_measurement_has_no_deltas() -> None:
    """Один замер: дельт нет, окна не открыты, но счётчик честный."""
    result = compute_momentum([obs(100, 1)], now=NOW)

    assert result.samples == 1
    assert result.latest_week == 100
    assert result.delta_7d is None and result.delta_30d is None and result.delta_90d is None
    assert not result.window_7d_ok and not result.window_30d_ok and not result.window_90d_ok


def test_two_measurements_same_day_give_zero_delta() -> None:
    """Два замера в один день — это 0%, а не «мало данных»."""
    result = compute_momentum([obs(100, 0, ), obs(100, 0)], now=NOW)

    assert result.samples == 2
    assert result.delta_30d == pytest.approx(0.0)
    assert result.window_30d_ok is True
    assert result.label_30d() == "→ 0.0%/30д"


def test_empty_history_gives_empty_momentum() -> None:
    """Пустая история — все дельты ``None``, ``samples = 0``."""
    result = compute_momentum([], now=NOW)

    assert result.samples == 0
    assert result.latest_week is None
    assert result.delta_30d is None


def test_zero_base_gives_none_instead_of_infinity() -> None:
    """База 0 -> ``None``: процент от нуля не определён (деление на ноль).

    Окно при этом «открыто» (два замера есть): UI должен показать «посчитать
    нельзя», а не «мало данных».
    """
    result = compute_momentum([obs(100, 0), obs(0, 10)], now=NOW)

    assert result.delta_30d is None
    assert result.window_30d_ok is True
    assert result.color_for_30d() == "gray"


def test_missing_adoption_gives_no_deltas() -> None:
    """``adoption_week = None`` у последнего замера — считать не от чего."""
    result = compute_momentum([obs(None, 0), obs(100, 10)], now=NOW)

    assert result.latest_week is None
    assert result.samples == 2
    assert result.delta_7d is None and result.delta_30d is None and result.delta_90d is None


def test_measurement_older_than_window_is_skipped() -> None:
    """Замер старше 90 дней в окна не попадает и дельту не портит."""
    result = compute_momentum(
        [obs(130, 0), obs(120, 40), obs(100, 120)], now=NOW
    )

    # Δ30: в окне только последний замер -> мало данных
    assert result.delta_30d is None
    # Δ90: самый старый в окне — 40 дней назад (120), а не 120 дней назад (100)
    assert result.delta_90d == pytest.approx(round((130 - 120) / 120 * 100, 2))


def test_unparsable_measured_at_is_ignored() -> None:
    """Замер без разбираемой даты не ставится на ось — и не ломает расчёт."""
    broken = {"name": "requests", "adoption_week": 100, "measured_at": "вчера"}
    result = compute_momentum([obs(110, 0), broken], now=NOW)

    assert result.samples == 2          # в истории он есть
    assert result.delta_30d is None     # но в окно не попал
    assert result.window_30d_ok is False


def test_naive_timestamp_is_read_as_utc() -> None:
    """Наивная строка времени — UTC, как её пишет Store (``utcnow_iso``)."""
    naive = {"name": "requests", "adoption_week": 90,
             "measured_at": "2026-10-01T12:00:00"}
    result = compute_momentum([obs(99, 0), naive], now=NOW)

    assert result.delta_30d == pytest.approx(10.0)


def test_latest_is_the_first_row_not_the_newest_date() -> None:
    """Порядок входа — контракт: Store отдаёт новые сверху, функция не сортирует."""
    rows = [obs(200, 0), obs(100, 10)]
    result = compute_momentum(rows, now=NOW)

    assert result.latest_week == 200
    assert result.delta_30d == pytest.approx(100.0)


def test_input_rows_are_not_mutated() -> None:
    """Входной список и словари не меняются: их читает ещё и мини-график."""
    rows = [obs(110, 0), obs(100, 10)]
    snapshot = [dict(row) for row in rows]

    compute_momentum(rows, now=NOW)

    assert rows == snapshot


# --- цвет и подпись ---------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (10.0, "green"),
        (MOMENTUM_UP_PCT + 0.01, "green"),
        (0.0, "gray"),
        (MOMENTUM_UP_PCT, "gray"),        # ровно порог — ещё шум
        (MOMENTUM_DOWN_PCT, "gray"),
        (-10.0, "red"),
        (3.0, "gray"),
        (None, "gray"),
    ],
)
def test_color_for(delta: float | None, expected: str) -> None:
    """Цвет: > +5% зелёный, < -5% красный, иначе (и ``None``) серый."""
    assert color_for(delta) == expected


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (10.0, "↑ +10.0%/30д"),
        (-10.0, "↓ -10.0%/30д"),
        (0.0, "→ 0.0%/30д"),
        (3.25, "↑ +3.2%/30д"),
        (None, "—"),
    ],
)
def test_label_30d(delta: float | None, expected: str) -> None:
    """Подпись карточки: стрелка по знаку, число с одним знаком после точки."""
    result = Momentum(
        name="requests",
        delta_7d=None,
        delta_30d=delta,
        delta_90d=None,
        samples=2,
        latest_week=100,
        window_7d_ok=False,
        window_30d_ok=True,
        window_90d_ok=False,
    )

    assert result.label_30d() == expected


def test_as_dict_is_the_api_contract() -> None:
    """``as_dict`` отдаёт поля датакласса плюс ``label_30d`` и ``color``."""
    payload = compute_momentum([obs(140, 0), obs(100, 29)], now=NOW).as_dict()

    assert payload["delta_30d"] == pytest.approx(40.0)
    assert payload["color"] == "green"
    assert payload["label_30d"] == "↑ +40.0%/30д"
    assert payload["samples"] == 2
    assert payload["window_30d_ok"] is True


# --- батч и «истории нет» ---------------------------------------------------


def test_compute_many_skips_names_without_history() -> None:
    """Три имени, у одного истории нет — в результате два (а не «momentum = 0»)."""
    result = compute_many(
        {
            "requests": [obs(140, 0), obs(100, 29)],
            "flask": [obs(90, 0), obs(100, 20)],
            "empty": [],
        },
        now=NOW,
    )

    assert set(result) == {"requests", "flask"}
    assert result["requests"].color_for_30d() == "green"
    assert result["flask"].color_for_30d() == "red"


# --- гигиена модуля ---------------------------------------------------------


def test_import_creates_no_files_and_imports_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Импорт модуля — без файлов и без сети (правило ``core``).

    Проверяется двумя способами: перезагрузка модуля в пустом каталоге не
    создаёт в нём ни одного файла (иначе кэш/база появились бы при импорте), а
    разбор исходника показывает, что из внешнего мира модуль тянет только
    ``datetime``/``dataclasses``/``typing``.
    """
    monkeypatch.chdir(tmp_path)
    importlib.reload(module)

    assert list(tmp_path.iterdir()) == []

    source = Path(module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "dataclasses", "datetime", "typing"}, imported
