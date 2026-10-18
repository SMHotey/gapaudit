"""Контрактный тест: core.http отдаёт ровно то, что читает legacy/check_predecessors.py.

Эта проверка фиксирует контракт, чтобы в шаге 1c замена legacy.get() на
core.http.default_client().get() была механической. Тела ответов — реальные образцы
из tests/fixtures/http (сняты один раз, сеть в тесте не нужна).
"""

from __future__ import annotations

import json
import pathlib

from core.cache import DictCache
from core.http import HttpClient, Throttle

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "http"


class FixtureTransport:
    """Отвечает фикстурой по URL; на неизвестный URL — 404, как живой PyPI."""

    def __init__(self, mapping: dict[str, pathlib.Path]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        path = self._mapping.get(url)
        if path is None:
            return 404, b'{"message": "Not Found"}'
        return 200, path.read_bytes()


def make_client(mapping: dict[str, pathlib.Path]) -> tuple[HttpClient, FixtureTransport]:
    transport = FixtureTransport(mapping)
    client = HttpClient(
        cache=DictCache(),
        throttle=Throttle(per_minute={"pypi.org": 6000, "registry.npmjs.org": 6000, "pypistats.org": 6000}),
        transport=transport,
        sleep=lambda _seconds: None,
    )
    return client, transport


def test_pypi_fields_used_by_legacy_get() -> None:
    url = "https://pypi.org/pypi/requests/json"
    client, transport = make_client({url: FIXTURES / "pypi_requests.json"})

    status, data = client.get(url)

    assert status == 200
    assert isinstance(data, dict)
    # Ровно те обращения, что делает legacy pypi(): info.version, releases[ver][0], info.summary.
    version = data["info"]["version"]
    assert version == "2.34.2"
    assert data["releases"][version][0]["upload_time"].startswith("2026-05-14")
    assert data["info"]["summary"] == "Python HTTP for Humans."
    assert transport.calls == [url]


def test_missing_package_is_404_none() -> None:
    url = "https://pypi.org/pypi/requests-nonexistent-fake-package/json"
    client, transport = make_client({})

    status, data = client.get(url)

    assert (status, data) == (404, None)
    # Отрицательный ответ кэшируется: второй прогон не дёргает сеть.
    assert client.get(url) == (404, None)
    assert transport.calls == [url]


def test_npm_fields_used_by_legacy_npm() -> None:
    url = "https://registry.npmjs.org/requests"
    client, _ = make_client({url: FIXTURES / "npm_requests.json"})

    status, data = client.get(url)

    assert status == 200
    latest = data["dist-tags"]["latest"]
    assert latest == "0.3.0"
    assert data["versions"][latest]["description"]
    assert data["time"][latest].startswith("2019-08-06")


def test_pypistats_fields_used_by_legacy_adoption() -> None:
    url = "https://pypistats.org/api/packages/requests/recent"
    client, _ = make_client({url: FIXTURES / "pypistats_requests.json"})

    status, data = client.get(url)

    assert status == 200
    assert data["data"]["last_month"] > 0
    assert data["data"]["last_week"] > 0


def test_fixtures_are_valid_json() -> None:
    """Фикстуры читаются как JSON — иначе контракт выше проверял бы не то."""
    for name in ("pypi_requests.json", "npm_requests.json", "pypistats_requests.json"):
        json.loads((FIXTURES / name).read_text(encoding="utf-8"))
