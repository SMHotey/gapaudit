"""``GET /api/packages/search`` (шаг 3a): поиск по имени/описанию + пагинация.

Контракт экрана «Пакеты»:

* ``q`` — подстрока без учёта регистра, ищется в ``name`` и ``summary``;
* пустой ``q`` — все пакеты (пагинация работает и без поиска);
* ``total`` — сколько всего под тем же ``q``, не длина страницы;
* ``limit`` (1..200) и ``offset`` (>=0) — 400 при нарушении.

Отдельно проверяется порядок маршрутов: ``/api/packages/search`` объявлен до
``/api/packages/{name}``, иначе слово «search» стало бы именем пакета и поиск
отвечал бы 404. Сеть — ноль, подпроцессы — ноль.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import app_state, auth


def observation(name: str, *, summary: str) -> dict:
    """Словарь вида ``core.packages.check()`` — столько нужно ``Store``."""
    return {
        "name": name,
        "src": "pypi",
        "ver": "1.0.0",
        "date": "2026-10-01",
        "summary": summary,
        "home": f"https://example.invalid/{name}",
        "flag": False,
        "month": 100,
        "week": 20,
        "verdict": "живой",
    }


def seed(client: TestClient) -> None:
    """requests, urllib3, flask — с разными summary и датами замера."""
    store = app_state(client).store
    store.record_observations(
        [
            observation("requests", summary="HTTP for Humans"),
            observation("urllib3", summary="HTTP library with thread-safe pools"),
            observation("flask", summary="A simple framework for web apps"),
        ],
        run_id="run-1",
        measured_at="2026-10-19T10:00:00Z",
    )


def test_empty_store_returns_empty_page(client: TestClient) -> None:
    response = client.get("/api/packages/search", headers=auth())

    assert response.status_code == 200
    assert response.json() == {
        "packages": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
        "q": "",
    }


def test_empty_query_returns_all_packages(client: TestClient) -> None:
    seed(client)

    payload = client.get("/api/packages/search?q=", headers=auth()).json()

    assert payload["total"] == 3
    assert {package["name"] for package in payload["packages"]} == {
        "requests", "urllib3", "flask",
    }
    # строка поиска отдаёт только нужные экрану поля
    assert set(payload["packages"][0]) == {"name", "source", "summary", "last_seen"}


def test_search_by_name_and_summary_case_insensitive(client: TestClient) -> None:
    seed(client)

    def names(q: str) -> list[str]:
        return [
            package["name"]
            for package in client.get(
                "/api/packages/search", params={"q": q}, headers=auth()
            ).json()["packages"]
        ]

    assert names("req") == ["requests"]
    assert names("REQ") == ["requests"]
    assert names("lib") == ["urllib3"]        # «library» — только в summary
    assert names("zzz") == []


def test_q_is_echoed_and_trimmed(client: TestClient) -> None:
    seed(client)

    payload = client.get(
        "/api/packages/search", params={"q": "  flask  "}, headers=auth()
    ).json()

    assert payload["q"] == "flask"
    assert payload["total"] == 1


def test_pagination_keeps_total(client: TestClient) -> None:
    seed(client)

    first = client.get(
        "/api/packages/search?limit=2&offset=0", headers=auth()
    ).json()
    second = client.get(
        "/api/packages/search?limit=2&offset=2", headers=auth()
    ).json()

    assert len(first["packages"]) == 2 and first["total"] == 3
    assert len(second["packages"]) == 1 and second["total"] == 3
    assert not {p["name"] for p in first["packages"]} & {
        p["name"] for p in second["packages"]
    }


def test_limit_over_max_is_400(client: TestClient) -> None:
    response = client.get("/api/packages/search?limit=201", headers=auth())

    assert response.status_code == 400
    assert "limit" in response.json()["detail"]


def test_negative_offset_is_400(client: TestClient) -> None:
    response = client.get("/api/packages/search?offset=-5", headers=auth())

    assert response.status_code == 400
    assert "offset" in response.json()["detail"]


def test_search_route_wins_over_package_name(client: TestClient) -> None:
    """``/search`` — свой маршрут, а не карточка пакета с именем «search».

    Если роут объявить после ``/api/packages/{name}``, FastAPI отдаст 404
    «пакет search не найден» — экран поиска молча останется пустым.
    """
    seed(client)

    response = client.get("/api/packages/search?q=req", headers=auth())

    assert response.status_code == 200
    assert response.json()["total"] == 1
