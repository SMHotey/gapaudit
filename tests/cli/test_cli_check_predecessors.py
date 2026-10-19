"""Тесты cli/check_predecessors.py — main() in-process, сеть ноль (шаг 1f).

``core.http.default_client`` подменён фейком с транспортом по карте URL ->
(статус, тело): все ответы из ``tests/fixtures/http/``. Троттлинг отключён
высокой скоростью по хостам, каталог данных уведён в ``tmp_path`` окружением
``GAPAUDIT_DATA_DIR`` — тесты не трогают ни сеть, ни чужие каталоги.
"""

from __future__ import annotations

import json
import pathlib

import pytest

import core.http as http_module
from cli import check_predecessors
from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.packages import POPULAR_URL
from core.registry import npm_search_url, npm_url, pypi_url, pypistats_url

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "http"

#: Скорость троттлинга для тестовых клиентов: 60 000/мин — пауз нет.
FAST_HOSTS = {
    "pypi.org": 60000,
    "registry.npmjs.org": 60000,
    "pypistats.org": 60000,
    "api.npmjs.org": 60000,
    "hugovk.github.io": 60000,
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Каталог данных и cwd — во временный каталог: настройки/кэш не от пользователя."""
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeTransport:
    """Транспорт по карте {url: (status, body)}; неизвестный URL — 404."""

    def __init__(self, mapping: dict[str, tuple[int, bytes]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(url)
        return self.mapping.get(url, (404, b'{"message": "Not Found"}'))


def install(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, tuple[int, bytes]]) -> FakeTransport:
    """Подменить core.http.default_client клиентом с фикстурным транспортом."""
    transport = FakeTransport(mapping)

    def build(cache_path: str = "raw/.pcache.json") -> HttpClient:
        return HttpClient(
            cache=DictCache(),
            throttle=Throttle(per_minute=dict(FAST_HOSTS)),
            transport=transport,
            sleep=lambda _seconds: None,
        )

    monkeypatch.setattr(http_module, "default_client", build)
    return transport


def requests_mapping() -> dict[str, tuple[int, bytes]]:
    """Живой пакет requests: PyPI + npm + pypistats + npm search + датасет популярных."""
    popular = json.dumps({"rows": [{"project": "requests"}, {"project": "httpx"}]})
    return {
        pypi_url("requests"): (200, fixture_bytes("pypi_requests.json")),
        npm_url("requests"): (200, fixture_bytes("npm_requests.json")),
        pypistats_url("requests"): (200, fixture_bytes("pypistats_requests.json")),
        npm_search_url("reqeusts", 25): (200, fixture_bytes("npm_search_requests.json")),
        POPULAR_URL: (200, popular.encode("utf-8")),
    }


# --- режимы -----------------------------------------------------------------------


def test_check_name_prints_both_verdicts_and_writes_no_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, requests_mapping())
    json_path = tmp_path / "out.json"

    code = check_predecessors.main(
        ["--check-name", "requests", "test-xyz-fake", "--json", str(json_path)]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "ЗАНЯТО      requests" in out
    assert "СВОБОДНО    test-xyz-fake" in out
    assert not json_path.exists()  # режим проверки имени JSON не пишет — как в legacy


def test_similar_finds_requests_for_typo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, requests_mapping())

    code = check_predecessors.main(["--similar", "reqeusts"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Похожие на «reqeusts»" in out
    assert "d=2  requests" in out  # перестановка букв — это два правки Левенштейна


def test_main_prints_table_and_writes_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, requests_mapping())
    json_path = tmp_path / "predecessors.json"

    code = check_predecessors.main(["requests", "--json", str(json_path)])
    out = capsys.readouterr().out

    assert code == 0
    assert "вердикт" in out
    assert "ЖИВО" in out
    assert "Python HTTP for Humans." in out  # строка находки видна рядом с вердиктом (§14)

    rows = json.loads(json_path.read_text(encoding="utf-8"))
    assert [row["name"] for row in rows] == ["requests"]
    assert rows[0]["src"] == "pypi"
    assert rows[0]["verdict"] == "ЖИВО"
    assert rows[0]["mcp"] is False


def test_strict_exits_2_on_alive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, requests_mapping())

    code = check_predecessors.main(["requests", "--strict", "--quiet"])
    out = capsys.readouterr().out

    assert code == 2
    assert "STRICT" in out


def test_from_requirements_reads_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, requests_mapping())
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests>=2.31\n# комментарий\n", encoding="utf-8")
    json_path = tmp_path / "out.json"

    code = check_predecessors.main(["--from", str(requirements), "--json", str(json_path)])
    out = capsys.readouterr().out

    assert code == 0
    assert f"из {requirements} взято имён: 1" in out
    rows = json.loads(json_path.read_text(encoding="utf-8"))
    assert [row["name"] for row in rows] == ["requests"]


def test_diff_prints_events_with_legacy_labels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, requests_mapping())
    prev = tmp_path / "prev.json"
    prev.write_text(
        json.dumps([{
            "name": "requests", "ver": "2.32.0", "date": "2025-01-01",
            "week": 100, "mcp": False, "verdict": "ЖИВО",
        }]),
        encoding="utf-8",
    )

    code = check_predecessors.main(["requests", "--diff", str(prev), "--quiet"])
    out = capsys.readouterr().out

    assert code == 0
    assert f"ИЗМЕНЕНИЯ с {prev}" in out
    assert "РЕЛИЗ" in out
    assert "2.32.0 → 2.34.2" in out


def test_no_names_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, {})

    code = check_predecessors.main([])
    out = capsys.readouterr().out

    assert code == 1
    assert "нечего проверять" in out
