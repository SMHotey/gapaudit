"""Smoke-тесты каркаса (шаг 0.1; контракт health пересмотрен в шаге 3b).

Что проверяется здесь: связка «клиент → миддлварь → экран» живая, контур
Host/Origin отбивает чужие запросы, свободный порт действительно свободен.

Контракт ``GET /api/health`` изменился в 3b: раньше эндпоинт требовал токен
и отдавал только ``{ok, version}``, теперь он безтокеновый (окно пингует его
до того, как узнаёт токен) и расширен (``gh_ok``/``db_ok``/``data_dir``) —
подробности в ``tests/test_web_health.py`` и ``tests/test_web_middleware.py``.
"""

from __future__ import annotations

import socket

from fastapi.testclient import TestClient

from desktop import free_port


def test_health_is_open_and_reports_version(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["version"] == "0.1.0"


def test_foreign_host_and_origin_are_forbidden(client: TestClient) -> None:
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403

    foreign_origin = client.get(
        "/api/health",
        headers={"Origin": "http://evil.example"},
    )
    assert foreign_origin.status_code == 403


def test_index_page_renders(client: TestClient) -> None:
    """Сборка: главный экран отдаётся и содержит каркас 2a (таблицу задач)."""
    response = client.get("/")

    assert response.status_code == 200
    assert '<h1><a href="/">GapAudit</a></h1>' in response.text
    assert "<tbody id=\"jobs-body\">" in response.text


def test_free_port_is_bindable() -> None:
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
