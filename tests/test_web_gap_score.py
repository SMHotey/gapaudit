"""``GET /api/gap-score`` (шаг P5e): Gap Score одного-нескольких пакетов.

Эндпоинт нужен карточке и Niche Finder (P5f): score пакета, посчитанный по
ТОЙ ЖЕ выборке, что у матрицы. Поэтому главная проверка здесь — не «число
есть», а «число совпадает с точкой матрицы»: если нормировка считается по
запрошенным именам, «87» в ответе расходится с «87» на экране, и метрика
перестаёт быть сравнимой.

Сеть — ноль, подпроцессы — ноль.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import app_state, auth


def observation(name: str, *, week: int = 1000, flag: int = 0) -> dict[str, Any]:
    """Словарь вида ``core.packages.check()`` — столько нужно ``Store``."""
    return {
        "name": name,
        "src": "pypi",
        "ver": "1.2.3",
        "date": "2026-10-01",
        "summary": f"описание {name}",
        "home": f"https://example.invalid/{name}",
        "flag": flag,
        "month": week * 4,
        "week": week,
        "verdict": "живой",
    }


def seed(client: TestClient, *observations: dict[str, Any]) -> None:
    app_state(client).store.record_observations(
        list(observations), run_id="run-1", measured_at="2026-10-19T10:00:00Z"
    )


def gap_score(client: TestClient, names: str) -> Any:
    return client.get(f"/api/gap-score?names={names}", headers=auth())


def test_gap_score_requires_token(client: TestClient) -> None:
    """Без токена — 403: эндпоинт живёт под той же миддлварью, что остальные."""
    seed(client, observation("a"), observation("b"))

    assert client.get("/api/gap-score?names=a,b").status_code == 403


def test_empty_names_is_400(client: TestClient) -> None:
    """Пустой ``names`` — 400: «ни одного пакета» не бывает осмысленным запросом."""
    response = gap_score(client, "")

    assert response.status_code == 400
    assert "names" in response.json()["detail"]


def test_single_name_is_400(client: TestClient) -> None:
    """Одно имя — 400 (нужно 2–3, как у ``/api/compare``)."""
    seed(client, observation("solo"))

    assert gap_score(client, "solo").status_code == 400


def test_four_names_are_400(client: TestClient) -> None:
    """Четыре имени — 400: верхняя граница та же, что у сравнения."""
    assert gap_score(client, "a,b,c,d").status_code == 400


def test_two_names_give_two_scores(client: TestClient) -> None:
    """Два имени — 200 и два score с полным набором полей."""
    seed(
        client,
        observation("pkg-a", week=500, flag=0),
        observation("pkg-b", week=1000, flag=1),
    )

    response = gap_score(client, "pkg-a,pkg-b")

    assert response.status_code == 200
    payload = response.json()
    assert [item["name"] for item in payload["scores"]] == ["pkg-a", "pkg-b"]
    assert payload["missing"] == []
    for item in payload["scores"]:
        assert item["score"] is not None
        assert 0.0 <= item["score"] <= 100.0
        assert item["components_available"] == 2
        assert item["components_total"] == 7
        assert set(item["weights_used"]) == {"adoption", "no_flag"}
        assert set(item["components"]) == {
            "adoption", "momentum", "no_flag", "funding",
            "patents", "jobs", "search",
        }


def test_score_matches_matrix_point(client: TestClient) -> None:
    """Score из ``/api/gap-score`` совпадает с точкой матрицы (одна нормировка).

    Это и есть контракт метрики: сравнение пакетов между экранами. Если
    нормализатор фитится по двум запрошенным именам, «pkg-a» получил бы 0.0
    здесь и 0.44 на матрице (там в выборке третий пакет).
    """
    seed(
        client,
        observation("pkg-a", week=100, flag=0),
        observation("pkg-b", week=1000, flag=1),
        observation("pkg-c", week=500, flag=0),
    )
    matrix_points = {
        point["name"]: point
        for point in client.get("/api/matrix", headers=auth()).json()["points"]
    }

    payload = gap_score(client, "pkg-a,pkg-c").json()

    for item in payload["scores"]:
        assert item["score"] == pytest.approx(matrix_points[item["name"]]["gap_score"])
        assert item["components"]["adoption"] == pytest.approx(
            matrix_points[item["name"]]["gap_breakdown"]["adoption"]
        )


def test_missing_names_are_reported(client: TestClient) -> None:
    """Несуществующее имя — в ``missing``, а не 404 (второй пакет мог не проверяться)."""
    seed(client, observation("known"), observation("other"))

    response = gap_score(client, "known,unknown")

    assert response.status_code == 200
    payload = response.json()
    assert [item["name"] for item in payload["scores"]] == ["known"]
    assert payload["missing"] == ["unknown"]


def test_all_missing_is_404(client: TestClient) -> None:
    """Ни одного известного имени — 404: пустой список score выглядел бы поломкой."""
    seed(client, observation("known"), observation("other"))

    response = gap_score(client, "ghost-1,ghost-2")

    assert response.status_code == 404


def test_card_reports_same_gap_score(client: TestClient) -> None:
    """Карточка ``/api/packages/{name}`` отдаёт то же число и breakdown.

    Без этого блок «Gap Score» в карточке рисовался бы из ничего: card.js
    читает ``gap_score`` верхнего уровня ответа.
    """
    seed(
        client,
        observation("pkg-a", week=100, flag=0),
        observation("pkg-b", week=1000, flag=0),
    )
    point = next(
        item
        for item in client.get("/api/matrix", headers=auth()).json()["points"]
        if item["name"] == "pkg-b"
    )

    card = client.get("/api/packages/pkg-b", headers=auth()).json()

    assert card["gap_score"] == pytest.approx(point["gap_score"])
    assert card["gap_breakdown"]["components_available"] == 2
    assert card["gap_breakdown"]["adoption"] == pytest.approx(1.0)
