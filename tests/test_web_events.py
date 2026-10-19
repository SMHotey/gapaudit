"""``GET /api/events`` (шаг 3a): таймлайн «Мониторинга» с фильтрами и пагинацией.

Данные готовятся через ``Store.record_events`` — тем же API, которым пишет шаг
3d-b; эндпоинт только читает. Проверяется контракт ответа:

* ``events`` — страница ``limit``/``offset`` по ``at DESC, id DESC``;
* ``total`` — сколько всего под теми же фильтрами (не длина страницы);
* ``since``/``kind``/``name`` — фильтры, пустые значения = без фильтра;
* ``limit`` больше 500 и ``offset`` меньше 0 — 400, а не 422 и не молчаливый
  срез: экран различает «плохой запрос» и «тело не по схеме».

Сеть — ноль, подпроцессы — ноль (клиент из conftest).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import app_state, auth


def seed_events(client: TestClient) -> None:
    """2 релиза requests + 1 новый flask — база из промта шага."""
    store = app_state(client).store
    store.record_events(
        [
            {"kind": "release", "name": "requests", "detail": "2.32.0"},
            {"kind": "release", "name": "requests", "detail": "2.32.1"},
            {"kind": "new", "name": "flask", "detail": None},
        ],
        run_id="run-1",
        at="2026-10-19T10:00:00Z",
    )


def test_empty_store_returns_empty_timeline(client: TestClient) -> None:
    response = client.get("/api/events", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"events": [], "total": 0, "limit": 100, "offset": 0}


def test_filters_name_kind_and_their_combination(client: TestClient) -> None:
    seed_events(client)

    everything = client.get("/api/events", headers=auth()).json()
    by_name = client.get("/api/events?name=requests", headers=auth()).json()
    by_kind = client.get("/api/events?kind=release", headers=auth()).json()
    both = client.get(
        "/api/events?kind=release&name=requests", headers=auth()
    ).json()
    flask = client.get("/api/events?name=flask", headers=auth()).json()

    assert everything["total"] == 3 and len(everything["events"]) == 3
    assert by_name["total"] == 2
    assert by_kind["total"] == 2
    assert both["total"] == 2
    assert flask["total"] == 1
    assert flask["events"][0]["kind"] == "new"
    # поля события — контракт карточки и таймлайна
    assert set(flask["events"][0]) == {"id", "kind", "name", "detail", "at", "run_id"}


def test_limit_trims_page_but_total_counts_all(client: TestClient) -> None:
    """``total`` не зависит от ``limit``: иначе «2 события» на экране при трёх."""
    seed_events(client)

    page = client.get("/api/events?limit=2", headers=auth()).json()

    assert len(page["events"]) == 2
    assert page["total"] == 3
    assert page["limit"] == 2


def test_offset_returns_second_page(client: TestClient) -> None:
    seed_events(client)

    page = client.get("/api/events?limit=2&offset=2", headers=auth()).json()

    assert len(page["events"]) == 1
    assert page["total"] == 3
    assert page["offset"] == 2


def test_empty_filter_values_mean_no_filter(client: TestClient) -> None:
    """Экран отправляет ``kind=`` и ``name=`` всегда — это не фильтр «пустое имя»."""
    seed_events(client)

    response = client.get("/api/events?kind=&name=&since=", headers=auth())

    assert response.status_code == 200
    assert response.json()["total"] == 3


def test_since_filter(client: TestClient) -> None:
    seed_events(client)

    later = client.get(
        "/api/events?since=2027-01-01T00:00:00Z", headers=auth()
    ).json()

    assert later["total"] == 0
    assert later["events"] == []


def test_limit_over_max_is_400(client: TestClient) -> None:
    response = client.get("/api/events?limit=1000", headers=auth())

    assert response.status_code == 400
    assert "limit" in response.json()["detail"]


def test_negative_offset_is_400(client: TestClient) -> None:
    negative = client.get("/api/events?offset=-1", headers=auth())
    non_numeric = client.get("/api/events?limit=abc", headers=auth())

    assert negative.status_code == 400
    assert "offset" in negative.json()["detail"]
    assert non_numeric.status_code == 400
    assert "limit" in non_numeric.json()["detail"]


def test_events_require_token(client: TestClient) -> None:
    """Новый эндпоинт закрыт миддлварью, как и остальные ``/api/*``."""
    assert client.get("/api/events").status_code == 403
