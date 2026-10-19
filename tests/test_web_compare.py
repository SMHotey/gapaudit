"""``GET /api/compare`` и ``POST /api/compare/export`` (шаг 3c).

Экран «Сравнение» — 2–3 пакета бок о бок. Эндпоинт только читает: данные
готовятся ``Store.record_observations`` — тем же методом, которым пишет хук
lifespan после задачи ``packages.check``.

Границы контракта, которые держат тесты:

* 400 — имён не 2–3 (после дедупликации и отбрасывания пустых кусков);
* 404 — ни одного имени нет в Store (пустая таблица выглядела бы сломанной);
* частичное совпадение — 200: найденные колонки в ``packages``, отсутствующие
  в ``missing``;
* поля пакета — полный набор (тот же, что у точки матрицы, плюс ``verdict``);
* «последнее наблюдение» — по ``measured_at``, а не по порядку вставки.

Сеть — ноль, подпроцессы — ноль.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import app_state, auth

PACKAGE_FIELDS = {
    "name",
    "source",
    "version",
    "released_at",
    "adoption_month",
    "adoption_week",
    "flag",
    "flag_label",
    "verdict",
    "measured_at",
}


def observation(
    name: str, *, week: int | None = 1000, month: int = 4000, flag: int = 0,
    version: str = "1.2.3", verdict: str = "живой",
) -> dict[str, Any]:
    """Словарь вида ``core.packages.check()`` — столько нужно ``Store``."""
    return {
        "name": name,
        "src": "pypi",
        "ver": version,
        "date": "2026-10-01",
        "summary": f"описание {name}",
        "home": f"https://example.invalid/{name}",
        "flag": flag,
        "month": month,
        "week": week,
        "verdict": verdict,
    }


def seed(
    client: TestClient, *observations: dict[str, Any], measured_at: str = "2026-10-19T10:00:00Z"
) -> None:
    store = app_state(client).store
    store.record_observations(list(observations), run_id="run-1", measured_at=measured_at)


def compare(client: TestClient, names: str) -> Any:
    return client.get("/api/compare", params={"names": names}, headers=auth())


# --- границы имён -------------------------------------------------------------


def test_empty_names_is_400(client: TestClient) -> None:
    """Пустой ``names`` — 400: сравнивать нечего."""
    response = compare(client, "")

    assert response.status_code == 400
    assert "names" in response.json()["detail"]


def test_one_name_is_400(client: TestClient) -> None:
    """Одно имя — не сравнение: 400 (даже если пакет в Store есть)."""
    seed(client, observation("requests"))

    response = compare(client, "requests")

    assert response.status_code == 400
    assert "2–3" in response.json()["detail"]


def test_four_names_is_400(client: TestClient) -> None:
    """Четыре имени — 400: четыре колонки в окно не влезают (DEVPLAN §9)."""
    response = compare(client, "a,b,c,d")

    assert response.status_code == 400


def test_repeated_names_are_deduplicated(client: TestClient) -> None:
    """``a,a,b`` — это два имени, а не 400: дедупликация до проверки границ."""
    seed(client, observation("a"), observation("b"))

    response = compare(client, "a, a, b")

    assert response.status_code == 200
    assert [package["name"] for package in response.json()["packages"]] == ["a", "b"]


# --- содержимое ---------------------------------------------------------------


def test_two_packages_returns_both_without_missing(client: TestClient) -> None:
    """Два проверенных пакета — две колонки, ``missing`` пуст."""
    seed(client, observation("requests"), observation("flask"))

    payload = compare(client, "requests,flask").json()

    assert [package["name"] for package in payload["packages"]] == ["requests", "flask"]
    assert payload["missing"] == []


def test_unknown_name_goes_to_missing(client: TestClient) -> None:
    """Третье имя без наблюдений — в ``missing``, две колонки остаются."""
    seed(client, observation("requests"), observation("flask"))

    payload = compare(client, "requests,flask,urllib3").json()

    assert [package["name"] for package in payload["packages"]] == ["requests", "flask"]
    assert payload["missing"] == ["urllib3"]


def test_no_known_packages_is_404(client: TestClient) -> None:
    """Пустая БД — 404: ноль колонок это не «сравнение», а сломанный экран."""
    response = compare(client, "requests,flask")

    assert response.status_code == 404


def test_package_has_full_field_set(client: TestClient) -> None:
    """Каждая колонка несёт полный набор полей — экран не гадает про ключи."""
    seed(client, observation("requests", flag=1, week=1234, month=5678))

    package = compare(client, "requests,flask").json()["packages"][0]

    assert set(package) == PACKAGE_FIELDS
    assert package["source"] == "pypi"
    assert package["version"] == "1.2.3"
    assert package["released_at"] == "2026-10-01"
    assert package["adoption_week"] == 1234
    assert package["adoption_month"] == 5678
    assert package["flag"] is True
    assert package["flag_label"] == "MCP"
    assert package["verdict"] == "живой"
    assert package["measured_at"] == "2026-10-19T10:00:00Z"


def test_latest_observation_wins(client: TestClient) -> None:
    """Два замера одного имени — в колонке последний по ``measured_at``.

    Проверяется именно adoption_week: он меняется от замера к замеру, и если
    эндпоинт возьмёт первое наблюдение, экран покажет устаревшую цифру.
    """
    seed(client, observation("requests", week=100, month=400))
    seed(
        client,
        observation("requests", week=999, month=4000),
        measured_at="2026-10-19T12:00:00Z",
    )
    seed(client, observation("flask", week=5))

    package = compare(client, "requests,flask").json()["packages"][0]

    assert package["adoption_week"] == 999
    assert package["measured_at"] == "2026-10-19T12:00:00Z"


# --- экспорт сравнения --------------------------------------------------------


def test_compare_export_csv_is_attachment_with_rows(client: TestClient) -> None:
    """CSV сравнения: attachment, BOM, шапка матрицы и строка на пакет."""
    seed(client, observation("requests", week=1234), observation("flask", week=5))

    response = client.post(
        "/api/compare/export",
        params={"format": "csv", "names": "requests,flask"},
        headers=auth(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('.csv"')
    assert response.content.startswith(b"\xef\xbb\xbf")

    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0][0] == "name"
    assert [row[0] for row in rows[1:]] == ["requests", "flask"]
    assert rows[1][rows[0].index("adoption_week")] == "1234"


def test_compare_export_rejects_unknown_format(client: TestClient) -> None:
    """``format=xml`` — 400: у сравнения только CSV."""
    seed(client, observation("requests"), observation("flask"))

    response = client.post(
        "/api/compare/export",
        params={"format": "xml", "names": "requests,flask"},
        headers=auth(),
    )

    assert response.status_code == 400
