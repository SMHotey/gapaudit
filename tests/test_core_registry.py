"""Тесты core/registry.py — на фикстурах, без сети (шаг 1b).

Фикстуры сняты один раз из живых API и лежат в tests/fixtures/http/.
"""

from __future__ import annotations

import json
import pathlib

from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.registry import (
    RegistryClient,
    adoption_raw,
    npm_downloads_url,
    npm_raw,
    npm_url,
    pypi_raw,
    pypi_url,
    pypistats_url,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "http"
PYPISTATS_URL = pypistats_url("requests")
NPM_DOWNLOADS_URL = npm_downloads_url("requests")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTransport:
    """Транспорт по карте {url: (status, body)}; по умолчанию — 404."""

    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        return self.mapping.get(url, (404, b'{"message": "Not Found"}'))


def make_client(mapping: dict[str, tuple[int, bytes]]) -> tuple[RegistryClient, FakeTransport]:
    transport = FakeTransport(mapping)
    http = HttpClient(
        cache=DictCache(),
        throttle=Throttle(per_minute={"pypi.org": 6000, "registry.npmjs.org": 6000, "pypistats.org": 6000, "api.npmjs.org": 6000}),
        transport=transport,
        sleep=lambda _seconds: None,
    )
    return RegistryClient(http), transport


def json_body(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


# --- pypi ----------------------------------------------------------------------------------


def test_pypi_raw_from_real_fixture() -> None:
    parsed = pypi_raw(fixture("pypi_requests.json"))

    assert parsed is not None
    assert parsed["ver"] == "2.34.2"
    assert parsed["date"] == "2026-05-14"
    assert parsed["summary"] == "Python HTTP for Humans."
    assert parsed["has_mcp"] is False
    # В современном ответе PyPI home_page отсутствует — как и в legacy, это "".
    assert parsed["home"] == ""


def test_pypi_raw_detects_mcp_in_description() -> None:
    payload = {
        "info": {"version": "1.0.0", "summary": "Grafana tools", "description": "An MCP server for Grafana"},
        "releases": {"1.0.0": [{"upload_time": "2024-05-01T10:00:00"}]},
    }

    parsed = pypi_raw(payload)

    assert parsed is not None
    assert parsed["has_mcp"] is True


def test_pypi_raw_survives_empty_description() -> None:
    payload = {"info": {"version": "2.0"}, "releases": {"2.0": [{"upload_time": "2024-01-02T03:04:05"}]}}

    parsed = pypi_raw(payload)

    assert parsed is not None
    assert parsed["ver"] == "2.0"
    assert parsed["date"] == "2024-01-02"
    assert parsed["summary"] == ""
    assert parsed["has_mcp"] is False


def test_pypi_raw_empty_releases_gives_question_mark() -> None:
    """В legacy здесь был бы IndexError; по контракту шага 1b — date = "?"."""
    payload = {"info": {"version": "3.0"}, "releases": {"3.0": []}}

    parsed = pypi_raw(payload)

    assert parsed is not None
    assert parsed["date"] == "?"


def test_pypi_raw_rejects_empty_payload() -> None:
    assert pypi_raw({}) is None
    assert pypi_raw(None) is None


def test_pypi_client_returns_raw_json() -> None:
    payload = fixture("pypi_requests.json")
    client, transport = make_client({pypi_url("requests"): (200, json_body(payload))})

    card = client.pypi("requests")

    assert card is not None
    assert card["ver"] == "2.34.2"
    assert card["raw"] == payload
    assert transport.calls == [pypi_url("requests")]


def test_pypi_client_none_on_404() -> None:
    client, _ = make_client({})

    assert client.pypi("requests-nonexistent-fake-package") is None


def test_pypi_client_none_when_network_failed() -> None:
    """Все попытки провалились -> (None, None) -> None, а не исключение."""

    def dead_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        raise TimeoutError("timed out")

    http = HttpClient(
        cache=DictCache(),
        throttle=Throttle(per_minute={"pypi.org": 6000}),
        transport=dead_transport,
        sleep=lambda _seconds: None,
    )
    client = RegistryClient(http)

    assert client.pypi("requests") is None


# --- npm -----------------------------------------------------------------------------------


def test_npm_raw_from_real_fixture() -> None:
    parsed = npm_raw(fixture("npm_requests.json"))

    assert parsed is not None
    assert parsed["ver"] == "0.3.0"
    assert parsed["date"] == "2019-08-06"
    assert parsed["summary"].startswith("An streaming XHR abstraction")
    assert parsed["has_mcp"] is False
    assert "github.com/unshiftio/requests" in parsed["home"]


def test_npm_raw_error_payload_is_none() -> None:
    assert npm_raw({"error": "Not found"}) is None
    assert npm_raw(None) is None
    assert npm_raw({}) is None


def test_npm_raw_detects_mcp_in_version_metadata() -> None:
    payload = {
        "dist-tags": {"latest": "1.2.3"},
        "versions": {"1.2.3": {"description": "plain", "keywords": ["mcp-server"]}},
        "time": {"1.2.3": "2025-02-03T00:00:00.000Z"},
    }

    parsed = npm_raw(payload)

    assert parsed is not None
    assert parsed["has_mcp"] is True
    assert parsed["date"] == "2025-02-03"


def test_npm_client_returns_raw_json() -> None:
    payload = fixture("npm_requests.json")
    client, _ = make_client({npm_url("requests"): (200, json_body(payload))})

    card = client.npm("requests")

    assert card is not None
    assert card["ver"] == "0.3.0"
    assert card["raw"] == payload


def test_npm_client_none_on_404() -> None:
    client, _ = make_client({})

    assert client.npm("requests-nonexistent-fake-package") is None


# --- adoption ------------------------------------------------------------------------------


def test_adoption_raw_from_real_pypistats_fixture() -> None:
    parsed = adoption_raw(fixture("pypistats_requests.json"), None)

    assert parsed == (1202777296, 317314822)


def test_adoption_raw_npm_fallback_multiplies_week_by_four() -> None:
    assert adoption_raw(None, 12345) == (49380, 12345)


def test_adoption_raw_without_any_data_is_none_not_zero() -> None:
    assert adoption_raw(None, None) == (None, None)


def test_adoption_raw_pypistats_without_fields_does_not_fall_back_to_npm() -> None:
    """Документ пришёл, полей нет — это «данных нет», а не повод идти в npm."""
    assert adoption_raw({"data": {}}, 999) == (None, None)


def test_adoption_client_uses_pypistats_first() -> None:
    payload = fixture("pypistats_requests.json")
    client, transport = make_client({PYPISTATS_URL: (200, json_body(payload))})

    assert client.adoption("requests") == (1202777296, 317314822)
    assert transport.calls == [PYPISTATS_URL]


def test_adoption_client_falls_back_to_npm_after_429() -> None:
    client, transport = make_client(
        {
            PYPISTATS_URL: (429, b"slow down"),
            NPM_DOWNLOADS_URL: (200, json_body({"downloads": 12345})),
        }
    )

    assert client.adoption("requests") == (49380, 12345)
    assert transport.calls[0] == PYPISTATS_URL
    assert NPM_DOWNLOADS_URL in transport.calls


def test_adoption_client_none_when_nothing_found() -> None:
    client, _ = make_client({})

    assert client.adoption("requests-nonexistent-fake-package") == (None, None)


def test_adoption_client_npm_payload_without_downloads_field() -> None:
    client, _ = make_client({NPM_DOWNLOADS_URL: (200, json_body({"package": "requests"}))})

    assert client.adoption("requests") == (None, None)


# --- адреса --------------------------------------------------------------------------------


def test_urls_quote_names_like_legacy() -> None:
    assert pypi_url("requests") == "https://pypi.org/pypi/requests/json"
    assert npm_url("@scope/pkg") == "https://registry.npmjs.org/@scope/pkg"
    assert npm_downloads_url("@scope/pkg") == "https://api.npmjs.org/downloads/point/last-week/@scope/pkg"
    assert pypistats_url("some name") == "https://pypistats.org/api/packages/some%20name/recent"
