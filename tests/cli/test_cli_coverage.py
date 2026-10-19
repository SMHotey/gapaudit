"""Тесты cli/coverage.py — фикстуры raw-файлов в tmp_path, сеть ноль (шаг 1f).

Методология (GENERIC/CAPS/BAD_*/TOOLISH) живёт в обёртке; тесты проверяют
конвейер: корпус -> покрытие -> фильтр не-инструментов -> три файла + сводка.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from cli import coverage

CATALOG = [
    {"server": {"name": "flakychef/flaky", "description": "flaky test detection for CI", "title": "Flaky"}},
    {"server": {"name": "other/toolbox", "description": "developer tools", "title": "Toolbox"}},
]
ECOSYSTEM = {
    "acme/impact-cli": {
        "full_name": "acme/impact-cli", "desc": "CLI for impact analysis",
        "topics": ["cli"], "lang": "Python", "stars": 500,
    }
}
POPULAR = {
    # В имени репозитория есть токен из корпуса — попадает в «покрыто».
    "owner/flaky": {
        "full_name": "owner/flaky", "desc": "flaky test detector", "lang": "Python",
        "stars": 900, "topics": ["testing"],
    },
    # Токена в корпусе нет — возможная дыра; описание делает его инструментом.
    "owner/queryplanner": {
        "full_name": "owner/queryplanner", "desc": "CLI that explains query plans for databases",
        "lang": "Python", "stars": 800, "topics": ["database"],
    },
    # Подборка — не инструмент: отсекается фильтром, остаётся в gaps.
    "owner/awesome-lists": {
        "full_name": "owner/awesome-lists", "desc": "curated list of awesome testing resources",
        "lang": "Markdown", "stars": 700, "topics": [],
    },
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def write_raw(
    tmp_path: pathlib.Path,
    *,
    catalog: object = CATALOG,
    eco: object = ECOSYSTEM,
    pop: object = POPULAR,
) -> pathlib.Path:
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    (raw / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    (raw / "ecosystem_repos.json").write_text(json.dumps(eco), encoding="utf-8")
    (raw / "popular.json").write_text(json.dumps(pop), encoding="utf-8")
    return raw


def test_pipeline_writes_three_files_and_summary(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = write_raw(tmp_path)
    summary = tmp_path / "summary.json"

    code = coverage.main(["--raw-dir", str(raw), "--json-out", str(summary)])
    out = capsys.readouterr().out

    assert code == 0
    gaps = json.loads((raw / "gaps.json").read_text(encoding="utf-8"))
    tools = json.loads((raw / "gap_tools.json").read_text(encoding="utf-8"))
    density = json.loads((raw / "saturation.json").read_text(encoding="utf-8"))
    stats = json.loads(summary.read_text(encoding="utf-8"))

    # flaky — в корпусе (покрыто); queryplanner и awesome-lists — пробелы.
    assert stats["covered_n"] == 1
    assert stats["gaps_n"] == 2
    # После фильтра не-инструментов awesome-lists выбыл: остался только queryplanner.
    assert stats["tools_n"] == 1
    assert [tool["full_name"] for tool in tools] == ["owner/queryplanner"]
    assert len(gaps["gaps"]) == 2 and len(gaps["covered"]) == 1

    assert density["флаки-тесты"] == 1  # «flaky» найден в описании сервера каталога
    assert "ПЛОТНОСТЬ КЛАССОВ ЗАДАЧ" in out
    assert "ТОП КАНДИДАТОВ" in out
    assert "queryplanner" in out


def test_empty_inputs_give_zeros_and_exit_0(tmp_path: pathlib.Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    for name in ("catalog.json", "ecosystem_repos.json", "popular.json"):
        (raw / name).write_text("{}", encoding="utf-8")
    summary = tmp_path / "summary.json"

    code = coverage.main(["--raw-dir", str(raw), "--json-out", str(summary)])

    assert code == 0
    stats = json.loads(summary.read_text(encoding="utf-8"))
    assert stats["gaps_n"] == 0
    assert stats["covered_n"] == 0
    assert stats["tools_n"] == 0


def test_missing_inputs_exit_1(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty_dir = tmp_path / "нет-такого"

    code = coverage.main(["--raw-dir", str(empty_dir)])
    captured = capsys.readouterr()

    assert code == 1
    assert "нет входных файлов" in captured.err
