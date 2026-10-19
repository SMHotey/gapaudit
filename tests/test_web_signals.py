"""``GET /api/signals/{package}`` (шаг P4b): конверт сигналов, пока momentum.

Эндпоинт — точка расширения на P4c/P5: ``signals_available`` перечисляет
посчитанное сейчас, ``signals_unavailable`` — то, что появится позже. Разница
между «сигнала нет» и «сигнал ещё не сделан» видна клиенту без знания
календаря шагов (§18.3).

Сеть — ноль, подпроцессы — ноль: данные готовятся ``Store``, эндпоинт только
читает.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import app_state, auth

PENDING = ["cross_eco", "jobs", "patents", "funding", "search_interest"]


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
