"""``core.collectors.cross_ecosystem`` (шаг P4c): сравнение экосистем по concept.

Чистая функция, как и ``momentum``: данные приходят списками, ни I/O, ни сети,
ни ``sqlite3``. Тесты проверяют четыре вещи, которые ломаются молча:

* **соседи только из других экосистем** — свой экосистемный «соперник»
  сравнивать нельзя (иначе «PyPI обгоняет npm» меряется с другим PyPI);
* **``None`` вместо выдумки** — без concept, без соседей, без истории:
  сигнала нет, а не «0%»;
* **порог ±5%** — берётся из ``momentum`` (``MOMENTUM_UP_PCT``), а не
  дублируется числом;
* **момент инжектируется** — окно 30 дней не зависит от дня прогона.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.collectors.cross_ecosystem import (
    CrossEcosystem,
    compute_cross_ecosystem,
    compute_many,
)
from core.collectors.momentum import MOMENTUM_UP_PCT

#: Фиксированный момент отсчёта: все окна в тестах считаются от него.
NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


def iso_days_ago(days: int) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def observation(name: str, *, days: int, week: int) -> dict[str, Any]:
    """Одна точка истории: ``days`` дней назад, adoption за неделю ``week``."""
    return {
        "name": name,
        "measured_at": iso_days_ago(days),
        "adoption_week": week,
        "adoption_month": week * 4,
    }


def history(*points: tuple[int, int]) -> list[dict[str, Any]]:
    """История нового сверху из пар ``(дней назад, adoption_week)``."""
    return [observation("pkg", days=days, week=week) for days, week in points]


def row(concept: str, ecosystem: str, package: str) -> dict[str, str]:
    """Строка ``concept_packages`` — как её отдаёт ``Store``."""
    return {"concept": concept, "ecosystem": ecosystem, "package": package}


def compute(
    package: str,
    concept_packages: list[dict[str, str]],
    observations: dict[str, list[dict[str, Any]]],
    ecosystem: str = "pypi",
) -> CrossEcosystem | None:
    return compute_cross_ecosystem(
        package,
        concept_packages=concept_packages,
        observations_by_name=observations,
        package_ecosystem=ecosystem,
        now=NOW,
    )


# --- отсутствие сигнала -------------------------------------------------------


def test_package_without_concept_is_none() -> None:
    """Пакета нет в mapping — ``None`` (сигнал «не настроен»), не «нет соседей»."""
    assert compute("requests", [], {}, ecosystem="pypi") is None


def test_empty_concept_packages_is_none() -> None:
    """Пустой mapping — ``None``, даже если история по имени есть."""
    observations = {"chromadb": history((0, 100), (25, 100))}
    assert compute("chromadb", [], observations) is None


def test_concept_without_neighbours_has_empty_peers_and_unknown() -> None:
    """Только свой экосистемный ряд: peers пуст, ``comparison='unknown'``."""
    rows = [
        row("vector-db", "pypi", "chromadb"),
        row("vector-db", "pypi", "weaviate-client"),  # свой же мир — не сосед
    ]
    observations = {
        "chromadb": history((0, 120), (25, 100)),
        "weaviate-client": history((0, 100), (25, 100)),
    }

    signal = compute("chromadb", rows, observations)

    assert signal is not None
    assert signal.peers == ()
    assert signal.comparison == "unknown"
    # Свои значения при этом посчитаны — карточка их рисует.
    assert signal.self_momentum_30d == 20.0


def test_concept_without_observations_is_unknown() -> None:
    """Concept задан, истории ни у кого нет: peers есть, momentum везде None."""
    rows = [row("vector-db", "pypi", "chromadb"), row("vector-db", "npm", "chromadb")]

    signal = compute("chromadb", rows, {})

    assert signal is not None
    assert signal.comparison == "unknown"
    assert signal.self_momentum_30d is None
    assert [peer.momentum_30d for peer in signal.peers] == [None]


def test_package_rows_missing_means_none() -> None:
    """В переданном mapping строки пакета нет — сигнал не настроен."""
    rows = [row("vector-db", "npm", "chromadb")]
    assert compute("weaviate-client", rows, {}, ecosystem="pypi") is None


# --- сравнение ----------------------------------------------------------------


def test_faster_when_self_grows_more_than_peer() -> None:
    """self +20% против npm +0%: ``faster`` (разница больше порога +5)."""
    rows = [row("vector-db", "pypi", "chromadb"), row("vector-db", "npm", "chromadb-npm")]
    observations = {
        "chromadb": history((0, 120), (25, 100)),
        "chromadb-npm": history((0, 100), (25, 100)),
    }

    signal = compute("chromadb", rows, observations)

    assert signal is not None
    assert signal.comparison == "faster"
    assert signal.self_momentum_30d == 20.0
    assert signal.peers[0].momentum_30d == 0.0


def test_slower_when_self_grows_less_than_peer() -> None:
    """self +0% против npm +20%: ``slower`` (ниже min(peers) − 5)."""
    rows = [row("vector-db", "pypi", "chromadb"), row("vector-db", "npm", "chromadb-npm")]
    observations = {
        "chromadb": history((0, 100), (25, 100)),
        "chromadb-npm": history((0, 120), (25, 100)),
    }

    signal = compute("chromadb", rows, observations)

    assert signal is not None
    assert signal.comparison == "slower"


def test_same_when_difference_within_noise_threshold() -> None:
    """+6% против +4% — шум: ``same`` (порог ±5% из momentum)."""
    rows = [row("vector-db", "pypi", "chromadb"), row("vector-db", "npm", "chromadb-npm")]
    observations = {
        "chromadb": history((0, 106), (25, 100)),
        "chromadb-npm": history((0, 104), (25, 100)),
    }

    signal = compute("chromadb", rows, observations)

    assert signal is not None
    assert signal.comparison == "same"


def test_threshold_is_momentum_constant_not_literal() -> None:
    """Порог сравнения — ``MOMENTUM_UP_PCT``: два источника «что есть рост»."""
    import core.collectors.cross_ecosystem as module

    rows = [row("vector-db", "pypi", "chromadb"), row("vector-db", "npm", "chromadb-npm")]
    observations = {
        "chromadb": history((0, int(100 + MOMENTUM_UP_PCT) + 1), (25, 100)),
        "chromadb-npm": history((0, 100), (25, 100)),
    }

    signal = compute("chromadb", rows, observations)

    assert signal is not None
    # Ровно на границе порога «быстрее» ещё нет — неравенство строгое.
    assert module._compare(MOMENTUM_UP_PCT, [0.0]) == "same"
    assert module._compare(MOMENTUM_UP_PCT + 0.01, [0.0]) == "faster"
    assert signal.comparison == "faster"


def test_two_peers_one_without_momentum_uses_known_only() -> None:
    """Из двух соседей известен один: сравнение идёт по нему."""
    rows = [
        row("vector-db", "pypi", "chromadb"),
        row("vector-db", "npm", "chromadb-npm"),
        row("vector-db", "github", "chroma-core/chroma"),
    ]
    observations = {
        "chromadb": history((0, 130), (25, 100)),   # +30%
        "chromadb-npm": history((0, 100), (25, 100)),  # 0%
        "chroma-core/chroma": [],                     # истории нет
    }

    signal = compute("chromadb", rows, observations)

    assert signal is not None
    assert len(signal.peers) == 2
    by_name = {peer.name: peer for peer in signal.peers}
    assert by_name["chroma-core/chroma"].momentum_30d is None
    # 30 > 0 + 5 — известный сосед делает вывод, неопределённый не мешает.
    assert signal.comparison == "faster"


def test_all_peers_without_momentum_is_unknown() -> None:
    """Ни у кого из соседей нет истории — сравнивать не с чем."""
    rows = [row("vector-db", "pypi", "chromadb"), row("vector-db", "npm", "chromadb-npm")]
    observations = {"chromadb": history((0, 120), (25, 100))}

    signal = compute("chromadb", rows, observations)

    assert signal is not None
    assert signal.comparison == "unknown"


# --- структура ответа ---------------------------------------------------------


def test_peers_exclude_own_ecosystem_and_include_others() -> None:
    """Соседи — только чужие экосистемы; одноимённый пакет в npm — сосед."""
    rows = [
        row("vector-db", "pypi", "chromadb"),
        row("vector-db", "pypi", "weaviate-client"),
        row("vector-db", "npm", "chromadb"),
        row("vector-db", "github", "chroma-core/chroma"),
        row("other-concept", "npm", "something-else"),
    ]
    observations = {
        "chromadb": history((0, 100), (25, 100)),
        "weaviate-client": history((0, 100), (25, 100)),
        "chroma-core/chroma": history((0, 100), (25, 100)),
    }

    signal = compute("chromadb", rows, observations)

    assert signal is not None
    assert {peer.ecosystem for peer in signal.peers} == {"npm", "github"}
    # одноимённый npm-пакет — сосед, а не «само себя» (так §7.6: pypi self + npm peer)
    assert "chromadb" in [peer.name for peer in signal.peers]
    assert signal.ecosystem == "pypi"
    assert signal.concept == "vector-db"


def test_peers_sorted_by_ecosystem_then_name() -> None:
    """Порядок соседей детерминирован: экран и тесты не зависят от вставки."""
    rows = [
        row("vector-db", "pypi", "chromadb"),
        row("vector-db", "github", "z-repo"),
        row("vector-db", "npm", "a-pkg"),
        row("vector-db", "npm", "b-pkg"),
    ]

    signal = compute("chromadb", rows, {})

    assert signal is not None
    assert [(peer.ecosystem, peer.name) for peer in signal.peers] == [
        ("github", "z-repo"),
        ("npm", "a-pkg"),
        ("npm", "b-pkg"),
    ]


def test_as_dict_carries_self_values_for_card() -> None:
    """``as_dict`` несёт ``self_momentum_30d``/``self_adoption_week`` — для UI."""
    rows = [row("vector-db", "pypi", "chromadb"), row("vector-db", "npm", "chromadb-npm")]
    observations = {
        "chromadb": history((0, 121), (25, 100)),
        "chromadb-npm": history((0, 105), (25, 100)),
    }

    signal = compute("chromadb", rows, observations)
    assert signal is not None
    payload = signal.as_dict()

    assert payload["concept"] == "vector-db"
    assert payload["ecosystem"] == "pypi"
    assert payload["self_momentum_30d"] == 21.0
    assert payload["self_adoption_week"] == 121
    assert payload["comparison"] == "faster"
    assert payload["peers"][0]["name"] == "chromadb-npm"
    assert payload["peers"][0]["momentum_30d"] == 5.0
    assert payload["peers"][0]["adoption_week"] == 105


# --- батч ---------------------------------------------------------------------


def test_compute_many_includes_only_mapped_packages() -> None:
    """В батче остаются пакеты с concept; без mapping — ключа нет вовсе."""
    rows = [row("vector-db", "pypi", "chromadb"), row("vector-db", "npm", "chromadb-npm")]
    observations = {
        "chromadb": history((0, 100), (25, 100)),
        "chromadb-npm": history((0, 100), (25, 100)),
        "requests": history((0, 100), (25, 100)),
    }

    result = compute_many(
        ["chromadb", "requests"],
        all_concept_packages=rows,
        observations_by_name=observations,
        package_ecosystems={"chromadb": "pypi"},
        now=NOW,
    )

    assert set(result) == {"chromadb"}
    assert result["chromadb"].peers[0].name == "chromadb-npm"


def test_compute_many_skips_package_without_ecosystem() -> None:
    """Экосистема неизвестна — пакет пропускается, а не падает."""
    result = compute_many(
        ["chromadb"],
        all_concept_packages=[row("vector-db", "pypi", "chromadb")],
        observations_by_name={},
        package_ecosystems={},
        now=NOW,
    )
    assert result == {}


def test_import_has_no_side_effects(tmp_path, monkeypatch) -> None:
    """Импорт модуля не пишет файлов и не ходит в сеть (правило §18.1).

    Проверяется повторным импортом из ``sys.modules`` с перехватом сокета:
    коллектор P4c вообще не должен знать про транспорт.
    """
    import importlib
    import socket

    def _blocked(*_args, **_kwargs):  # pragma: no cover - быть должно не вызваться
        raise AssertionError("cross_ecosystem открыл сокет при импорте")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "socket", _blocked)

    import core.collectors.cross_ecosystem as module

    importlib.reload(module)

    assert list(tmp_path.iterdir()) == []
