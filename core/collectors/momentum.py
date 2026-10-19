"""Momentum — Δ adoption по истории observations (шаг P4b, DEVPLAN §18.3).

Не «сколько скачиваний», а «как быстро растёт»: высокий adoption сам по себе
перестаёт быть синонимом «хорошо», потому что растущий пакет и падающий пакет
с одинаковым числом скачиваний — разные ответы на вопрос «сейчас решать?».

Работает без ключей и без сети: данные уже лежат в Store, модуль получает их
списком (``get_observations_history``) и считает арифметику. Поэтому здесь нет
ни транспорта, ни коллектора в смысле P5 — только чистая функция.

Формула (§18.3)::

    delta = (latest.adoption_week - oldest_in_window.adoption_week)
            / oldest_in_window.adoption_week * 100

``oldest_in_window`` — самый старый замер, отстоящий от текущего момента не
больше чем на 7/30/90 дней. Меньше двух замеров в окне — ``None``: «Δ за 30
дней» по одному замеру было бы выдумкой, а не измерением.

Три правила, которые легко нарушить молча:

* **часовой пояс** — ``measured_at`` пишется ``utcnow_iso()`` (ISO 8601 с ``Z``).
  Наивную строку тоже читаем как UTC, а не как локальное время: иначе на
  UTC+3 замер «сегодня 09:00`` уехал бы в окно 90 дней с другой стороны;
* **``now`` инжектируется** — тесты не зависят от календаря, а UI не получает
  «дельту, посчитанную вчера»;
* **деление на ноль** — пакет, у которого в начале окна был 0 скачиваний
  (замер есть, значения нет), даёт ``None``, а не ``inf``: процент от нуля не
  определён, и «+∞%» на экране было бы ложью.

``adoption_week = None`` (пакет без adoption) — тоже ``None`` во всех окнах:
считать не от чего, и такой пакет честно показывается как «мало данных».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

__all__ = [
    "MOMENTUM_DOWN_PCT",
    "MOMENTUM_UP_PCT",
    "Momentum",
    "color_for",
    "compute_many",
    "compute_momentum",
]

#: Порог «растёт» / «падает» в процентах (§18.3): ±5% — шум измерения, а не
#: тренд. Одна константа на оба конца, чтобы цвет и подпись не разъехались.
MOMENTUM_UP_PCT = 5.0
MOMENTUM_DOWN_PCT = -5.0

#: Окна сигнала в днях — они же имена полей ``delta_<N>d`` и ``window_<N>d_ok``.
WINDOWS: tuple[int, ...] = (7, 30, 90)


@dataclass(frozen=True)
class Momentum:
    """Δ adoption пакета за 7/30/90 дней — то, что уезжает в API и в карточку.

    ``delta_*`` — проценты (``+42.0`` = рост на 42%), ``None`` — данных мало
    или считать не от чего. ``window_*_ok`` говорит, почему ``None``: в окне
    меньше двух замеров (``False``) или замеры есть, но база нулевая/пустая
    (``True`` и ``None``). UI по этому флагу отличает «мало данных» от
    «посчитать нельзя».
    """

    name: str
    delta_7d: float | None
    delta_30d: float | None
    delta_90d: float | None
    samples: int
    latest_week: int | None
    window_7d_ok: bool
    window_30d_ok: bool
    window_90d_ok: bool

    def label_30d(self) -> str:
        """Подпись для карточки: ``'↑ +42.0%/30д'`` / ``'↓ -5.0%/30д'`` / ``'—'``.

        Стрелка — по знаку, а не по порогу цвета: ``+3%`` — это всё ещё рост
        (просто в пределах шума), и рисовать его «→» значило бы соврать о знаке.
        Ровно ноль — ``'→ 0.0%/30д'``.
        """
        delta = self.delta_30d
        if delta is None:
            return "—"
        if delta > 0:
            return f"↑ {delta:+.1f}%/30д"
        if delta < 0:
            return f"↓ {delta:+.1f}%/30д"
        # Ровно ноль: «+0.0%» выглядело бы как рост, а это «не изменился».
        return f"→ {delta:.1f}%/30д"

    def color_for_30d(self) -> str:
        """Цвет точки матрицы по ``delta_30d`` — см. :func:`color_for`."""
        return color_for(self.delta_30d)

    def as_dict(self) -> dict[str, Any]:
        """Плоский словарь для JSON: поля датакласса плюс вычисленные.

        ``label_30d`` и ``color`` считаются здесь, а не в JS: подпись — часть
        контракта сигнала (§7), и две её реализации (Python для тестов, JS для
        экрана) разошлись бы при первой правке формата.
        """
        return {
            "name": self.name,
            "delta_7d": self.delta_7d,
            "delta_30d": self.delta_30d,
            "delta_90d": self.delta_90d,
            "samples": self.samples,
            "latest_week": self.latest_week,
            "window_7d_ok": self.window_7d_ok,
            "window_30d_ok": self.window_30d_ok,
            "window_90d_ok": self.window_90d_ok,
            "label_30d": self.label_30d(),
            "color": self.color_for_30d(),
        }


def compute_momentum(
    observations: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> Momentum:
    """Посчитать momentum пакета по истории наблюдений.

    ``observations`` — из ``Store.get_observations_history`` (новые сверху).
    Порядок входа — часть контракта: ``latest`` — первый элемент. Переданный
    список не сортируется и не меняется (его читает ещё и мини-график).

    ``now`` — момент отсчёта (по умолчанию текущий UTC). Тесты подставляют своё
    время, чтобы окна 7/30/90 дней не зависели от дня прогона.
    """
    rows = [row for row in observations if isinstance(row, Mapping)]
    if not rows:
        return _empty_momentum("", 0, None)

    latest_row = rows[0]
    name = str(latest_row.get("name") or "")
    latest_week = _week(latest_row)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    deltas: dict[int, float | None] = {}
    windows_ok: dict[int, bool] = {}
    for days in WINDOWS:
        oldest = _oldest_in_window(rows, moment - timedelta(days=days))
        if oldest is None:
            # В окне меньше двух замеров: дельту считать не из чего.
            windows_ok[days] = False
            deltas[days] = None
            continue
        windows_ok[days] = True
        oldest_week = _week(oldest)
        if latest_week is None or not oldest_week:
            # База нулевая или значение не число: процент не определён.
            deltas[days] = None
            continue
        deltas[days] = round((latest_week - oldest_week) / oldest_week * 100, 2)

    return Momentum(
        name=name,
        delta_7d=deltas[7],
        delta_30d=deltas[30],
        delta_90d=deltas[90],
        samples=len(rows),
        latest_week=latest_week,
        window_7d_ok=windows_ok[7],
        window_30d_ok=windows_ok[30],
        window_90d_ok=windows_ok[90],
    )


def compute_many(
    observations_by_name: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    now: datetime | None = None,
) -> dict[str, Momentum]:
    """Momentum для батча имён (``GET /api/matrix``): есть история — есть запись.

    Имя без наблюдений в результат не попадает: пустой истории соответствует
    «сигнала нет», а не «momentum = 0» (иначе точка матрицы с нулевым
    adoption получила бы цвет «стабилен», хотя замеров нет вовсе).
    """
    result: dict[str, Momentum] = {}
    for name, observations in observations_by_name.items():
        rows = list(observations)
        if not rows:
            continue
        result[name] = compute_momentum(rows, now=now)
    return result


def color_for(delta_30d: float | None) -> str:
    """Цвет точки матрицы: ``'green'`` / ``'red'`` / ``'gray'`` (§18.3).

    Порог ±5%: меньше — шум измерения, а не тренд, и серый цвет честнее
    зелёного «почти не изменился». ``None`` (мало данных) — тоже серый.
    """
    if delta_30d is None:
        return "gray"
    if delta_30d > MOMENTUM_UP_PCT:
        return "green"
    if delta_30d < MOMENTUM_DOWN_PCT:
        return "red"
    return "gray"


# --- внутреннее --------------------------------------------------------------


def _empty_momentum(name: str, samples: int, latest: int | None) -> Momentum:
    """Запись «данных нет»: все дельты ``None``, все окна ``False``."""
    return Momentum(
        name=name,
        delta_7d=None,
        delta_30d=None,
        delta_90d=None,
        samples=samples,
        latest_week=latest,
        window_7d_ok=False,
        window_30d_ok=False,
        window_90d_ok=False,
    )


def _oldest_in_window(
    rows: list[Mapping[str, Any]], since: datetime
) -> Mapping[str, Any] | None:
    """Самый старый замер не старше ``since``; ``None`` — в окне меньше двух.

    Список пришёл новыми сверху, поэтому «самый старый в окне» — последний
    подходящий элемент, а не результат сортировки. Дата, которую не удалось
    разобрать, в окно не попадает: без времени замер нельзя поставить на ось,
    и притворяться, что он «недавний», нельзя.
    """
    in_window = [
        row for row in rows if (measured := _measured_at(row)) is not None and measured >= since
    ]
    if len(in_window) < 2:
        return None
    return in_window[-1]


def _measured_at(row: Mapping[str, Any]) -> datetime | None:
    """``measured_at`` строкой ISO (``Z`` или ``+00:00``) → ``datetime`` в UTC."""
    raw = row.get("measured_at")
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Наивная строка трактуется как UTC: так её писала и пишет БД.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _week(row: Mapping[str, Any]) -> int | None:
    """``adoption_week`` как целое; ``None``/не число — ``None``.

    ``bool`` числом не считается (как в ``app/web.py``): JSON ``true`` в
    колонке adoption означал бы замер «1 скачивание».
    """
    value = row.get("adoption_week")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
