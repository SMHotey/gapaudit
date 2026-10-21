"""Демо-режим (``tools/demo_mode.py``, шаг P4d): фикстуры вместо сети, память вместо диска.

Проверяется три вещи, каждая из которых ломалась бы «тихо»:

* демо-транспорт физически не умеет в сеть — запрос на несуществующий домен
  получает ``404`` из словаря, а не исключение транспорта;
* ``seed_demo_store`` пишет обычные строки Store, из которых считаются
  momentum и Gap Score (то есть демо-экран показывает то же, что настоящий);
* демо ничего не пишет в каталог данных: кэш — ``DictCache``, файлы не
  создаются, ``api_keys.json`` не появляется.

Сети и подпроцессов — ноль (AGENTS gotcha 23).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from app.store import Store
from cli._common import load_runtime
from core.cache import DictCache
from core.collectors.momentum import compute_momentum
from core.http import HttpClient, Throttle, Transport
from core.registry import npm_url, pypi_url, pypistats_url
from core.settings import Settings
from desktop import prepare_demo, seed_demo
from tools.demo_mode import (
    DEMO_EVENTS,
    DEMO_PACKAGES,
    install_demo_gh,
    make_demo_http_client,
    make_demo_settings,
    make_demo_transport,
    seed_demo_store,
)


def demo_parser() -> argparse.ArgumentParser:
    """Парсер с общими флагами CLI — ровно те, что читает ``load_runtime``."""
    parser = argparse.ArgumentParser(prog="demo-test")
    parser.add_argument("--data-dir")
    parser.add_argument("--settings")
    parser.add_argument("--cache-path")
    parser.add_argument("--keyword", action="append")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--demo", action="store_true")
    return parser


@pytest.fixture()
def store():
    """Открытый ``Store`` в памяти: демо-данные не касаются диска."""
    with Store(":memory:") as memory_store:
        yield memory_store


def test_seed_demo_store_writes_observations_events_and_catalogs(store: Store) -> None:
    """Сид даёт 18 наблюдений трёх замеров, 8 событий и 2 каталога со строками."""
    counts = seed_demo_store(store)

    assert counts["observations"] == len(DEMO_PACKAGES) * 3
    assert counts["events"] == len(DEMO_EVENTS)
    assert counts["catalogs"] == 2
    assert counts["items"] > 0
    assert store.count_observations() == counts["observations"]
    assert store.count_events() == counts["events"]
    assert len(store.list_catalogs()) == 2
    # Имена пакетов попали в packages из наблюдений (upsert в записи).
    names = {row["name"] for row in store.list_packages(limit=100)}
    assert {package["name"] for package in DEMO_PACKAGES} <= names


def test_seed_is_idempotent(store: Store) -> None:
    """Повторный сид ничего не пишет: демо можно перезапускать в одном процессе."""
    first = seed_demo_store(store)
    before = store.count_observations()

    second = seed_demo_store(store)

    assert first["observations"] > 0
    assert second == {"observations": 0, "events": 0, "catalogs": 0, "items": 0}
    assert store.count_observations() == before


def test_demo_history_gives_momentum(store: Store) -> None:
    """Три замера дают momentum: у растущего пакета дельта 30 дней положительна."""
    seed_demo_store(store)
    history = store.get_observations_history("fastmcp", limit=10)

    momentum = compute_momentum(history)

    assert momentum.samples == 3
    assert momentum.window_30d_ok is True
    assert momentum.delta_30d is not None and momentum.delta_30d > 5.0
    assert momentum.color_for_30d() == "green"


def test_demo_transport_never_touches_network() -> None:
    """Неизвестный URL — ``404`` из словаря; настоящий транспорт упал бы на нём."""
    transport: Transport = make_demo_transport()

    status, body = transport(pypi_url("requests"), {}, 5.0)
    assert status == 200 and json.loads(body)["info"]["name"] == "requests"

    status, body = transport(npm_url("requests"), {}, 5.0)
    assert status == 200 and body

    status, body = transport(pypistats_url("requests"), {}, 5.0)
    assert status == 200 and json.loads(body)["data"]["last_week"] > 0

    # Домен, которого нет: реальный транспорт бросил бы исключение, демо — 404.
    assert transport("https://gapaudit-demo.invalid/api", {}, 5.0) == (404, b"")


def test_demo_client_is_memory_only() -> None:
    """Клиент демо: ``DictCache``, троттлинг выключен, транспорт-фикстура."""
    client = make_demo_http_client()

    assert isinstance(client, HttpClient)
    assert isinstance(client.cache, DictCache)
    assert client.throttle.rate_for("pypi.org") == 0
    status, payload = client.get(pypi_url("requests"))
    assert status == 200 and payload["info"]["name"] == "requests"


def test_load_runtime_demo_ignores_settings_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--demo`` читает дефолты, а не файл настроек, и не создаёт каталог данных."""
    from core.settings import save

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save(Settings(alive_weekly=77, keywords=("helm",)), data_dir / "settings.json")
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(data_dir))

    settings, client = load_runtime(demo_parser().parse_args(["--demo"]))

    assert settings.alive_weekly == Settings().alive_weekly  # файл не прочитан
    assert settings.keywords == Settings().keywords
    assert isinstance(client.cache, DictCache)
    assert not (data_dir / "api_keys.json").exists()
    assert not (data_dir / "cache.json").exists()


def test_env_demo_enables_demo_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GAPAUDIT_DEMO=1`` — то же, что ``--demo`` (флаг не нужен)."""
    monkeypatch.setenv("GAPAUDIT_DEMO", "1")

    settings, client = load_runtime(demo_parser().parse_args(["--keyword", "otel"]))

    assert settings.keywords == ("otel",)  # CLI-ключевые слова уважаются и в демо
    assert isinstance(client.cache, DictCache)


def test_env_demo_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Без флага и без переменной демо-режим не включается."""
    monkeypatch.delenv("GAPAUDIT_DEMO", raising=False)
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))

    _settings, client = load_runtime(demo_parser().parse_args([]))

    # Обычный рантайм: файловый кэш, а не память (демо-транспорта здесь нет).
    assert not isinstance(client.cache, DictCache)


def test_prepare_demo_points_at_temporary_data_dir(tmp_path: Path) -> None:
    """``prepare_demo`` ставит флаг и уводит каталог данных во временный."""
    env: dict[str, str] = {}

    demo_dir = prepare_demo(env)

    assert env["GAPAUDIT_DEMO"] == "1"
    assert env["GAPAUDIT_DATA_DIR"] == str(demo_dir)
    assert demo_dir.is_dir()
    assert "gapaudit-demo-" in demo_dir.name
    assert str(tmp_path) not in str(demo_dir)


def test_seed_demo_via_desktop_application(store: Store) -> None:
    """``desktop.seed_demo`` работает через ``app.state`` (то, что делает запуск)."""

    class FakeApp:
        def __init__(self, opened_store: Store) -> None:
            self.state = argparse.Namespace(store=opened_store)

    counts = seed_demo(FakeApp(store))

    assert counts["observations"] > 0
    assert seed_demo(argparse.Namespace()) == {}


def test_make_demo_settings_are_defaults() -> None:
    """Настройки демо — ``Settings()``: чужой ``settings.json`` не влияет."""
    settings = make_demo_settings()

    assert settings == Settings()


def test_demo_gh_returns_fixture_and_undoes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``gh api search/repositories`` отвечает снимком; отмена возвращает настоящий run."""
    import subprocess

    undo = install_demo_gh()
    try:
        page1 = subprocess.run(
            ["gh", "api", "-X", "GET", "search/repositories", "-f", "q=test", "-f", "page=1"],
            capture_output=True,
            text=True,
        )
        page2 = subprocess.run(
            ["gh", "api", "-X", "GET", "search/repositories", "-f", "q=test", "-f", "page=2"],
            capture_output=True,
            text=True,
        )
        auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    finally:
        undo()

    assert page1.returncode == 0 and json.loads(page1.stdout)["items"]
    assert json.loads(page2.stdout)["items"] == []
    assert auth.returncode == 0


def test_demo_client_throttle_type() -> None:
    """Троттлинг демо — экземпляр ``Throttle`` с нулевой скоростью по умолчанию."""
    client = make_demo_http_client()

    assert isinstance(client.throttle, Throttle)


def test_cli_check_predecessors_demo_runs_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m cli.check_predecessors requests --demo`` — код 0 без сети.

    ``cwd`` — временный каталог: CLI по контракту пишет свой ``raw/predecessors.json``
    рядом (legacy-поведение), и тест не должен оставлять файлы в репозитории.
    """
    from cli import check_predecessors

    monkeypatch.delenv("GAPAUDIT_DEMO", raising=False)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "predecessors.json"

    code = check_predecessors.main(["requests", "--demo", "--json-out", str(out)])

    captured = capsys.readouterr()
    assert code == 0
    assert "демо-режим" in captured.err
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(rows, list) and rows and rows[0]["name"] == "requests"
    # adoption взят из фикстуры pypistats, а не из сети.
    assert rows[0]["week"] == 317314822


def test_cli_catalog_build_demo_by_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``GAPAUDIT_DEMO=1`` — каталог собирается из фикстур (gh, npm, реестр MCP)."""
    from cli import catalog_build

    monkeypatch.setenv("GAPAUDIT_DEMO", "1")
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "catalog.json"

    code = catalog_build.main(["test impact analysis", "--out", str(out)])

    captured = capsys.readouterr()
    assert code == 0
    assert "демо-режим" in captured.err
    payload = json.loads(out.read_text(encoding="utf-8"))
    items = payload["items"]
    assert items, "каталог пуст: демо-транспорт не отдал фикстуры"
    assert payload["stats"], "сводка каталога пуста"
    sources = {item.get("source") for item in items}
    assert "github" in sources  # gh подменён снимком tests/fixtures/catalog


def test_app_serves_seeded_demo_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Приложение в демо-окружении отдаёт матрицу из сида, каталог данных — временный."""
    from fastapi.testclient import TestClient

    from app.web import create_app

    monkeypatch.setenv("GAPAUDIT_DEMO", "1")
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    application = create_app(token="demo-token", port=8799)

    with TestClient(application, base_url="http://127.0.0.1:8799") as client:
        seeded = seed_demo(application)
        headers = {"X-GapAudit-Token": "demo-token"}
        health = client.get("/api/health").json()
        matrix = client.get("/api/matrix", headers=headers).json()

    assert seeded["observations"] > 0
    assert health["db_ok"] is True
    assert health["data_dir"] == str(tmp_path / "data")
    assert matrix["counts"]["total"] == len(DEMO_PACKAGES)
    assert all(point["adoption_week"] > 0 for point in matrix["points"])


def test_demo_client_never_leaves_the_process() -> None:
    """Клиент демо на несуществующем домене отвечает ``404`` из словаря.

    Гейт против подмены транспорта: настоящий ``urllib``-транспорт на таком
    адресе исчерпал бы повторы и вернул ``(None, None)``, а не ``404``.
    """
    client = make_demo_http_client()

    status, value = client.get("https://gapaudit-demo.invalid/api")

    assert (status, value) == (404, None)
