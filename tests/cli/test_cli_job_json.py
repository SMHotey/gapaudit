"""Протокол ``--job-json`` в CLI (шаг 3d-a): JSON-lines, подавление таблиц, fake HTTP.

``GAPAUDIT_FAKE_HTTP=1`` подменяет сеть фикстурами ``tests/fixtures/http`` — это
единственный путь, которым CLI реально проверяется без сети (AGENTS gotcha 30).
``catalog_scan`` проверяется фейковым ``HttpClient`` (паттерн шага 1f).
"""

from __future__ import annotations

import json
import pathlib

import pytest

import core.http as http_module
from cli import _common, catalog_scan, check_predecessors
from core.cache import DictCache
from core.http import HttpClient, Throttle
from core.mcp_registry import MCP_HOST, MCP_LIST_URL, next_url

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "mcp_registry"
LIMIT = 10

PAGE1 = json.loads((FIXTURES / "servers_page1.json").read_text(encoding="utf-8"))
PAGE1_CURSOR = PAGE1["metadata"]["nextCursor"]
LAST_PAGE = {
    "servers": json.loads((FIXTURES / "servers_page2.json").read_text(encoding="utf-8"))["servers"],
    "metadata": {"count": 0},
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Каталог данных и cwd — во временный каталог; сеть по умолчанию выключена."""
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def protocol(out: str) -> list[dict]:
    """Разобрать stdout как JSON-lines: каждая непустая строка обязана быть JSON."""
    lines = [line for line in out.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_check_predecessors_job_json_emits_hello_progress_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """hello -> progress* -> done, каждая строка — валидный JSON, таблицы нет."""
    monkeypatch.setenv(_common.ENV_FAKE_HTTP, "1")
    out_path = tmp_path / "predecessors.json"

    code = check_predecessors.main(["requests", "--job-json", "--json-out", str(out_path)])
    out = capsys.readouterr().out

    assert code == 0
    messages = protocol(out)
    assert messages[0]["type"] == "hello"
    assert isinstance(messages[0]["pid"], int)
    assert messages[-1] == {"type": "done"}
    progress = [message for message in messages if message["type"] == "progress"]
    assert progress, messages
    assert all(0.0 <= message["progress"] <= 1.0 for message in progress)
    assert progress[-1]["progress"] == 1.0
    # «имя» — заголовок таблицы legacy: под протоколом её быть не должно
    assert "имя" not in out
    assert out_path.is_file()


def test_check_predecessors_without_flag_prints_table_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Без флага — таблица и итоги в stdout, JSON-lines нет (паритет с legacy)."""
    monkeypatch.setenv(_common.ENV_FAKE_HTTP, "1")

    code = check_predecessors.main(["requests", "--json-out", str(tmp_path / "p.json")])
    out = capsys.readouterr().out

    assert code == 0
    assert "имя" in out
    assert '"type"' not in out


def test_catalog_scan_job_json_emits_hello_and_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пять «простых» CLI: hello + done, таблица ушла в stderr (лог задачи)."""
    def url(cursor: str | None = None) -> str:
        return next_url(MCP_LIST_URL.format(limit=LIMIT), cursor, LIMIT)

    mapping = {
        url(None): (200, json.dumps(PAGE1).encode("utf-8")),
        url(PAGE1_CURSOR): (200, json.dumps(LAST_PAGE).encode("utf-8")),
    }

    def transport(url_: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        return mapping.get(url_, (404, b'{"message": "Not Found"}'))

    def build(cache_path: str = "raw/.pcache.json") -> HttpClient:
        return HttpClient(
            cache=DictCache(),
            throttle=Throttle(per_minute={MCP_HOST: 60000}),
            transport=transport,
            sleep=lambda _seconds: None,
        )

    monkeypatch.setattr(http_module, "default_client", build)

    code = catalog_scan.main(["--job-json", "--limit", str(LIMIT), "--no-sleep"])
    captured = capsys.readouterr()

    assert code == 0
    assert [message["type"] for message in protocol(captured.out)] == ["hello", "done"]
    # таблица классов — в stderr (её читает лог задачи), не в протоколе
    assert "КЛАССЫ ЗАДАЧ" in captured.err


def test_fake_http_without_fixtures_is_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Нет фикстур — RuntimeError с путём, а не тихий 404 (AGENTS gotcha 30)."""
    monkeypatch.setattr(_common, "FIXTURES_HTTP_DIR", tmp_path / "нет-фикстур")

    with pytest.raises(RuntimeError, match="нет фикстур HTTP"):
        _common.make_fake_transport_from_fixtures()


def test_cli_error_under_job_json_emits_error_and_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Исключение в теле CLI — строка ``error`` и код 2 (а не трейсбек)."""
    def boom(args: object) -> tuple[object, object]:
        raise ValueError("сломанный рантайм")

    monkeypatch.setattr(check_predecessors, "load_runtime", boom)

    code = check_predecessors.main(["requests", "--job-json"])
    captured = capsys.readouterr()

    assert code == 2
    messages = protocol(captured.out)
    assert messages[0]["type"] == "hello"
    assert messages[-1]["type"] == "error"
    assert "сломанный рантайм" in messages[-1]["error"]
    assert "сломанный рантайм" in captured.err


def test_job_emitter_clamps_progress(capsys: pytest.CaptureFixture[str]) -> None:
    """Подпроцесс не может отправить 150% прогресса: значения зажаты в [0, 1]."""
    emit = _common.build_job_emitter()
    emit(1.5, "больше единицы")
    emit(-0.5, "меньше нуля")

    values = [message["progress"] for message in protocol(capsys.readouterr().out)]
    assert values == [1.0, 0.0]


# --- --diff: формат результата (шаг 3b) --------------------------------------


def baseline(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Прошлый прогон: файл наблюдений + изменённая версия — baseline для --diff."""
    monkeypatch.setenv(_common.ENV_FAKE_HTTP, "1")
    prev_path = tmp_path / "prev.json"
    check_predecessors.main(["requests", "--quiet", "--json-out", str(prev_path)])
    previous = json.loads(prev_path.read_text(encoding="utf-8"))
    # версия в прошлом прогоне другая — сравнение обязано дать событие release
    previous[0]["ver"] = "0.0.1"
    previous[0]["date"] = "2020-01-01"
    prev_path.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setenv(_common.ENV_FAKE_HTTP, "1")
    return prev_path


def test_diff_under_job_json_writes_events_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """result.json задачи — объект ``{"events": [...]}``, а не список наблюдений.

    Иначе хук lifespan не может записать события: ``Store.record_events`` ждёт
    записи с ``kind``/``name``, а список наблюдений — это другая форма.
    """
    prev_path = baseline(tmp_path, monkeypatch)
    result_path = tmp_path / "jobs" / "abc.result.json"

    code = check_predecessors.main([
        "requests", "--job-json", "--json-out", str(result_path),
        "--diff", str(prev_path),
    ])
    capsys.readouterr()

    assert code == 0
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert isinstance(payload["events"], list)
    assert [event["kind"] for event in payload["events"]] == ["release"]
    assert payload["events"][0]["name"] == "requests"
    assert set(payload["events"][0]) >= {"kind", "name", "detail"}
    # наблюдения не потеряны: файл остаётся пригодным как baseline
    assert payload["observations"][0]["name"] == "requests"


def test_diff_without_job_json_keeps_list_and_prints_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Автономный CLI: печать сравнения на месте, а --json-out — по-прежнему список.

    Файл используется как baseline следующего ``--diff``, поэтому формат
    «ради очереди» здесь менять нельзя (Железное правило 2: CLI автономен).
    """
    prev_path = baseline(tmp_path, monkeypatch)
    out_path = tmp_path / "current.json"

    code = check_predecessors.main([
        "requests", "--json-out", str(out_path), "--diff", str(prev_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert f"ИЗМЕНЕНИЯ с {prev_path}" in captured.out
    assert "РЕЛИЗ" in captured.out
    stored = json.loads(out_path.read_text(encoding="utf-8"))
    assert isinstance(stored, list)
    assert stored[0]["name"] == "requests"


def test_diff_with_missing_baseline_is_quiet_for_humans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Нет файла сравнения — как раньше: ни печати, ни падения (legacy-поведение)."""
    monkeypatch.setenv(_common.ENV_FAKE_HTTP, "1")
    out_path = tmp_path / "current.json"

    code = check_predecessors.main([
        "requests", "--json-out", str(out_path), "--diff", str(tmp_path / "нет.json"),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert "ИЗМЕНЕНИЯ" not in captured.out
    assert isinstance(json.loads(out_path.read_text(encoding="utf-8")), list)


# --- --diff без позиционных имён: задача monitor.diff (шаг 3c) ----------------


def test_diff_fills_names_from_prev_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Имена берутся из prev.json, если в argv их нет (дефект 3b).

    Именно так запускает мониторинг очередь задач: ``_default_runner``
    собирает argv из параметров ``monitor.diff``, а там только ``diff_from``.
    До фикса CLI отвечал «нечего проверять» и не доходил до сравнения, поэтому
    событий в ``events`` не появлялось никогда.
    """
    monkeypatch.setenv(_common.ENV_FAKE_HTTP, "1")
    prev_path = tmp_path / "prev.json"
    prev_path.write_text(
        json.dumps(
            [
                {"name": "requests", "src": "pypi", "ver": "0.0.1", "date": "2020-01-01",
                 "month": 1, "week": 1, "flag": False, "mcp": False, "verdict": "ЖИВОЙ"},
                {"name": "flask", "src": "pypi", "ver": "3.0.0", "date": "2024-04-01",
                 "month": 5, "week": 1, "flag": False, "mcp": False, "verdict": "ЖИВОЙ"},
                {"name": "urllib3", "src": "pypi", "ver": "2.2.0", "date": "2024-01-01",
                 "month": 7, "week": 2, "flag": False, "mcp": False, "verdict": "ЖИВОЙ"},
            ]
        ),
        encoding="utf-8",
    )
    result_path = tmp_path / "jobs" / "monitor.result.json"

    code = check_predecessors.main([
        "--job-json", "--json-out", str(result_path), "--diff", str(prev_path),
    ])
    messages = protocol(capsys.readouterr().out)

    assert code == 0
    assert messages[0]["type"] == "hello"
    assert messages[-1] == {"type": "done"}
    assert [message["type"] for message in messages].count("progress") >= 1

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert set(payload) >= {"events", "observations"}
    # три наблюдения — по одному на имя из baseline (порядок — sorted, как у --diff)
    assert [row["name"] for row in payload["observations"]] == [
        "flask", "requests", "urllib3",
    ]
    assert any(
        event["kind"] == "release" and event["name"] == "requests"
        for event in payload["events"]
    )


def test_diff_with_empty_prev_without_names_reports_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Пустой baseline без имён — ``emit_error`` и код 2, а не тихий ``done``.

    Иначе очередь записала бы в events ноль строк и показала «готово»: задача
    выглядела бы выполненной, хотя ничего не проверено.
    """
    monkeypatch.setenv(_common.ENV_FAKE_HTTP, "1")
    prev_path = tmp_path / "empty.json"
    prev_path.write_text("[]", encoding="utf-8")
    result_path = tmp_path / "jobs" / "empty.result.json"

    code = check_predecessors.main([
        "--job-json", "--json-out", str(result_path), "--diff", str(prev_path),
    ])
    messages = protocol(capsys.readouterr().out)

    assert code == 2
    assert messages[0]["type"] == "hello"
    assert messages[-1]["type"] == "error"
    assert "нечего проверять" in messages[-1]["error"]
    assert not result_path.exists()

