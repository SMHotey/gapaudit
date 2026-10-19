"""``/api/packages`` (шаг 3b): read-only чтение Store, без его изменений.

Данные готовятся через ``Store.record_observations``/``record_events`` — ровно
тем API, которым пользуется шаг 2; эндпоинты только читают. События в
карточке фильтруются по имени в ``app/web.py`` (Store не менялся — шаг 9).

Сеть — ноль, подпроцессы — ноль.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import app_state, auth


def observation(name: str, *, week: int = 40, month: int = 120, flag: int = 1) -> dict:
    """Словарь вида ``core.packages.check()`` — столько нужно ``Store``."""
    return {
        "name": name,
        "src": "pypi",
        "ver": "1.2.3",
        "date": "2026-10-01",
        "summary": f"описание {name}",
        "home": f"https://example.invalid/{name}",
        "flag": flag,
        "month": month,
        "week": week,
        "verdict": "живой",
    }


def seed(client: TestClient) -> None:
    """alpha и beta с наблюдениями, gamma — пакет без наблюдений."""
    store = app_state(client).store
    store.record_observations(
        [observation("alpha"), observation("beta")],
        run_id="run-1",
        measured_at="2026-10-19T10:00:00Z",
    )
    # пакет без наблюдений: вставка строки packages напрямую (isolation_level=None
    # — автокоммит; публичного API «просто добавить пакет» у Store нет)
    assert store.conn is not None
    store.conn.execute(
        "INSERT INTO packages(name, source, url, summary, first_seen, last_seen)"
        " VALUES('gamma', 'npm', NULL, NULL, '2026-10-19T10:00:00Z',"
        " '2026-10-19T10:00:00Z')"
    )


def test_empty_store_returns_empty_list(client: TestClient) -> None:
    response = client.get("/api/packages", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"packages": []}


def test_list_returns_all_packages(client: TestClient) -> None:
    seed(client)

    packages = client.get("/api/packages", headers=auth()).json()["packages"]

    assert {package["name"] for package in packages} == {"alpha", "beta", "gamma"}


def test_only_with_observations_filter(client: TestClient) -> None:
    seed(client)

    packages = client.get(
        "/api/packages?only_with_observations=true", headers=auth()
    ).json()["packages"]

    assert {package["name"] for package in packages} == {"alpha", "beta"}


def test_list_respects_limit(client: TestClient) -> None:
    seed(client)

    packages = client.get("/api/packages?limit=1", headers=auth()).json()["packages"]

    assert len(packages) == 1


def test_package_card_with_observations_and_events(client: TestClient) -> None:
    seed(client)
    store = app_state(client).store
    store.record_events(
        [
            {"kind": "release", "name": "alpha", "detail": "1.2.3"},
            {"kind": "new", "name": "beta", "detail": None},
        ],
        run_id="run-1",
        at="2026-10-19T11:00:00Z",
    )

    response = client.get("/api/packages/alpha", headers=auth())
    other = client.get("/api/packages/beta", headers=auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["package"]["name"] == "alpha"
    assert [item["name"] for item in payload["observations"]] == ["alpha"]
    # события отфильтрованы по имени здесь, в web.py
    assert [event["kind"] for event in payload["events"]] == ["release"]
    assert other.json()["events"][0]["kind"] == "new"


def test_unknown_package_is_404(client: TestClient) -> None:
    card = client.get("/api/packages/neti-est", headers=auth())
    history = client.get("/api/packages/neti-est/history", headers=auth())

    assert card.status_code == 404
    assert history.status_code == 404


def test_history_limit(client: TestClient) -> None:
    store = app_state(client).store
    store.record_observations(
        [observation("alpha", week=10), observation("alpha", week=20)],
        run_id="run-1",
        measured_at="2026-10-18T10:00:00Z",
    )
    store.record_observations(
        [observation("alpha", week=30)],
        run_id="run-2",
        measured_at="2026-10-19T10:00:00Z",
    )

    history = client.get(
        "/api/packages/alpha/history?limit=2", headers=auth()
    ).json()["observations"]
    all_items = client.get("/api/packages/alpha/history", headers=auth()).json()[
        "observations"
    ]

    assert len(history) == 2
    assert len(all_items) == 3
    assert history[0]["measured_at"] >= history[1]["measured_at"]
