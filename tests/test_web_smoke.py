"""Smoke-тесты каркаса (шаг 0.1): миддлварь токена/Host и выбор порта.

Полноценные тесты метода появятся вместе с `core` (шаг 1). Здесь проверяем ровно
то, что обещано в DEVPLAN.md §11: без токена — 403, с чужим Origin — 403,
с токеном — 200; и что свободный порт действительно свободен.
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from app.web import TOKEN_COOKIE, TOKEN_HEADER, create_app
from desktop import free_port

TOKEN = "test-token-123"
PORT = 8799
HOST = f"127.0.0.1:{PORT}"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(token=TOKEN, port=PORT))


def test_health_requires_token(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Host": HOST})
    assert response.status_code == 403
    assert response.json()["ok"] is False


def test_health_with_header_token(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Host": HOST, TOKEN_HEADER: TOKEN})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": "0.1.0"}


def test_health_with_cookie_and_query_token(client: TestClient) -> None:
    cookie = client.get("/api/health", headers={"Host": HOST}, cookies={TOKEN_COOKIE: TOKEN})
    assert cookie.status_code == 200
    query = client.get(f"/api/health?token={TOKEN}", headers={"Host": HOST})
    assert query.status_code == 200


def test_foreign_host_and_origin_are_forbidden(client: TestClient) -> None:
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    foreign_origin = client.get(
        "/api/health",
        headers={"Host": HOST, TOKEN_HEADER: TOKEN, "Origin": "http://evil.example"},
    )
    assert foreign_origin.status_code == 403


def test_index_page_renders(client: TestClient) -> None:
    response = client.get("/", headers={"Host": HOST})
    assert response.status_code == 200
    assert "каркас работает" in response.text


def test_free_port_is_bindable() -> None:
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
