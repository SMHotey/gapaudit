"""Экраны «Каталоги» и ``/api/catalogs/*`` (шаг 3b): сборка, список, элементы, экспорт.

Что проверяется:

* ``POST /api/catalogs`` — валидация тела (фразы 1..10, ``min_stars >= 0``,
  ``pages`` 1..10, источники из списка, лишние ключи) и 202 с
  ``catalog_id == job_id``: каталог пишется хуком lifespan под id задачи;
* ``GET /api/catalogs`` — список с ``items_count``, свежие сверху;
* ``GET /api/catalogs/{id}`` — метаданные, 404 на неизвестный;
* ``GET /api/catalogs/{id}/items`` — фильтры ``alive``/``lang``/``min_stars``/
  ``q``, пагинация, ``total`` по тем же фильтрам;
* ``POST /api/catalogs/{id}/export`` — CSV (``utf-8-sig``, attachment) и
  Markdown (``core.catalog.to_markdown``), ``format=xml`` — 400.

Данные готовятся через ``Store.record_catalog``/``record_catalog_items`` — тем
же API, которым пишет хук: эндпоинты только читают. Сеть — ноль,
подпроцессы — ноль (клиент из conftest, воркеры не поднимаются).
"""

from __future__ import annotations

import time

import httpx
from fastapi.testclient import TestClient

from app.store import Store
from tests.conftest import app_state, auth

CATALOG_ID = "a1b2c3d4e5f60718"


def date_ago(days: int) -> str:
    """``YYYY-MM-DD`` N дней назад — формат ``catalog_items.pushed``."""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))


def item(name: str = "owner/repo", **overrides) -> dict:
    """Строка каталога в форме ``core.catalog.build_catalog``."""
    base = {
        "source": "github",
        "id": name,
        "desc": "test impact analysis tool",
        "stars": 100,
        "lang": "Python",
        "pushed": date_ago(5),
        "sources": ["github"],
    }
    base.update(overrides)
    return base


def seed(
    client: TestClient,
    catalog_id: str = CATALOG_ID,
    *,
    items: list[dict] | None = None,
    queries: list[str] | None = None,
) -> Store:
    """Каталог + элементы в Store живого приложения; возвращает сам Store."""
    store = app_state(client).store
    store.record_catalog(
        catalog_id=catalog_id,
        queries=queries if queries is not None else ["test impact analysis"],
        stats={"total": len(items or [])},
        path=f"data/jobs/{catalog_id}.result.json",
    )
    store.record_catalog_items(
        catalog_id, items if items is not None else [item()]
    )
    return store


def create(client: TestClient, body: dict) -> httpx.Response:
    return client.post("/api/catalogs", json=body, headers=auth())


# --- POST /api/catalogs: валидация -------------------------------------------


def test_create_without_phrases_is_400(client: TestClient) -> None:
    response = create(client, {"min_stars": 20})

    assert response.status_code == 400
    assert "phrases" in response.json()["detail"]


def test_create_with_too_many_phrases_is_400(client: TestClient) -> None:
    response = create(client, {"phrases": [f"phrase {n}" for n in range(11)]})

    assert response.status_code == 400
    assert "phrases" in response.json()["detail"]
    # задача не создана: каталогов и задач по этому пути нет
    assert client.get("/api/catalogs", headers=auth()).json()["catalogs"] == []


def test_create_with_negative_min_stars_is_400(client: TestClient) -> None:
    response = create(client, {"phrases": ["flaky test detection"], "min_stars": -1})

    assert response.status_code == 400
    assert "min_stars" in response.json()["detail"]


def test_create_with_zero_pages_is_400(client: TestClient) -> None:
    response = create(client, {"phrases": ["flaky test detection"], "pages": 0})

    assert response.status_code == 400
    assert "pages" in response.json()["detail"]


def test_create_with_unknown_source_is_400(client: TestClient) -> None:
    response = create(client, {"phrases": ["flaky test detection"], "sources": ["evil"]})

    assert response.status_code == 400
    assert "sources" in response.json()["detail"]


def test_create_with_unknown_key_is_400(client: TestClient) -> None:
    """Лишний ключ — ошибка, а не «проигнорируем»: иначе опечатка в имени
    параметра выглядела бы как сборка с дефолтами."""
    response = create(client, {"phrases": ["flaky test detection"], "min_star": 20})

    assert response.status_code == 400
    assert "min_star" in response.json()["detail"]


def test_create_valid_returns_202_with_catalog_id_equal_job(client: TestClient) -> None:
    response = create(
        client,
        {
            "phrases": ["test impact analysis", "flaky test detection"],
            "min_stars": 20,
            "pages": 2,
            "sources": ["github", "npm"],
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["catalog_id"] == body["job_id"]
    job = client.get(f"/api/jobs/{body['job_id']}", headers=auth()).json()
    assert job["kind"] == "catalog.build"
    assert job["params"]["phrases"] == [
        "test impact analysis",
        "flaky test detection",
    ]
    assert job["params"]["sources"] == ["github", "npm"]
    # дефолты уезжают в задачу явно — их видно в карточке задачи
    assert job["params"]["min_stars"] == 20
    assert job["params"]["pages"] == 2


def test_create_requires_token(client: TestClient) -> None:
    response = client.post("/api/catalogs", json={"phrases": ["flaky test detection"]})

    assert response.status_code == 403


# --- GET /api/catalogs --------------------------------------------------------


def test_list_is_empty_on_fresh_store(client: TestClient) -> None:
    response = client.get("/api/catalogs", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"catalogs": []}


def test_list_returns_catalogs_newest_first(client: TestClient) -> None:
    store = seed(client, "cat-old", items=[item("a/x")])
    seed(client, "cat-new", items=[item("b/y"), item("c/z")])
    # время записи у обеих строк — одна секунда; задаём явно, иначе порядок
    # держал бы tie-breaker id, и тест проверял бы не сортировку
    assert store.conn is not None
    store.conn.execute(
        "UPDATE catalogs SET created_at = ? WHERE id = ?",
        ("2026-10-19T10:00:00Z", "cat-old"),
    )
    store.conn.execute(
        "UPDATE catalogs SET created_at = ? WHERE id = ?",
        ("2026-10-19T11:00:00Z", "cat-new"),
    )

    catalogs = client.get("/api/catalogs", headers=auth()).json()["catalogs"]

    assert [catalog["id"] for catalog in catalogs] == ["cat-new", "cat-old"]
    assert [catalog["items_count"] for catalog in catalogs] == [2, 1]
    assert catalogs[0]["queries"] == ["test impact analysis"]
    assert catalogs[0]["stats"] == {"total": 2}


def test_list_limit_is_validated(client: TestClient) -> None:
    too_big = client.get("/api/catalogs?limit=100000", headers=auth())
    not_a_number = client.get("/api/catalogs?limit=abc", headers=auth())

    assert too_big.status_code == 400
    assert "limit" in too_big.json()["detail"]
    assert not_a_number.status_code == 400


# --- GET /api/catalogs/{id} ---------------------------------------------------


def test_get_catalog_returns_metadata_and_items_count(client: TestClient) -> None:
    seed(client, items=[item("a/x"), item("b/y")])

    response = client.get(f"/api/catalogs/{CATALOG_ID}", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == CATALOG_ID
    assert body["queries"] == ["test impact analysis"]
    assert body["items_count"] == 2
    assert body["path"].endswith(".result.json")


def test_get_unknown_catalog_is_404(client: TestClient) -> None:
    assert client.get("/api/catalogs/nope", headers=auth()).status_code == 404
    assert (
        client.get("/api/catalogs/nope/items", headers=auth()).status_code == 404
    )
    assert (
        client.post("/api/catalogs/nope/export?format=csv", headers=auth()).status_code
        == 404
    )


# --- GET /api/catalogs/{id}/items --------------------------------------------


def test_items_filters_by_alive_lang_stars_and_query(client: TestClient) -> None:
    seed(
        client,
        items=[
            item("fresh/py", stars=900, lang="Python", desc="test impact analysis"),
            item("stale/py", stars=50, lang="Python", desc="flaky test detection",
                 pushed=date_ago(400)),
            item("fresh/go", stars=20, lang="Go", desc="vector database"),
        ],
    )
    base = f"/api/catalogs/{CATALOG_ID}/items"

    everything = client.get(base, headers=auth()).json()
    alive = client.get(f"{base}?alive=true", headers=auth()).json()
    dead = client.get(f"{base}?alive=false", headers=auth()).json()
    go = client.get(f"{base}?lang=Go", headers=auth()).json()
    stars = client.get(f"{base}?min_stars=100", headers=auth()).json()
    found = client.get(f"{base}?q=flaky", headers=auth()).json()

    assert everything["total"] == 3
    # порядок — по звёздам вниз: экран не сортирует на клиенте
    assert [row["name"] for row in everything["items"]] == [
        "fresh/py",
        "stale/py",
        "fresh/go",
    ]
    assert [row["name"] for row in alive["items"]] == ["fresh/py", "fresh/go"]
    assert alive["total"] == 2
    assert [row["name"] for row in dead["items"]] == ["stale/py"]
    assert [row["name"] for row in go["items"]] == ["fresh/go"]
    assert [row["name"] for row in stars["items"]] == ["fresh/py"]
    assert [row["name"] for row in found["items"]] == ["stale/py"]


def test_items_pagination_and_total(client: TestClient) -> None:
    seed(
        client,
        items=[item(f"repo/{n}", stars=n * 10) for n in range(1, 6)],
    )
    base = f"/api/catalogs/{CATALOG_ID}/items"

    first = client.get(f"{base}?limit=2", headers=auth()).json()
    second = client.get(f"{base}?limit=2&offset=2", headers=auth()).json()

    assert [row["name"] for row in first["items"]] == ["repo/5", "repo/4"]
    assert [row["name"] for row in second["items"]] == ["repo/3", "repo/2"]
    # total не зависит от страницы: иначе «2 элемента» на экране при пяти
    assert first["total"] == second["total"] == 5
    assert first["limit"] == 2 and second["offset"] == 2


def test_items_empty_values_mean_no_filter(client: TestClient) -> None:
    """Экран отправляет ``alive=`` всегда — это не фильтр «пустое значение»."""
    seed(client, items=[item("a/x"), item("b/y", pushed=date_ago(400))])

    response = client.get(
        f"/api/catalogs/{CATALOG_ID}/items?alive=&lang=&q=&min_stars=",
        headers=auth(),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2


def test_items_bad_filters_are_400(client: TestClient) -> None:
    seed(client)
    base = f"/api/catalogs/{CATALOG_ID}/items"

    assert client.get(f"{base}?alive=maybe", headers=auth()).status_code == 400
    assert client.get(f"{base}?min_stars=-5", headers=auth()).status_code == 400
    assert client.get(f"{base}?limit=0", headers=auth()).status_code == 400


# --- POST /api/catalogs/{id}/export -----------------------------------------


def test_export_csv_is_attachment_with_bom(client: TestClient) -> None:
    seed(
        client,
        items=[
            item("a/x", stars=7, lang="Go", desc="первый"),
            item("b/y", stars=None, lang=None, desc=None),
        ],
    )

    response = client.post(
        f"/api/catalogs/{CATALOG_ID}/export?format=csv", headers=auth()
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="catalog-{CATALOG_ID}.csv"'
    )
    # BOM: без него Excel открывает кириллицу как cp1251
    assert response.content.startswith(b"\xef\xbb\xbf")
    text = response.content.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0] == "name,source,stars,lang,pushed,desc"
    assert len(lines) == 3  # шапка + два элемента, без усечения страницей
    assert "a/x,github,7,Go" in text
    # None звёзд — пустое поле, а не слово «None»
    assert "b/y,github,,," in text


def test_export_markdown_uses_core_table(client: TestClient) -> None:
    seed(client, items=[item("a/x", stars=42)])

    response = client.post(
        f"/api/catalogs/{CATALOG_ID}/export?format=md", headers=auth()
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="catalog-{CATALOG_ID}.md"'
    )
    text = response.content.decode("utf-8")
    # шапка от экрана + таблица от core.catalog.to_markdown (один формат с CLI)
    assert "Каталог `" + CATALOG_ID + "`" in text
    assert "Фразы: test impact analysis" in text
    assert "# Каталог: test impact analysis" in text
    assert "| Звёзды | Проект | Язык | Push | Источники | Описание |" in text
    assert "a/x" in text


def test_export_default_format_is_csv(client: TestClient) -> None:
    seed(client)

    response = client.post(f"/api/catalogs/{CATALOG_ID}/export", headers=auth())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")


def test_export_unknown_format_is_400(client: TestClient) -> None:
    seed(client)

    response = client.post(
        f"/api/catalogs/{CATALOG_ID}/export?format=xml", headers=auth()
    )

    assert response.status_code == 400
    assert "format" in response.json()["detail"]


# --- слои валидации не расходятся --------------------------------------------


def test_catalog_params_pass_jobs_validation() -> None:
    """Параметры, которые собирает эндпоинт, проходят таблицы ``app/jobs.py``.

    Два описания «валидной задачи» (границы эндпоинта и whitelist runner'а)
    разошлись бы тихо: API отвечал бы 202, а задача падала в
    ``_params_to_argv`` с ``unknown param``. Тест держит согласие слоёв —
    добавление/переименование ключа в ``KIND_PARAM_KEYS`` уронит его сразу.
    """
    from app.web import _catalog_params, _param_errors

    params, problems = _catalog_params(
        {
            "phrases": ["test impact analysis"],
            "min_stars": 0,
            "pages": 1,
            "sources": ["github", "npm", "registry"],
        }
    )

    assert problems == []
    assert _param_errors("catalog.build", params) == []
    # и наоборот: runner действительно умеет собрать argv из этих params
    from app.jobs import _params_to_argv

    argv = _params_to_argv("catalog.build", params)
    assert argv[0] == "test impact analysis"
    assert "--min-stars=0" in argv and "--pages=1" in argv
    assert "--no-npm" not in argv and "--no-registry" not in argv
