"""Тесты cli/catalog_scan.py — фейковый HttpClient: обход, обрыв, продолжение (шаг 1f).

Страницы — фикстуры 1d (``servers_page1.json``/``servers_page2.json``); последняя
страница собирается копией второй без ``metadata.nextCursor``.
"""

from __future__ import annotations

import json
import pathlib

import pytest

import core.http as http_module
from cli import catalog_scan
from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.mcp_registry import MCP_HOST, MCP_LIST_URL, next_url

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "mcp_registry"
LIMIT = 10

PAGE1 = json.loads((FIXTURES / "servers_page1.json").read_text(encoding="utf-8"))
PAGE2 = json.loads((FIXTURES / "servers_page2.json").read_text(encoding="utf-8"))
PAGE1_CURSOR = PAGE1["metadata"]["nextCursor"]
LAST_PAGE = {"servers": PAGE2["servers"], "metadata": {"count": len(PAGE2["servers"])}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def url(cursor: str | None = None) -> str:
    return next_url(MCP_LIST_URL.format(limit=LIMIT), cursor, LIMIT)


class FakeTransport:
    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url_: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url_)
        return self.mapping.get(url_, (404, b'{"message": "Not Found"}'))


def install(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, tuple[int, bytes]]) -> FakeTransport:
    transport = FakeTransport(mapping)

    def build(cache_path: str = "raw/.pcache.json") -> HttpClient:
        return HttpClient(
            cache=DictCache(),
            throttle=Throttle(per_minute={MCP_HOST: 60000}),
            transport=transport,
            sleep=lambda _seconds: None,
        )

    monkeypatch.setattr(http_module, "default_client", build)
    return transport


def body(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def run(tmp_path: pathlib.Path, extra: list[str] | None = None) -> tuple[int, pathlib.Path, pathlib.Path]:
    checkpoint = tmp_path / "ckpt.json"
    result = tmp_path / "scan.json"
    argv = [
        "--limit", str(LIMIT),
        "--checkpoint", str(checkpoint),
        "--json-out", str(result),
        "--no-sleep",
    ] + list(extra or [])
    return catalog_scan.main(argv), checkpoint, result


def test_full_pass_writes_result_and_keeps_checkpoint_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, {url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(LAST_PAGE))})

    code, checkpoint, result = run(tmp_path)
    out = capsys.readouterr().out

    assert code == 0
    assert "КУРСОР КОНЧИЛСЯ" in out
    assert "КЛАССЫ ЗАДАЧ" in out  # классы напечатаны, как требует шаг 1f

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["finished"] is True
    assert payload["pages"] == 2
    assert payload["entries"] == 20
    # Чекпоинт остаётся как след для продолжения — legacy его не удалял.
    assert checkpoint.exists()
    trace = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert trace["finished"] is True


def test_interrupted_scan_exits_1_with_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = install(monkeypatch, {url(None): (200, body(PAGE1))})

    code, checkpoint, result = run(tmp_path)
    out = capsys.readouterr().out

    assert code == 1
    assert "СТОП" in out and "курсор сохранён" in out

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["finished"] is False
    assert payload["entries"] == 10  # обрыв: страница 2 не посчитана

    trace = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert trace["cursor"] == PAGE1_CURSOR  # курсор сохранён — продолжим с него
    # Обрыв пришёл как 404 на неизвестный URL фикстура-маппинга.
    assert url(PAGE1_CURSOR) in transport.calls


def test_resume_from_checkpoint_skips_done_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = install(monkeypatch, {url(PAGE1_CURSOR): (200, body(LAST_PAGE))})
    (tmp_path / "ckpt.json").write_text(
        json.dumps({
            "cursor": PAGE1_CURSOR, "pages": 1, "entries": 10,
            "names": ["ac.inference.sh/mcp"], "counts": {}, "kinds": {}, "ns": {},
        }),
        encoding="utf-8",
    )

    code, _checkpoint, result = run(tmp_path)
    out = capsys.readouterr().out

    assert code == 0
    assert "резюме с чекпоинта: стр.1, записей 10" in out
    # Страница 1 не запрашивалась повторно — только курсор из чекпоинта.
    assert transport.calls == [url(PAGE1_CURSOR)]

    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["pages"] == 2
    assert payload["entries"] == 20
    assert payload["finished"] is True


def test_hide_zero_filters_class_table_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, {url(None): (200, body(PAGE1)), url(PAGE1_CURSOR): (200, body(LAST_PAGE))})

    code, _checkpoint, result = run(tmp_path, extra=["--hide-zero"])
    out = capsys.readouterr().out

    assert code == 0
    assert "observability" not in out  # нулевой класс скрыт в печати
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert "observability" in payload["class_counts"]  # в JSON нули сохранены полностью


def test_custom_base_url_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """``--base-url`` реально уходит в запрос — шаг 1g (фейк фиксирует URL)."""
    custom = "https://x.example/y?limit={limit}"
    expected = next_url("https://x.example/y?limit=10", None, LIMIT)
    transport = install(monkeypatch, {expected: (200, body(LAST_PAGE))})

    code, _checkpoint, result = run(tmp_path, extra=["--base-url", custom])

    assert code == 0
    assert transport.calls == [expected]  # на MCP_LIST_URL запроса не было
    assert json.loads(result.read_text(encoding="utf-8"))["finished"] is True
