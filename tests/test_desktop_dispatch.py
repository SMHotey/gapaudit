"""Диспетчер CLI в frozen-сборке: ``desktop._dispatch_cli`` (шаг P4d-3).

В frozen-сборке ``sys.executable`` = сам ``gapaudit.exe`` (у него нет ``-m``),
а ``app/jobs.py::_default_runner`` собирает argv как
``[sys.executable, "-m", module, ...]`` — тот же argv, что и в source.
Диспетчер перехватывает ``-m`` до argparse и вызывает ``main()`` модуля.

В source (не frozen) диспетчер возвращает None — обычный
``python -m cli.*`` работает как раньше (обратная совместимость,
compat_check не падает). Сеть и окно не используются: фиксируется только
контракт перехвата (AGENTS gotcha 75).
"""

from __future__ import annotations

import importlib
import sys
import types

import desktop


def test_dispatch_without_args_is_not_cli_mode() -> None:
    """Пустой argv — не CLI-режим (frozen или нет, проверка порядка)."""
    assert desktop._dispatch_cli([]) is None


def test_dispatch_with_desktop_flag_is_not_cli_mode() -> None:
    """``--browser`` — флаг desktop, не CLI: диспетчер не мешает."""
    assert desktop._dispatch_cli(["--browser"]) is None


def test_dispatch_with_bare_m_is_not_cli_mode() -> None:
    """``-m`` без имени модуля — слишком мало аргументов, не CLI-режим."""
    assert desktop._dispatch_cli(["-m"]) is None


def test_dispatch_in_source_returns_none_for_m() -> None:
    """Вне frozen даже ``-m <module>`` — None: ``python -m cli.*`` работает
    штатно, диспетчер в source ничего не перехватывает."""
    assert getattr(sys, "frozen", False) is False
    assert desktop._dispatch_cli(["-m", "cli.check_predecessors", "--help"]) is None


def test_dispatch_in_frozen_calls_module_main(monkeypatch) -> None:
    """Frozen + ``-m <module>``: импортируем модуль, зовём ``main(rest)``."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    seen: dict[str, object] = {}

    def fake_main(rest: list[str]) -> int:
        seen["rest"] = rest
        return 7

    def fake_import_module(name: str) -> types.SimpleNamespace:
        seen["name"] = name
        return types.SimpleNamespace(main=fake_main)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    code = desktop._dispatch_cli(
        ["-m", "cli.check_predecessors", "--job-json", "--json-out=/tmp/x.json"]
    )
    assert code == 7
    assert seen["name"] == "cli.check_predecessors"
    # Остаток argv уходит в main() без "-m" и имени модуля — ровно то,
    # что собрал _default_runner.
    assert seen["rest"] == ["--job-json", "--json-out=/tmp/x.json"]


def test_dispatch_in_frozen_missing_module_returns_2(monkeypatch, capsys) -> None:
    """Frozen + несуществующий модуль: exit code 2 и причина в stderr."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def fake_import_module(name: str) -> types.SimpleNamespace:
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    code = desktop._dispatch_cli(["-m", "nonexistent.module", "--job-json"])
    assert code == 2
    assert "не удалось импортировать" in capsys.readouterr().err


def test_dispatch_in_frozen_reopens_streams_as_utf8(monkeypatch) -> None:
    """stdout/stderr дочернего windowed-процесса переоткрываются в utf-8:
    JSON-протокол с кириллицей/«→» иначе падает с UnicodeEncodeError
    (в P4d-3 niche.find падал на catalog.build именно так)."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    calls: list[dict[str, str]] = []

    class FakeStream:
        def reconfigure(self, **kwargs: str) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdout", FakeStream())
    monkeypatch.setattr(sys, "stderr", FakeStream())
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: types.SimpleNamespace(main=lambda rest: 0),
    )
    code = desktop._dispatch_cli(["-m", "cli.check_predecessors", "--job-json"])
    assert code == 0
    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ]
