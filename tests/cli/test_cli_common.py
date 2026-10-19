"""Тесты cli/_common.py — общая инфраструктура шести обёрток (шаг 1f)."""

from __future__ import annotations

import argparse
import json
import pathlib

import pytest

from cli._common import build_argparser, exit_code, print_table, write_json


def parser() -> argparse.ArgumentParser:
    return build_argparser(prog="demo.py", description="тест")


def test_build_argparser_has_all_common_flags() -> None:
    args = parser().parse_args([])

    # Общие флаги есть у каждого скрипта и по умолчанию выключены.
    assert args.data_dir is None
    assert args.settings is None
    assert args.cache_path is None
    assert args.keyword is None
    assert args.json_out is None
    assert args.no_cache is False
    assert args.quiet is False


def test_exit_code_strict_gate() -> None:
    assert exit_code(False, True, True) == 0  # без --strict гейт не работает
    assert exit_code(True, False, False) == 0  # строгий, но срабатываний нет
    assert exit_code(True, True, False) == 2  # есть живой
    assert exit_code(True, False, True) == 2  # есть признак ниши


def test_print_table_aligns_and_widths(capsys: pytest.CaptureFixture[str]) -> None:
    print_table(["имя", "звёзды"], [["alpha", "10"], ["b", "2000"]], aligns=["l", "r"])
    out = capsys.readouterr().out.splitlines()

    assert out[0].startswith("имя")
    assert out[1].startswith("alpha") and out[1].endswith("10")
    assert out[2].startswith("b") and out[2].endswith("2000")


def test_write_json_atomic_creates_dirs_and_lf(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sub" / "out.json"

    write_json(target, {"ключ": ["значение"]})

    assert target.is_file()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {"ключ": ["значение"]}
    # Атомарная запись не оставляет временных файлов.
    assert [item.name for item in target.parent.iterdir()] == ["out.json"]
    # newline="\n": без перевода строки файла JSON на Windows хватило бы.
    assert "\r\n" not in target.read_bytes().decode("utf-8")


def test_write_json_replaces_without_leftover_tmp(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "out.json"

    write_json(target, [1])
    write_json(target, [1, 2])

    assert json.loads(target.read_text(encoding="utf-8")) == [1, 2]
    assert sorted(item.name for item in tmp_path.iterdir()) == ["out.json"]


def test_load_runtime_order_file_env_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Порядок наложения: файл -> окружение -> CLI (контракт §0/1f)."""
    from cli._common import load_runtime
    from core.settings import save, Settings

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save(Settings(alive_weekly=50, keywords=("mcp",), cache_ttl_hours=3.0), data_dir / "settings.json")

    args = parser().parse_args(["--data-dir", str(data_dir)])
    settings, _client = load_runtime(args)
    assert settings.alive_weekly == 50  # из файла
    assert settings.cache_ttl_hours == 3.0

    monkeypatch.setenv("GAPAUDIT_CACHE_TTL_H", "1.5")
    monkeypatch.setenv("GAPAUDIT_KEYWORDS", "helm,chart")
    args = parser().parse_args(["--data-dir", str(data_dir), "--keyword", "otel"])
    settings, _client = load_runtime(args)
    assert settings.cache_ttl_hours == 1.5  # окружение перекрыло файл
    assert settings.alive_weekly == 50  # незаданное поле файла уцелело
    assert settings.keywords == ("otel",)  # CLI перекрыл окружение

    # Каталог данных --data-dir указывает на корень проекта: guard не даёт такое.
    from core.settings import project_root
    bad = parser().parse_args(["--data-dir", str(project_root())])
    with pytest.raises(ValueError, match="--data-dir"):
        load_runtime(bad)
