"""Матрица токена и контура Host/Origin (шаг 3b, ``app/web.py``).

Матрица (из промта шага 3b):

* ``Host`` — только ``127.0.0.1:<port>`` / ``localhost:<port>``, иначе 403;
* ``Origin`` (если есть) — только ``http(s)://127.0.0.1:<port>`` /
  ``http(s)://localhost:<port>``, иначе 403;
* ``GET /api/*`` — header, cookie или ``?token=``;
* ``POST/PUT/DELETE /api/*`` — header или cookie, ``?token=`` не принимается;
* ``GET /api/stream`` — header, cookie или query (EventSource без заголовков);
* ``GET /api/health`` и ``GET /`` — без токена.

Сеть — ноль; ``Host`` задаёт ``base_url`` клиента (conftest), чужие значения
подставляются явными заголовками.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.web import TOKEN_QUERY
from tests.conftest import PORT, TOKEN, auth, cookie_header

VALID_JOB = {"kind": "monitor.diff", "params": {"days": 7}}


# --- GET /api/*: три места для токена ----------------------------------------


def test_get_jobs_with_header_token(client: TestClient) -> None:
    response = client.get("/api/jobs", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"jobs": []}


def test_get_jobs_with_cookie_token(client: TestClient) -> None:
    response = client.get("/api/jobs", headers=cookie_header())

    assert response.status_code == 200


def test_get_jobs_with_query_token(client: TestClient) -> None:
    response = client.get(f"/api/jobs?{TOKEN_QUERY}={TOKEN}")

    assert response.status_code == 200


def test_get_jobs_without_token_is_forbidden(client: TestClient) -> None:
    response = client.get("/api/jobs")

    assert response.status_code == 403
    assert "токен" in response.json()["detail"]


def test_get_jobs_with_wrong_token_is_forbidden(client: TestClient) -> None:
    response = client.get("/api/jobs", headers=auth("wrong-token"))

    assert response.status_code == 403


# --- мутации: query не принимается -------------------------------------------


def test_post_jobs_with_header_token_accepted(client: TestClient) -> None:
    response = client.post("/api/jobs", json=VALID_JOB, headers=auth())

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_post_jobs_with_cookie_token_accepted(client: TestClient) -> None:
    response = client.post("/api/jobs", json=VALID_JOB, headers=cookie_header())

    assert response.status_code == 202


def test_post_jobs_with_query_token_is_forbidden(client: TestClient) -> None:
    response = client.post(f"/api/jobs?{TOKEN_QUERY}={TOKEN}", json=VALID_JOB)

    assert response.status_code == 403


def test_delete_takes_token_from_header_not_query(client: TestClient) -> None:
    # DELETE-роута нет, но миддлварь работает до маршрутизации:
    # query-токен = 403, заголовок = миддлварь пропустил (405 от роутера).
    forbidden = client.delete(f"/api/jobs/abc/cancel?{TOKEN_QUERY}={TOKEN}")
    passed = client.delete("/api/jobs/abc/cancel", headers=auth())

    assert forbidden.status_code == 403
    assert passed.status_code == 405


# --- resume: заглушка 501 (шаг 3c) -------------------------------------------


def test_resume_with_header_token_is_501(client: TestClient) -> None:
    """Метод методом есть (шаг 3c), продолжать пока нечего — 501 (3d)."""
    response = client.post(f"/api/jobs/{'a' * 32}/resume", headers=auth())

    assert response.status_code == 501
    assert "3d" in response.json()["detail"]


def test_resume_without_token_is_forbidden(client: TestClient) -> None:
    response = client.post(f"/api/jobs/{'a' * 32}/resume")

    assert response.status_code == 403
    assert "токен" in response.json()["detail"]


def test_resume_takes_token_from_header_not_query(client: TestClient) -> None:
    """``/resume`` — мутация: query-токен миддлварь не принимает."""
    forbidden = client.post(f"/api/jobs/{'a' * 32}/resume?{TOKEN_QUERY}={TOKEN}")
    passed = client.post(f"/api/jobs/{'a' * 32}/resume", headers=auth())

    assert forbidden.status_code == 403
    assert passed.status_code == 501


# --- SSE: все три места, включая query ---------------------------------------


def _stream_status(client: TestClient, headers: dict[str, str] | None = None, url: str = "/api/stream"):
    with client.stream("GET", url, headers=headers or {}) as response:
        return response.status_code, dict(response.headers)


def test_stream_with_header_token(client: TestClient) -> None:
    status, headers = _stream_status(client, auth())

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")


def test_stream_with_cookie_token(client: TestClient) -> None:
    status, _headers = _stream_status(client, cookie_header())

    assert status == 200


def test_stream_with_query_token(client: TestClient) -> None:
    status, headers = _stream_status(client, url=f"/api/stream?{TOKEN_QUERY}={TOKEN}")

    assert status == 200
    assert headers["cache-control"] == "no-cache"
    assert headers["x-accel-buffering"] == "no"


def test_stream_without_token_is_forbidden(client: TestClient) -> None:
    response = client.get("/api/stream")

    assert response.status_code == 403


# --- без токена: health и главный экран --------------------------------------


def test_health_without_token_is_200(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_index_without_token_is_200(client: TestClient) -> None:
    """Экран шага 3c: список задач, счётчик и кнопка обновления."""
    response = client.get("/")

    assert response.status_code == 200
    assert "GapAudit" in response.text
    assert 'id="counter"' in response.text  # индикатор «N задач (M в работе)»
    assert 'id="refresh"' in response.text  # кнопка «Обновить»


# --- Host / Origin -----------------------------------------------------------


def test_foreign_host_is_forbidden(client: TestClient) -> None:
    api = client.get("/api/jobs", headers={**auth(), "Host": "evil.example"})
    page = client.get("/", headers={"Host": "evil.example"})

    assert api.status_code == 403
    assert page.status_code == 403
    assert "Host" in api.json()["detail"]


def test_foreign_origin_is_forbidden(client: TestClient) -> None:
    response = client.get(
        "/api/jobs",
        headers={**auth(), "Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert "Origin" in response.json()["detail"]


def test_own_origin_and_localhost_host_are_allowed(client: TestClient) -> None:
    origin = client.get("/api/health", headers={"Origin": f"http://127.0.0.1:{PORT}"})
    localhost = client.get("/api/health", headers={"Host": f"localhost:{PORT}"})

    assert origin.status_code == 200
    assert localhost.status_code == 200
