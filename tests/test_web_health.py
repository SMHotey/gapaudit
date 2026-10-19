"""``GET /api/health`` (шаг 3b): расширенный ответ и кэш ``gh_ok``.

Ответ без токена (окно стартует раньше, чем знает токен), но внутри контура
Host/Origin — матрица в ``tests/test_web_middleware.py``.

``gh_ok`` не должен стоить подпроцесс ``gh auth status`` на каждый пинг: TTL —
60 секунд, время в тесте сдвигается, а не пережидается.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import GhProbe, app_state


def test_health_reports_all_fields(
    client: TestClient, gh_probe: GhProbe
) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["version"] == "0.1.0"
    assert payload["gh_ok"] is True
    assert payload["db_ok"] is True  # Store открыт lifespan'ом
    data_dir = Path(payload["data_dir"])
    assert data_dir.is_absolute()
    assert data_dir.is_dir()
    assert gh_probe.calls <= 1


def test_gh_ok_is_cached_for_60_seconds(
    client: TestClient, gh_probe: GhProbe
) -> None:
    client.get("/api/health")
    client.get("/api/health")

    assert gh_probe.calls == 1, "второй пинг не должен гонять gh"

    cache = app_state(client).gh_status
    base = cache.clock()
    cache.clock = lambda: base + 61.0  # TTL прошёл — можно проверять снова

    client.get("/api/health")

    assert gh_probe.calls == 2


def test_gh_ok_is_none_when_check_fails(
    client: TestClient, gh_probe: GhProbe
) -> None:
    gh_probe.error = RuntimeError("gh недоступен")

    payload = client.get("/api/health").json()

    assert payload["ok"] is True  # health живёт, даже когда gh не ответил
    assert payload["gh_ok"] is None
    assert gh_probe.calls == 1

    client.get("/api/health")

    assert gh_probe.calls == 1, "ошибка тоже кэшируется на TTL"
