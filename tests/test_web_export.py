"""``POST /api/events/export`` и ``POST /api/matrix/export`` (шаг 3c).

Выгрузка — файл в ответе (``Content-Disposition: attachment``), поэтому тесты
проверяют три вещи, которые ломаются тихо:

* тип и заголовки ответа (без ``attachment`` браузер откроет CSV вкладкой);
* BOM ``utf-8-sig`` — без него Excel показывает кириллицу как cp1251
  (тот же дефект, что ловили у экспорта каталога в 3b);
* состав строк: экспорт отдаёт **все** события под фильтрами и **ровно те**
  точки, что на матрице (``_matrix_points`` общий с ``GET /api/matrix``).

Данные готовятся ``Store.record_events`` / ``Store.record_observations`` — тем
же API, которым пишет хук lifespan. Сеть — ноль, подпроцессы — ноль.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import app_state, auth

MATRIX_HEADER = [
    "name",
    "source",
    "version",
    "released_at",
    "adoption_week",
    "adoption_month",
    "flag",
    "verdict",
    "measured_at",
]

# Шапка экспорта матрицы с P4c: momentum_30d десятой колонкой (§0.1 шага).
# С P5f-1 одиннадцатая — gap_score (то же число, что размер точки на экране).
# Сравнение (/api/compare/export) использует прежние девять — MATRIX_HEADER.
MATRIX_EXPORT_HEADER = MATRIX_HEADER + ["momentum_30d", "gap_score"]


def observation(
    name: str, *, week: int | None = 1000, month: int = 4000, flag: int = 0
) -> dict[str, Any]:
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


def seed_events(client: TestClient) -> None:
    """Пять событий двух видов: 3 release и 2 flag_appeared."""
    store = app_state(client).store
    store.record_events(
        [
            {"kind": "release", "name": "requests", "detail": "1.2.3 → 1.3.0"},
            {"kind": "release", "name": "flask", "detail": "2.0 → 2.1"},
            {"kind": "release", "name": "urllib3", "detail": "1.0 → 1.1"},
            {"kind": "flag_appeared", "name": "ruff", "detail": "MCP в описании"},
            {"kind": "flag_appeared", "name": "uv", "detail": "MCP в README"},
        ],
        run_id="run-1",
        at="2026-10-19T10:00:00Z",
    )


def seed_observations(client: TestClient) -> None:
    store = app_state(client).store
    store.record_observations(
        [
            observation("requests", week=1234),
            observation("flask", week=5, flag=1),
            # нулевой adoption — не точка матрицы (log10(0) не существует)
            observation("no-adoption", week=0, month=0),
        ],
        run_id="run-1",
        measured_at="2026-10-19T10:00:00Z",
    )


def read_csv(content: bytes) -> list[list[str]]:
    """Разобрать CSV-ответ: BOM снимается декодированием ``utf-8-sig``."""
    assert content.startswith(b"\xef\xbb\xbf"), "нет BOM — Excel сломает кириллицу"
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))


# --- события -----------------------------------------------------------------


def test_events_export_csv(client: TestClient) -> None:
    """CSV событий: attachment, ``text/csv``, шапка и все строки."""
    seed_events(client)

    response = client.post("/api/events/export", params={"format": "csv"}, headers=auth())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert disposition.endswith('.csv"')

    rows = read_csv(response.content)
    assert rows[0] == ["id", "kind", "name", "detail", "at", "run_id"]
    assert len(rows) == 6  # шапка + 5 событий
    assert {row[1] for row in rows[1:]} == {"release", "flag_appeared"}


def test_events_export_json(client: TestClient) -> None:
    """JSON: ``{"events": [...]}`` с кириллицей как есть (``ensure_ascii=False``)."""
    seed_events(client)

    response = client.post("/api/events/export", params={"format": "json"}, headers=auth())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    payload = response.json()
    assert len(payload["events"]) == 5
    # кириллица не превращена в \uXXXX — файл читает и внешний клиент
    assert "MCP в описании".encode("utf-8") in response.content


def test_events_export_filter_by_kind(client: TestClient) -> None:
    """``kind=release`` — только релизы: фильтр тот же, что у экрана."""
    seed_events(client)

    response = client.post(
        "/api/events/export", params={"format": "csv", "kind": "release"}, headers=auth()
    )

    rows = read_csv(response.content)
    assert len(rows) == 4  # шапка + 3 релиза
    assert {row[1] for row in rows[1:]} == {"release"}


def test_events_export_is_not_a_page(client: TestClient) -> None:
    """Выгрузка не равна странице экрана: 60 событий уезжают все, а не 50.

    Экран листает по 50 (``PAGE_SIZE`` в ``monitor.js``), а выгрузка обязана
    содержать весь таймлайн под фильтром — иначе файл молча теряет хвост, и
    «экспорт» оказывается копией одного экрана.
    """
    store = app_state(client).store
    store.record_events(
        [
            {"kind": "release", "name": f"pkg-{index:02d}", "detail": f"1.0 → 1.{index}"}
            for index in range(60)
        ],
        run_id="run-1",
        at="2026-10-19T10:00:00Z",
    )

    rows = read_csv(
        client.post("/api/events/export", params={"format": "csv"}, headers=auth()).content
    )

    assert len(rows) == 61  # шапка + 60


def test_events_export_rejects_unknown_format(client: TestClient) -> None:
    """``format=xml`` — 400."""
    response = client.post("/api/events/export", params={"format": "xml"}, headers=auth())

    assert response.status_code == 400


def test_events_export_limit_above_max_is_400(client: TestClient) -> None:
    """``limit`` больше 50 000 — 400, а не тихое усечение до потолка."""
    response = client.post(
        "/api/events/export", params={"format": "csv", "limit": "100000"}, headers=auth()
    )

    assert response.status_code == 400


# --- матрица -----------------------------------------------------------------


def test_matrix_export_csv_has_points_of_matrix(client: TestClient) -> None:
    """CSV матрицы: attachment, шапка как у точки, нулевой adoption не попал."""
    seed_observations(client)

    response = client.post("/api/matrix/export", params={"format": "csv"}, headers=auth())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    rows = read_csv(response.content)
    assert rows[0] == MATRIX_EXPORT_HEADER
    # порядок — как у Store.get_latest_observations (по имени), не по adoption:
    # файл и экран строятся из одного списка, сортировку делает экран
    assert [row[0] for row in rows[1:]] == ["flask", "requests"]
    # flag — числом 0/1: колонку считают формулами в Excel
    flag_index = MATRIX_HEADER.index("flag")
    assert {row[flag_index] for row in rows[1:]} == {"0", "1"}


def test_matrix_export_matches_api_matrix_points(client: TestClient) -> None:
    """Экспорт и экран — одни и те же точки (общий ``_matrix_points``)."""
    seed_observations(client)

    api_points = client.get("/api/matrix", headers=auth()).json()["points"]
    rows = read_csv(
        client.post("/api/matrix/export", params={"format": "csv"}, headers=auth()).content
    )

    assert [point["name"] for point in api_points] == [row[0] for row in rows[1:]]


def test_matrix_export_rejects_unknown_format(client: TestClient) -> None:
    """У матрицы только CSV: ``format=json`` — 400."""
    response = client.post("/api/matrix/export", params={"format": "json"}, headers=auth())

    assert response.status_code == 400


def test_matrix_export_momentum_filled_for_points_with_history(
    client: TestClient,
) -> None:
    """Десятая колонка: значение есть у точки с историей, пусто — у одиночной.

    Пустая строка, а не «0»: отсутствие истории и нулевой рост — разные ответы.
    """
    seed_observations(client)
    # второй замер requests за 25 дней до основного: Δ30 = (1234-1000)/1000
    app_state(client).store.record_observations(
        [observation("requests", week=1000)],
        run_id="run-0",
        measured_at="2026-09-25T10:00:00Z",
    )

    rows = read_csv(
        client.post("/api/matrix/export", params={"format": "csv"}, headers=auth()).content
    )

    momentum_index = MATRIX_EXPORT_HEADER.index("momentum_30d")
    by_name = {row[0]: row for row in rows[1:]}
    assert by_name["requests"][momentum_index] == "23.4"
    assert by_name["flask"][momentum_index] == ""


def test_matrix_export_keeps_legacy_nine_columns_first(
    client: TestClient,
) -> None:
    """Backward-compat: первые девять колонок в прежнем порядке, ровно 11 в строке."""
    seed_observations(client)

    rows = read_csv(
        client.post("/api/matrix/export", params={"format": "csv"}, headers=auth()).content
    )

    assert rows[0][: len(MATRIX_HEADER)] == MATRIX_HEADER
    assert rows[0][len(MATRIX_HEADER) :] == ["momentum_30d", "gap_score"]
    assert all(len(row) == len(MATRIX_EXPORT_HEADER) for row in rows)


def test_matrix_export_gap_score_matches_api_matrix_points(
    client: TestClient,
) -> None:
    """Одиннадцатая колонка (P5f-1): gap_score заполнен и равен точке API.

    Экспорт не пересчитывает score своей копией формулы: он зовёт тот же
    ``_attach_gap_scores``, что ``GET /api/matrix``. Иначе число в файле и
    число на экране разошлись бы при первой правке формулы — а файл сверяют с
    картинкой (та же логика, что у ``momentum_30d`` в P4c).
    """
    seed_observations(client)

    response = client.post("/api/matrix/export", params={"format": "csv"}, headers=auth())
    rows = read_csv(response.content)
    points = client.get("/api/matrix", headers=auth()).json()["points"]
    by_name = {point["name"]: point for point in points}

    gap_index = MATRIX_EXPORT_HEADER.index("gap_score")
    assert gap_index == 10
    assert len(rows) == 1 + len(points)
    for row in rows[1:]:
        point = by_name[row[0]]
        assert point["gap_score"] is not None, row[0]
        # CSV пишет float как есть; сравнение — по числу, не по формату
        assert float(row[gap_index]) == pytest.approx(point["gap_score"])
