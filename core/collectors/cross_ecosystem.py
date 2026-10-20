"""Cross-ecosystem momentum — сравнение пакета с соседями по concept (P4c, §18.3).

Один «концепт» (``vector-db``, ``llm-framework``) живёт сразу в нескольких
экосистемах: PyPI-пакет ``chromadb`` и npm-пакет ``chromadb`` — про одно и то
же, но спрос на них растёт с разной скоростью. Сигнал отвечает на вопрос
«в какой экосистеме концепт сейчас горячее» — без единого ключа: данные уже
лежат в ``concept_packages`` (mapping) и в ``observations`` (история).

Как и ``momentum``, модуль — чистая функция над списками: ни I/O, ни сети, ни
``sqlite3`` (AGENTS gotcha 19). Сторона, которая ходит в Store, — ``app/web.py``.

Формула сравнения (§18.3, уточнение P4c) — сравнение относительных momentum,
а не деление на ноль::

    faster  — momentum_30d(p) > max(momentum_30d(peers)) + MOMENTUM_UP_PCT
    slower  — momentum_30d(p) < min(momentum_30d(peers)) + MOMENTUM_DOWN_PCT
    same    — разница в пределах порога ±5% (шум измерения, как в momentum)
    unknown — ни у пакета, ни у соседей нет посчитанного momentum

Правила, которые легко нарушить молча:

* **соседи — только из других экосистем**: пакеты того же concept в своей
  собственной экосистеме соперниками не считаются (иначе «PyPI обгоняет npm»
  сравнивалось бы с другим PyPI-пакетом);
* **одноимённый пакет в другой экосистеме — сосед**, а не само себя: так
  ``chromadb`` (pypi) честно сравнивается с ``chromadb`` (npm);
* **нет concept или нет соседей** — не «0%», а честное отсутствие сигнала
  (``None`` или ``comparison='unknown'``): выдуманный процент на экране
  хуже пустого поля.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from core.collectors.momentum import (
    MOMENTUM_DOWN_PCT,
    MOMENTUM_UP_PCT,
    compute_momentum,
)

__all__ = [
    "CrossEcosystem",
    "CrossEcoPeer",
    "compute_cross_ecosystem",
    "compute_many",
]

#: Экосистемы концептов (§5): только эти значения лежат в concept_packages.
ECOSYSTEMS: tuple[str, ...] = ("pypi", "npm", "github")


@dataclass(frozen=True)
class CrossEcoPeer:
    """Сосед по концепту в другой экосистеме — строка таблицы в карточке."""

    name: str
    ecosystem: str
    momentum_30d: float | None
    adoption_week: int | None

    def as_dict(self) -> dict[str, Any]:
        """Плоский словарь для JSON (то же имя поля, что в momentum)."""
        return {
            "name": self.name,
            "ecosystem": self.ecosystem,
            "momentum_30d": self.momentum_30d,
            "adoption_week": self.adoption_week,
        }


@dataclass(frozen=True)
class CrossEcosystem:
    """Сигнал cross-ecosystem для одного пакета (§18.3).

    ``peers`` — пакеты того же concept в ДРУГИХ экосистемах (пустой кортеж —
    соседей нет). ``comparison`` — словесный вердикт сравнения: ``'faster'`` /
    ``'slower'`` / ``'same'`` / ``'unknown'``. Свои значения пакета
    (``self_momentum_30d`` / ``self_adoption_week``) лежат здесь же: карточка
    рисует «3 полосы» одним запросом и не должна второй раз считать momentum
    на клиенте.
    """

    concept: str
    ecosystem: str
    peers: tuple[CrossEcoPeer, ...]
    comparison: str
    self_momentum_30d: float | None
    self_adoption_week: int | None

    def as_dict(self) -> dict[str, Any]:
        """JSON для ``/api/signals`` и карточки пакета."""
        return {
            "concept": self.concept,
            "ecosystem": self.ecosystem,
            "peers": [peer.as_dict() for peer in self.peers],
            "comparison": self.comparison,
            "self_momentum_30d": self.self_momentum_30d,
            "self_adoption_week": self.self_adoption_week,
        }


def compute_cross_ecosystem(
    package: str,
    *,
    concept_packages: Iterable[Mapping[str, Any]],
    observations_by_name: Mapping[str, Iterable[Mapping[str, Any]]],
    package_ecosystem: str,
    now: datetime | None = None,
) -> CrossEcosystem | None:
    """Сигнал для одного пакета; ``None`` — у пакета нет concept (не настроен).

    ``concept_packages`` — строки ``Store.get_concept_packages(concept=...)``:
    все пакеты концепта, включая сам ``package``. ``observations_by_name`` —
    история каждого имени из тех строк (``Store.get_observations_history``).
    ``package_ecosystem`` — экосистема самого пакета: соседями становятся
    только строки других экосистем.

    Момент ``now`` инжектируется (как в ``momentum``), чтобы окно 30 дней в
    тестах не зависело от дня прогона.
    """
    rows = [row for row in concept_packages if str(row.get("package")) == package]
    if not rows:
        # Пакета нет в mapping — сигнал «не настроен», а не «соседей нет».
        return None
    concept = str(rows[0].get("concept") or "")
    package_momentum = compute_momentum(
        observations_by_name.get(package, []), now=now
    )
    self_delta = package_momentum.delta_30d

    peers: list[CrossEcoPeer] = []
    for row in concept_packages:
        if str(row.get("concept") or "") != concept:
            continue
        if str(row.get("ecosystem") or "") == package_ecosystem:
            # Своя экосистема — не соперник в сравнении экосистем.
            continue
        peer_name = str(row.get("package") or "")
        if not peer_name:
            continue
        peer_momentum = compute_momentum(
            observations_by_name.get(peer_name, []), now=now
        )
        peers.append(
            CrossEcoPeer(
                name=peer_name,
                ecosystem=str(row.get("ecosystem") or ""),
                momentum_30d=peer_momentum.delta_30d,
                adoption_week=peer_momentum.latest_week,
            )
        )
    # Сортировка: сначала экосистема, потом имя — ответ детерминирован.
    peers.sort(key=lambda peer: (peer.ecosystem, peer.name))

    return CrossEcosystem(
        concept=concept,
        ecosystem=package_ecosystem,
        peers=tuple(peers),
        comparison=_compare(self_delta, [peer.momentum_30d for peer in peers]),
        self_momentum_30d=self_delta,
        self_adoption_week=package_momentum.latest_week,
    )


def compute_many(
    packages: Iterable[str],
    *,
    all_concept_packages: Iterable[Mapping[str, Any]],
    observations_by_name: Mapping[str, Iterable[Mapping[str, Any]]],
    package_ecosystems: Mapping[str, str],
    now: datetime | None = None,
) -> dict[str, CrossEcosystem]:
    """Батч для ``/api/signals`` и матрицы: имя → сигнал.

    Имя без concept в результат не попадает — как у ``momentum.compute_many``:
    «сигнала нет» не равен «сигнал = 0».
    """
    rows = list(all_concept_packages)
    result: dict[str, CrossEcosystem] = {}
    for package in packages:
        ecosystem = package_ecosystems.get(package)
        if ecosystem is None:
            continue
        signal = compute_cross_ecosystem(
            package,
            concept_packages=rows,
            observations_by_name=observations_by_name,
            package_ecosystem=ecosystem,
            now=now,
        )
        if signal is not None:
            result[package] = signal
    return result


# --- внутреннее --------------------------------------------------------------


def _compare(
    self_delta: float | None, peer_deltas: list[float | None]
) -> str:
    """Вердикт сравнения — см. докстринг модуля.

    Порог ±5% берётся из ``momentum`` (``MOMENTUM_UP_PCT``/``MOMENTUM_DOWN_PCT``):
    два источника «что считать ростом» разошлись бы при первой правке константы,
    и цвет точки в матрице перестал бы совпадать с вердиктом в карточке.
    ``None`` вместо ``inf``: momentum от нулевой базы не определён.
    """
    if self_delta is None:
        return "unknown"
    known = [delta for delta in peer_deltas if delta is not None]
    if not known:
        # Соседей нет вовсе или ни у кого нет истории — сравнивать не с чем.
        return "unknown"
    if self_delta > max(known) + MOMENTUM_UP_PCT:
        return "faster"
    if self_delta < min(known) + MOMENTUM_DOWN_PCT:
        return "slower"
    return "same"
