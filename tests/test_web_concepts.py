"""``/api/concepts`` (шаг P4c): концепты в API и загрузка ``concepts.json``.

Концепты — это mapping «vector-db → пакеты в экосистемах», из которого
считается cross-ecosystem momentum. Проверяется контракт:

* GET — список ``[{concept, n_packages, ecosystems}]``, пусто до первых записей;
* POST — 202 при валидном теле, 400 при чужой экосистеме/пустых полях;
* DELETE — ``{"deleted": N}``, 404 для неизвестного концепта;
* стартовое чтение ``<data_dir>/concepts.json``: merge при каждом старте, битый
  файл не роняет приложение (gotcha 50).

Сеть — ноль, подпроцессы — ноль: всё пишется и читается через ``Store``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.web import create_app
from tests.conftest import PORT, TOKEN, app_state, auth, fake_runner


def concepts(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/concepts", headers=auth())
    assert response.status_code == 200, response.text
    return response.json()["concepts"]


def add(client: TestClient, payload: dict[str, Any]) -> Any:
    return client.post("/api/concepts", json=payload, headers=auth())


def test_empty_list_initially(client: TestClient) -> None:
    """Без записей — пустой список, а не 404: экран списка всегда рисуем."""
    response = client.get("/api/concepts", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"concepts": []}


def test_post_then_get_shows_concept(client: TestClient) -> None:
    """Валидная привязка → 202 и появляется в GET с посчётом."""
    response = add(
        client,
        {"concept": "vector-db", "ecosystem": "pypi", "package": "chromadb"},
    )

    assert response.status_code == 202, response.text
    assert response.json() == {"ok": True}
    assert concepts(client) == [
        {
            "concept": "vector-db",
            "n_packages": 1,
            "ecosystems": ["pypi"],
        }
    ]


def test_post_rejects_unknown_ecosystem(client: TestClient) -> None:
    """``ecosystem='evil'`` — 400 до записи: мусор не попадает в mapping."""
    response = add(
        client,
        {"concept": "vector-db", "ecosystem": "evil", "package": "chromadb"},
    )

    assert response.status_code == 400
    assert "ecosystem" in response.json()["detail"]
    assert concepts(client) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"concept": "", "ecosystem": "pypi", "package": "chromadb"},
        {"concept": "vector-db", "ecosystem": "pypi", "package": "  "},
        {"concept": "vector-db", "ecosystem": "pypi"},
        {},
    ],
)
def test_post_rejects_missing_fields(client: TestClient, payload: dict[str, Any]) -> None:
    """Пустой concept/package или отсутствующие поля — 400, ничего не записано."""
    response = add(client, payload)

    assert response.status_code == 400
    assert concepts(client) == []


def test_delete_removes_and_unknown_is_404(client: TestClient) -> None:
    """Удаление отдаёт число строк; повтор удаления неизвестного — 404."""
    add(client, {"concept": "vector-db", "ecosystem": "pypi", "package": "chromadb"})
    add(client, {"concept": "vector-db", "ecosystem": "npm", "package": "chromadb"})

    response = client.delete("/api/concepts/vector-db", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"deleted": 2}
    assert concepts(client) == []

    missing = client.delete("/api/concepts/vector-db", headers=auth())
    assert missing.status_code == 404


def test_concepts_json_loaded_on_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``<data_dir>/concepts.json`` подмешивается в mapping на старте (merge).

    Файл пишется ДО первого ``TestClient`` — lifespan читает его один раз при
    открытии Store. Два концепта из файла + один, добавленный API после старта,
    сосуществуют: загрузка — ``INSERT OR IGNORE``, а не «заменить всё».
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "concepts.json").write_text(
        json.dumps(
            {
                "version": 1,
                "concepts": {
                    "vector-db": {"pypi": ["chromadb"], "npm": ["chromadb"]},
                    "llm-framework": {"pypi": ["langchain"]},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(data_dir))
    application = create_app(token=TOKEN, port=PORT, runner=fake_runner)

    with TestClient(application, base_url=f"http://127.0.0.1:{PORT}") as started:
        listed = concepts(started)
        assert [item["concept"] for item in listed] == ["llm-framework", "vector-db"]
        assert listed[1]["n_packages"] == 2
        assert listed[1]["ecosystems"] == ["npm", "pypi"]
        # merge, а не replace: привязка, добавленная после старта, переживает
        # повторное чтение того же файла — следующий старт её не затрёт.
        add(
            started,
            {"concept": "vector-db", "ecosystem": "github", "package": "chroma-core/chroma"},
        )
        assert len(concepts(started)) == 2
        assert started.get(
            "/api/concepts", headers=auth()
        ).json()["concepts"][1]["n_packages"] == 3
        assert app_state(started).store.get_concept_packages(package="chromadb") == [
            {"concept": "vector-db", "ecosystem": "npm", "package": "chromadb"},
            {"concept": "vector-db", "ecosystem": "pypi", "package": "chromadb"},
        ]


def test_broken_concepts_json_does_not_break_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Битый ``concepts.json`` — предупреждение в логе, приложение стартует."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "concepts.json").write_text("{не json", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(data_dir))
    application = create_app(token=TOKEN, port=PORT, runner=fake_runner)

    with TestClient(application, base_url=f"http://127.0.0.1:{PORT}") as started:
        health = started.get("/api/health")
        assert health.status_code == 200
        assert concepts(started) == []
