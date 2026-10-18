"""Гейт шага 1c: ``core.packages.check()`` один-в-один с legacy ``check()``.

Запускается сам legacy-код (``legacy/check_predecessors.py``) — в него подменяется только
транспорт (``lg.get``), а разбор ответов, вердикты, «ЗАСТОЙ» и сборка словаря остаются
родными. Тела ответов — фикстуры из ``tests/fixtures/http``, сеть не нужна.

Сравниваются все ключи legacy-результата; игнорируются только поля, которых в legacy нет:
``flag`` (алиас ``mcp`` для UI) и ``age_days`` (он есть и в legacy — но объявлен «новым»,
поэтому сверяется отдельно, как бонус).
"""

from __future__ import annotations

import json
import pathlib

import pytest

import legacy.check_predecessors as legacy

from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.packages import check as new_check
from core.registry import (
    RegistryClient,
    npm_downloads_url,
    npm_url,
    pypi_url,
    pypistats_url,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "http"

PYPI_URL = pypi_url("requests")
NPM_URL = npm_url("requests")
PYPISTATS_URL = pypistats_url("requests")
NPM_DOWNLOADS_URL = npm_downloads_url("requests")

PYPI_PAYLOAD = json.loads((FIXTURES / "pypi_requests.json").read_text(encoding="utf-8"))
NPM_PAYLOAD = json.loads((FIXTURES / "npm_requests.json").read_text(encoding="utf-8"))

#: Ключи, по которым сверяем; новые поля core в список не входят.
COMPARED_KEYS = ("name", "src", "ver", "date", "summary", "home", "mcp", "month", "week", "verdict")
NEW_ONLY_KEYS = {"flag", "age_days"}

#: Родной транспорт legacy — чтобы проверить, что тест подменяет его только на время.
ORIGINAL_LEGACY_GET = legacy.get


class FixtureTransport:
    """Ответ по подстроке URL; неизвестный URL — 404, как живой PyPI."""

    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = dict(mapping)
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        for fragment, response in self.mapping.items():
            if fragment in url:
                return response
        return 404, b'{"message": "Not Found"}'


def make_client(mapping: dict[str, tuple[int, bytes]]) -> tuple[RegistryClient, FixtureTransport]:
    transport = FixtureTransport(mapping)
    http = HttpClient(
        cache=DictCache(),
        throttle=Throttle(
            per_minute={
                "pypi.org": 6000,
                "registry.npmjs.org": 6000,
                "pypistats.org": 6000,
                "api.npmjs.org": 6000,
            }
        ),
        transport=transport,
        sleep=lambda _seconds: None,
    )
    return RegistryClient(http), transport


def body(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def pypi_body(upload_time: str = "2026-05-14T19:25:26") -> bytes:
    payload = json.loads(json.dumps(PYPI_PAYLOAD))
    version = payload["info"]["version"]
    payload["releases"][version][0]["upload_time"] = upload_time
    return body(payload)


def pypistats_body() -> bytes:
    return (FIXTURES / "pypistats_requests.json").read_bytes()


def npm_body() -> bytes:
    return (FIXTURES / "npm_requests.json").read_bytes()


def patch_legacy_get(monkeypatch: pytest.MonkeyPatch, client: RegistryClient) -> None:
    """Подменить legacy-транспорт на core.http с теми же фикстурами.

    Кэш legacy тоже обнуляем: он файловый (``raw/.pcache.json``), и обращение к нему
    превратило бы тест в «читает чужое состояние» вместо «сравнивает поведение».
    """

    def fake_get(url, timeout=25, tries=3, raw=False):  # noqa: ANN001, ANN202 — сигнатура legacy
        return client.http.get(url, timeout=timeout, tries=tries, raw=raw)

    monkeypatch.setattr(legacy, "get", fake_get)
    monkeypatch.setattr(legacy, "_CACHE", {})
    monkeypatch.setattr(legacy, "_save_cache", lambda: None)


def compare(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, tuple[int, bytes]]) -> tuple[dict, dict]:
    """Прогнать оба check() на одних данных и вернуть (новый, legacy) результаты."""
    client, _ = make_client(mapping)
    patch_legacy_get(monkeypatch, client)

    new_row = new_check("requests", client)
    old_row = legacy.check("requests")
    return new_row, old_row


def assert_parity(new_row: dict, old_row: dict) -> None:
    assert {key: new_row[key] for key in COMPARED_KEYS} == {key: old_row[key] for key in COMPARED_KEYS}
    # Новые поля — надстройка, а не замена: набор ключей legacy сохранён целиком.
    assert set(old_row) | NEW_ONLY_KEYS == set(new_row)
    assert new_row["flag"] is new_row["mcp"]
    # Дата релиза считается одинаково (legacy days_since против core.diffing.days_since).
    assert new_row["age_days"] == old_row["age_days"]


def test_case_1_live_package_with_adoption(monkeypatch: pytest.MonkeyPatch) -> None:
    new_row, old_row = compare(
        monkeypatch,
        {
            PYPI_URL: (200, pypi_body()),
            NPM_URL: (200, npm_body()),
            PYPISTATS_URL: (200, pypistats_body()),
        },
    )

    assert_parity(new_row, old_row)
    assert new_row["verdict"] == "ЖИВО"
    assert (new_row["ver"], new_row["date"]) == ("2.34.2", "2026-05-14")
    assert new_row["week"] == 317314822


def test_case_2_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    new_row, old_row = compare(monkeypatch, {})

    assert_parity(new_row, old_row)
    assert new_row["verdict"] == "ОТСУТСТВУЕТ"
    assert new_row["src"] == "-"


def test_case_3_alive_but_old_release_becomes_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """«ЗАСТОЙ» накладывается в обеих реализациях: скачивания есть, релизов нет с 2020."""
    new_row, old_row = compare(
        monkeypatch,
        {
            PYPI_URL: (200, pypi_body(upload_time="2020-01-01T00:00:00")),
            PYPISTATS_URL: (200, pypistats_body()),
        },
    )

    assert_parity(new_row, old_row)
    assert new_row["verdict"] == "ЗАСТОЙ" == old_row["verdict"]
    assert new_row["date"] == "2020-01-01"
    # Без даты релиза вердикт остался бы «ЖИВО» — значит, именно apply_stale дал «ЗАСТОЙ».
    assert new_row["week"] == 317314822


@pytest.mark.parametrize(
    ("downloads", "expected"),
    [
        (500, "ЗАСТОЙ"),  # живые скачивания, но релиз npm 2019 -> зомби, как в legacy
        (50, "СЛАБО"),
        (5, "МЁРТВО"),
    ],
)
def test_case_4_npm_only(monkeypatch: pytest.MonkeyPatch, downloads: int, expected: str) -> None:
    """Пакет только в npm: вердикт считается по недельным скачиваниям npm."""
    new_row, old_row = compare(
        monkeypatch,
        {
            NPM_URL: (200, npm_body()),
            NPM_DOWNLOADS_URL: (200, body({"downloads": downloads})),
        },
    )

    assert_parity(new_row, old_row)
    assert new_row["src"] == "npm"
    assert new_row["week"] == downloads and new_row["month"] == downloads * 4
    assert new_row["verdict"] == expected == old_row["verdict"]


def test_patching_legacy_transport_is_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подмена транспорта не остаётся в модуле: эталон после теста тот же, что был до."""
    client, _ = make_client({})

    assert legacy.get is ORIGINAL_LEGACY_GET
    with monkeypatch.context() as scoped:
        patch_legacy_get(scoped, client)
        assert legacy.get is not ORIGINAL_LEGACY_GET
    assert legacy.get is ORIGINAL_LEGACY_GET
    # И сами константы метода тест не трогал.
    assert legacy.PRED_KEYWORDS == ("mcp", "model context protocol")
    assert (legacy.ALIVE_WEEKLY, legacy.STALE_DAYS) == (100, 540)
