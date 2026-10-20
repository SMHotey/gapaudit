"""``GET /api/signals/{package}`` (P4b: momentum, P4c: cross-ecosystem).

Эндпоинт — точка расширения на P5: ``signals_available`` перечисляет
посчитанное сейчас, ``signals_unavailable`` — то, что появится позже. Разница
между «сигнала нет» и «сигнал ещё не сделан» видна клиенту без знания
календаря шагов (§18.3). В P4c к momentum добавлен ``cross_ecosystem`` — он
доступен только при заданном concept, и без concept уходит в unavailable.

Сеть — ноль, подпроцессы — ноль: данные готовятся ``Store``, эндпоинт только
читает.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import app_state, auth

PENDING = ["cross_ecosystem", "jobs", "patents", "funding", "search_interest"]


def observation(name: str, *, week: int = 100) -> dict[str, Any]:
    """Строка вида ``core.packages.check()`` — столько нужно ``Store``."""
    return {
        "name": name,
        "src": "pypi",
        "ver": "1.0.0",
        "date": "2026-10-01",
        "summary": f"описание {name}",
        "home": f"https://example.invalid/{name}",
        "flag": 0,
        "month": week * 4,
        "week": week,
        "verdict": "живой",
    }


def days_ago_iso(days: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def seed_at(client: TestClient, days: int, *observations: dict[str, Any]) -> None:
    app_state(client).store.record_observations(
        list(observations), run_id=f"run-{days}", measured_at=days_ago_iso(days)
    )


def signals(client: TestClient, name: str) -> dict[str, Any]:
    response = client.get(f"/api/signals/{name}", headers=auth())
    assert response.status_code == 200, response.text
    return response.json()


def test_unknown_package_is_404(client: TestClient) -> None:
    """Имени нет ни в observations, ни в packages — 404, а не пустой сигнал."""
    response = client.get("/api/signals/nonexistent", headers=auth())

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_single_measurement_gives_no_delta(client: TestClient) -> None:
    """Один замер: сигнал есть, дельты нет — честное «мало данных»."""
    seed_at(client, 0, observation("requests", week=1000))

    payload = signals(client, "requests")

    assert payload["package"] == "requests"
    assert payload["momentum"]["samples"] == 1
    assert payload["momentum"]["delta_30d"] is None
    assert payload["momentum"]["window_30d_ok"] is False
    assert payload["momentum"]["label_30d"] == "—"
    assert payload["momentum"]["color"] == "gray"
    assert payload["momentum"]["latest_week"] == 1000


def test_three_measurements_over_month_compute_delta_and_color(
    client: TestClient,
) -> None:
    """100 -> 110 -> 121 за 30 дней: Δ30 = +21%, цвет зелёный."""
    seed_at(client, 25, observation("requests", week=100))
    seed_at(client, 20, observation("requests", week=110))
    seed_at(client, 0, observation("requests", week=121))

    momentum = signals(client, "requests")["momentum"]

    assert momentum["samples"] == 3
    assert momentum["delta_30d"] == pytest.approx(21.0)
    assert momentum["color"] == "green"
    assert momentum["label_30d"] == "↑ +21.0%/30д"


def test_available_and_unavailable_lists(client: TestClient) -> None:
    """В P4b доступен только momentum; остальные перечислены как будущие."""
    seed_at(client, 0, observation("requests", week=100))

    payload = signals(client, "requests")

    assert payload["signals_available"] == ["momentum"]
    assert payload["signals_unavailable"] == PENDING
    # gap_score — агрегат, а не сигнал: в списках его нет ни как готового, ни
    # как запланированного (§18.3).
    assert "gap_score" not in payload["signals_unavailable"]
    assert "gap_score" not in payload["signals_available"]



# --- cross-ecosystem (шаг P4c) ------------------------------------------------


def seed_concept(
    client: TestClient, concept: str = "vector-db", packages: list[tuple[str, str]] | None = None
) -> None:
    """Записать mapping ``[(ecosystem, package), ...]`` тем же API, что и старт."""
    store = app_state(client).store
    for ecosystem, package in packages or [("pypi", "chromadb"), ("npm", "chromadb-npm")]:
        store.record_concept_package(
            concept=concept, ecosystem=ecosystem, package=package
        )


def test_cross_ecosystem_present_with_two_peers(client: TestClient) -> None:
    """Concept с пакетом и двумя соседями: сигнал посчитан, peers — двое."""
    seed_at(client, 25, observation("chromadb", week=100))
    seed_at(client, 0, observation("chromadb", week=121))
    seed_at(client, 0, observation("chromadb-npm", week=105))
    seed_at(client, 0, observation("chroma-core/chroma", week=110))
    seed_concept(
        client,
        packages=[
            ("pypi", "chromadb"),
            ("npm", "chromadb-npm"),
            ("github", "chroma-core/chroma"),
        ],
    )

    payload = signals(client, "chromadb")
    cross = payload["cross_ecosystem"]

    assert cross is not None
    assert cross["concept"] == "vector-db"
    assert cross["ecosystem"] == "pypi"
    assert len(cross["peers"]) == 2
    assert {peer["ecosystem"] for peer in cross["peers"]} == {"npm", "github"}
    # свои значения — для карточки (3 полосы), а не только соседи
    assert cross["self_momentum_30d"] == 21.0
    assert cross["self_adoption_week"] == 121
    assert payload["signals_available"] == ["momentum", "cross_ecosystem"]


def test_package_without_concept_has_no_cross_signal(client: TestClient) -> None:
    """Нет concept — ``cross_ecosystem=None``, имя уходит в unavailable."""
    seed_at(client, 0, observation("requests", week=100))

    payload = signals(client, "requests")

    assert payload["cross_ecosystem"] is None
    assert payload["signals_available"] == ["momentum"]
    assert "cross_ecosystem" in payload["signals_unavailable"]


def test_unavailable_lists_only_missing_signals(client: TestClient) -> None:
    """С посчитанным cross-ecosystem в unavailable его уже нет (и gap_score нет)."""
    seed_at(client, 0, observation("chromadb", week=100))
    seed_concept(client)

    payload = signals(client, "chromadb")

    assert payload["signals_available"] == ["momentum", "cross_ecosystem"]
    assert payload["signals_unavailable"] == [
        "jobs",
        "patents",
        "funding",
        "search_interest",
    ]
    assert "gap_score" not in payload["signals_unavailable"]


def test_peer_without_history_has_null_momentum(client: TestClient) -> None:
    """Сосед есть в mapping, истории нет: ``momentum_30d=None`` — честное «мало
    данных», а не 0%."""
    seed_at(client, 0, observation("chromadb", week=100))
    seed_concept(client)

    cross = signals(client, "chromadb")["cross_ecosystem"]

    assert cross is not None
    assert len(cross["peers"]) == 1
    peer = cross["peers"][0]
    assert peer["name"] == "chromadb-npm"
    assert peer["momentum_30d"] is None
    assert peer["adoption_week"] is None
    assert cross["comparison"] == "unknown"


def test_self_ecosystem_follows_observation_source(client: TestClient) -> None:
    """Пакет, замапленный в обе экосистемы, показывает себя под своим источником.

    Наблюдение ``src='pypi'`` → ``ecosystem='pypi'``, сосед — npm-строка того же
    имени. Без выбора по источнику первая строка mapping (npm идёт раньше pypi
    в ORDER BY) показала бы pypi-пакет под npm-полосой — это ловил живой прогон
    P4c на chromadb.
    """
    seed_at(client, 25, observation("chromadb", week=100))
    seed_at(client, 0, observation("chromadb", week=130))
    seed_concept(
        client,
        packages=[("pypi", "chromadb"), ("npm", "chromadb")],
    )

    cross = signals(client, "chromadb")["cross_ecosystem"]

    assert cross is not None
    assert cross["ecosystem"] == "pypi"
    assert [peer["ecosystem"] for peer in cross["peers"]] == ["npm"]
    assert cross["self_momentum_30d"] == 30.0
