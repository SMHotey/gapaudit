"""Gap Score — интегральная метрика 0–100: агрегат всех сигналов (§18.3, шаг P5e).

Ответ на вопрос «ниша свободна?» одним числом. Модуль чистый: ни файлов, ни
сети, ни SQLite — на вход приходят уже собранные числа (наблюдение пакета и
значения сигналов), на выходе :class:`GapScore`; кто и откуда их взял, решает
слой приложения (``app/web.py``). Работает частично без ключей (adoption,
momentum, no_flag), полный score — когда собраны jobs/patents/funding/search.

Формула (§18.3)::

    score_raw = Σ w_i · norm_i          # только компоненты, у которых есть данные
    score     = 100 · score_raw / Σ |w_i|   # нормировка по ИСПОЛЬЗОВАННЫМ весам

``norm_i`` — min-max по выборке пакетов (:class:`Normalizer`), поэтому score
сравним только внутри одного набора: матрица нормирует по всем точкам, а
``/api/gap-score`` — по тем же точкам, иначе «87» на карточке не совпало бы
с «87» на матрице.

Что здесь решено явно (и чего нет в тексте §18.3):

* **Усечение в 0..100.** При отрицательных весах (funding, patents) сумма
  может уйти ниже нуля: при дефолтных весах минимум −0.8/5.6 ≈ −14 баллов, а
  контракт обещает «0–100». Поэтому результат усечён снизу нулём: «ниша
  занята по всем штрафным компонентам» — это ноль, а не отрицательный балл.
* **Вырожденный диапазон** (в выборке одно значение: min == max) даёт 0.5, а не
  деление на ноль: сравнивать не с чем, и «середина» честнее крайностей.
* **Сигнал с ошибкой — не штраф.** Компонент приходит как ``None``, если
  данных нет (в том числе «ключ настроен, но API не ответил», gotcha 66), и
  тогда он не входит ни в сумму, ни в знаменатель — «нет данных» не должно
  понижать score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping

if TYPE_CHECKING:  # только для аннотации: на импорте core.settings не нужен
    from core.settings import Settings

__all__ = [
    "COMPONENT_KEYS",
    "GapComponents",
    "GapScore",
    "GapWeights",
    "NORMALIZED_COMPONENTS",
    "Normalizer",
    "compute_gap_score",
    "compute_gap_scores_batch",
    "default_weights_from_settings",
]

#: Компоненты score в том порядке, в каком они показываются в breakdown
#: (сначала положительные, потом штрафные). Порядок — часть контракта UI.
COMPONENT_KEYS: tuple[str, ...] = (
    "adoption",
    "momentum",
    "no_flag",
    "funding",
    "patents",
    "jobs",
    "search",
)

#: Компонент -> поле входной строки, по которому строится min-max.
#: ``no_flag`` здесь нет: он не нормируется (это 0/1 из признака ниши).
NORMALIZED_COMPONENTS: dict[str, str] = {
    "adoption": "adoption_week",
    "momentum": "momentum_30d",
    "funding": "funding_total",
    "patents": "patents_impulse_pct",
    "jobs": "jobs_total",
    "search": "search_interest_trend",
}


def _number(value: Any) -> float | None:
    """Число из значения или ``None``; ``bool`` числом не считается.

    ``True`` пришёл бы из JSON как 1 и молча стал бы компонентом score —
    та же ловушка, что у порогов в ``PUT /api/settings``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _int_number(value: Any) -> int | None:
    """Целое из значения (adoption_week) или ``None``."""
    number = _number(value)
    return int(number) if number is not None else None


@dataclass(frozen=True)
class GapComponents:
    """Нормированные компоненты score. ``None`` — данных нет.

    ``no_flag`` уже нормирован «руками»: 1.0, если признака ниши нет,
    0.0, если есть, ``None`` — признак неизвестен (наблюдения нет).
    """

    adoption: float | None = None       # norm(adoption_week)
    momentum: float | None = None       # norm(momentum_30d)
    no_flag: float | None = None        # 1.0 если flag=False, 0.0 если True
    funding: float | None = None        # norm(funding_total), вес отрицательный
    patents: float | None = None        # norm(patents_impulse_pct), вес отрицательный
    jobs: float | None = None           # norm(jobs_total)
    search: float | None = None         # norm(search_interest_trend)

    def as_dict(self) -> dict[str, float | None]:
        """Компоненты словарём в порядке :data:`COMPONENT_KEYS`."""
        return {key: getattr(self, key) for key in COMPONENT_KEYS}


@dataclass(frozen=True)
class GapWeights:
    """Веса из Settings; дефолты — §18.3 (те же, что у ``Settings.gap_weight_*``).

    Положительные: adoption, momentum, no_flag, jobs, search.
    Отрицательные: funding, patents — штраф («ниша занята»).
    """

    adoption: float = 1.0
    momentum: float = 1.0
    no_flag: float = 2.0
    funding: float = -0.5
    patents: float = -0.3
    jobs: float = 0.5
    search: float = 0.3

    def as_dict(self) -> dict[str, float]:
        """Веса словарём в порядке :data:`COMPONENT_KEYS`."""
        return {key: getattr(self, key) for key in COMPONENT_KEYS}

    def abs_sum(self, keys: Iterable[str]) -> float:
        """Σ|w| по перечисленным компонентам — знаменатель нормировки."""
        values = self.as_dict()
        return sum(abs(values[key]) for key in keys if key in values)


@dataclass(frozen=True)
class GapScore:
    """Score одного пакета: число 0..100 (или ``None``), компоненты и веса.

    ``weights_used`` — только фактически применённые веса (не все семь: у
    пакета без сигналов их меньше). ``components_available`` — сколько
    компонентов посчитано; UI показывает «учтено N из 7», чтобы «87» без
    половины сигналов не читалось как «87 по всем данным».
    """

    name: str
    score: float | None
    components: GapComponents
    weights_used: dict[str, float]
    components_available: int
    components_total: int = len(COMPONENT_KEYS)

    def as_dict(self) -> dict[str, Any]:
        """JSON для ``/api/gap-score``."""
        return {
            "name": self.name,
            "score": self.score,
            "components": self.components.as_dict(),
            "weights_used": dict(self.weights_used),
            "components_available": self.components_available,
            "components_total": self.components_total,
        }

    def breakdown(self) -> dict[str, Any]:
        """``gap_breakdown`` точки матрицы и карточки: компоненты + веса + счётчик.

        Плоский словарь (а не вложенный ``components``): карточка читает
        ``data.gap_breakdown[key]`` по имени компонента, и вложенность
        заставила бы её знать про две формы одного и того же ответа.
        """
        payload: dict[str, Any] = dict(self.components.as_dict())
        payload["weights_used"] = dict(self.weights_used)
        payload["components_available"] = self.components_available
        return payload


@dataclass
class Normalizer:
    """Min/max по выборке пакетов для min-max нормализации компонентов.

    Используется дважды: один раз для матрицы (все точки), один раз для
    точечных запросов — но всегда по ОДНОМУ набору, иначе score нельзя
    сравнивать между экранами. Значения по умолчанию — «датчики» на случай
    пустой выборки: если данных нет, компонент всё равно придёт ``None`` и в
    сумму не попадёт.
    """

    adoption_min: float = 0.0
    adoption_max: float = 1.0
    momentum_min: float = -100.0
    momentum_max: float = 100.0
    funding_min: float = 0.0
    funding_max: float = 1.0
    patents_min: float = -100.0
    patents_max: float = 100.0
    jobs_min: float = 0.0
    jobs_max: float = 1.0
    search_min: float = -100.0
    search_max: float = 100.0

    def fit(self, observations: Iterable[Mapping[str, Any]]) -> None:
        """Пересчитать min/max по выборке; ``None``-значения пропускаются.

        Именно пересчёт, а не расширение диапазона: экран рисует точки,
        нормированные по текущему набору, и «накопленный» диапазон от прошлой
        выборки сдвинул бы все score при смене фильтров. Компонент без единого
        значения оставляет свои значения по умолчанию.
        """
        values: dict[str, list[float]] = {key: [] for key in NORMALIZED_COMPONENTS}
        for row in observations:
            for key, field_name in NORMALIZED_COMPONENTS.items():
                number = _number(row.get(field_name))
                if number is not None:
                    values[key].append(number)
        for key, numbers in values.items():
            if not numbers:
                continue
            setattr(self, f"{key}_min", min(numbers))
            setattr(self, f"{key}_max", max(numbers))

    def _norm(self, key: str, value: Any) -> float | None:
        """Min-max в 0..1; ``None`` — значения нет.

        Вырожденный диапазон (min == max: в выборке одно значение) даёт 0.5.
        Значения вне диапазона усекаются: при точечном запросе пакет может
        оказаться ниже минимума выборки, и norm < 0 дал бы компонент со знаком,
        противоположным весу.
        """
        number = _number(value)
        if number is None:
            return None
        low = float(getattr(self, f"{key}_min"))
        high = float(getattr(self, f"{key}_max"))
        span = high - low
        if span <= 0:
            return 0.5
        return min(1.0, max(0.0, (number - low) / span))

    def norm_adoption(self, value: float | None) -> float | None:
        return self._norm("adoption", value)

    def norm_momentum(self, value: float | None) -> float | None:
        return self._norm("momentum", value)

    def norm_funding(self, value: float | None) -> float | None:
        return self._norm("funding", value)

    def norm_patents(self, value: float | None) -> float | None:
        return self._norm("patents", value)

    def norm_jobs(self, value: float | None) -> float | None:
        return self._norm("jobs", value)

    def norm_search(self, value: float | None) -> float | None:
        return self._norm("search", value)


def compute_gap_score(
    *,
    name: str,
    adoption_week: int | None,
    momentum_30d: float | None,
    flag: bool | None,
    funding_total: int | None,
    patents_impulse_pct: float | None,
    jobs_total: int | None,
    search_interest_trend: float | None,
    weights: GapWeights,
    normalizer: Normalizer,
) -> GapScore:
    """Нормировать компоненты, умножить на веса, сложить и привести к 0..100.

    ``score = 100 · Σ w_i·norm_i / Σ |w_i|``, где суммы — только по
    компонентам, у которых есть данные (``None`` не входит ни в числитель, ни
    в знаменатель). Все семь компонентов ``None`` → ``score=None``: «нет
    данных» и «ноль баллов» — разные ответы, и одинаковое число для них
    запретило бы отличать пустой пакет от занятой ниши.

    Если все применённые веса нулевые (пользователь так настроил), знаменатель
    нулевой: score = 0.0 — «ни один компонент ничего не значит» — вместо
    деления на ноль.
    """
    components = GapComponents(
        adoption=normalizer.norm_adoption(adoption_week),
        momentum=normalizer.norm_momentum(momentum_30d),
        no_flag=None if flag is None else (0.0 if flag else 1.0),
        funding=normalizer.norm_funding(funding_total),
        patents=normalizer.norm_patents(patents_impulse_pct),
        jobs=normalizer.norm_jobs(jobs_total),
        search=normalizer.norm_search(search_interest_trend),
    )

    weight_map = weights.as_dict()
    numerator = 0.0
    used: dict[str, float] = {}
    for key, value in components.as_dict().items():
        if value is None:
            continue
        weight = weight_map[key]
        used[key] = weight
        numerator += weight * value

    available = len(used)
    if available == 0:
        return GapScore(
            name=name,
            score=None,
            components=components,
            weights_used={},
            components_available=0,
        )

    denominator = weights.abs_sum(used)
    raw = 0.0 if denominator <= 0 else 100.0 * numerator / denominator
    score = min(100.0, max(0.0, raw))
    return GapScore(
        name=name,
        score=score,
        components=components,
        weights_used=used,
        components_available=available,
    )


def compute_gap_scores_batch(
    packages: list[dict],
    *,
    weights: GapWeights,
) -> dict[str, GapScore]:
    """Score для набора пакетов: один :class:`Normalizer` на всю выборку.

    Строка выборки — ``{name, adoption_week, momentum_30d, flag,
    funding_total, patents_impulse_pct, jobs_total, search_interest_trend}``
    (лишние ключи игнорируются). Пакеты с одинаковым именем схлопываются:
    ключ результата — имя, и «две строки одного пакета» — ошибка вызывающего,
    а не повод отдать два score.
    """
    normalizer = Normalizer()
    normalizer.fit(packages)

    scores: dict[str, GapScore] = {}
    for row in packages:
        name = str(row.get("name") or "")
        scores[name] = compute_gap_score(
            name=name,
            adoption_week=_int_number(row.get("adoption_week")),
            momentum_30d=_number(row.get("momentum_30d")),
            flag=_flag(row.get("flag")),
            funding_total=_int_number(row.get("funding_total")),
            patents_impulse_pct=_number(row.get("patents_impulse_pct")),
            jobs_total=_int_number(row.get("jobs_total")),
            search_interest_trend=_number(row.get("search_interest_trend")),
            weights=weights,
            normalizer=normalizer,
        )
    return scores


def _flag(value: Any) -> bool | None:
    """Признак ниши: ``None`` — неизвестен (наблюдения нет), иначе булево."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return None


def default_weights_from_settings(settings: "Settings") -> GapWeights:
    """Веса из ``Settings.gap_weight_*``; поля нет или мусор — дефолт §18.3.

    Настройки приходят из JSON-файла, который правит и пользователь: значение
    не того типа не должно ронять расчёт score, поэтому каждое поле проверяется
    отдельно и падает на дефолт.
    """
    defaults = GapWeights()
    values: dict[str, float] = {}
    for key in COMPONENT_KEYS:
        number = _number(getattr(settings, f"gap_weight_{key}", None))
        values[key] = getattr(defaults, key) if number is None else number
    return GapWeights(**values)
