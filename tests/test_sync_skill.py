"""Тесты синхронизации со скиллом (``tools/sync_skill.py``, шаг P4d).

Каждый тест строит свой «проект» в ``tmp_path``: копирование настоящего
``core/`` здесь не нужно (это проверяет ``test_smoke_*`` на реальном дереве),
а фейковый источник позволяет положить рядом секреты и ``__pycache__`` и
убедиться, что они не уезжают в скилл.

Сеть — ноль. Подпроцесс — только в ``@pytest.mark.slow`` тесте (проверочная
команда ``python -m scripts.run``), AGENTS gotcha 23.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.sync_skill import INIT_PY, PROJECT_ROOT, main, render_run_py, sync


def make_source(root: Path) -> Path:
    """Мини-проект: ``core/`` с подпакетом, ``cli/`` с двумя командами.

    Рядом нарочно лежат ``tests/``, ``data/api_keys.json``, ``core/api_keys.json``
    и ``__pycache__``: ни один из них не должен попасть в копию.
    """
    source = root / "project"
    (source / "core" / "collectors").mkdir(parents=True)
    (source / "core" / "__init__.py").write_text('"""core"""\n', encoding="utf-8", newline="\n")
    (source / "core" / "http.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    (source / "core" / "collectors" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (source / "core" / "collectors" / "jobs.py").write_text(
        "VALUE = 2\n", encoding="utf-8", newline="\n"
    )
    (source / "core" / "__pycache__").mkdir()
    (source / "core" / "__pycache__" / "http.cpython-311.pyc").write_bytes(b"\x00cache")
    (source / "core" / "api_keys.json").write_text('{"providers": {}}', encoding="utf-8")

    (source / "cli").mkdir()
    (source / "cli" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (source / "cli" / "_common.py").write_text("VALUE = 3\n", encoding="utf-8", newline="\n")
    (source / "cli" / "verify.py").write_text(
        "def main(argv=None):\n    return 0\n", encoding="utf-8", newline="\n"
    )
    (source / "cli" / "collect.py").write_text(
        "def main(argv=None):\n    return 0\n", encoding="utf-8", newline="\n"
    )

    (source / "tests").mkdir()
    (source / "tests" / "test_something.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (source / "data").mkdir()
    (source / "data" / "api_keys.json").write_text('{"providers": {}}', encoding="utf-8")
    return source


def make_skill(root: Path) -> Path:
    """Каталог скилла с ``SKILL.md`` и пустым ``scripts/``."""
    skill = root / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# skill\n", encoding="utf-8", newline="\n")
    return skill


def test_sync_copies_core_cli_and_entrypoint(tmp_path: Path) -> None:
    """Копия содержит ``core/`` с подпакетом, ``cli/`` и сгенерированные файлы."""
    source = make_source(tmp_path)
    skill = make_skill(tmp_path)

    result = sync(skill, source_root=source, smoke=False)

    assert result.code == 0, result.messages
    scripts = skill / "scripts"
    assert (scripts / "core" / "http.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (scripts / "core" / "collectors" / "jobs.py").is_file()
    assert (scripts / "cli" / "verify.py").is_file()
    assert (scripts / "__init__.py").read_text(encoding="utf-8") == INIT_PY
    run_py = (scripts / "run.py").read_text(encoding="utf-8")
    assert "sys.path.insert" in run_py
    assert "from cli import" in run_py
    assert 'sys.exit(main())' in run_py
    # run.py знает только команды: _common и __init__ в список не попадают.
    assert '"verify": verify,' in run_py and '"collect": collect,' in run_py
    assert "_common" not in run_py


def test_dry_run_copies_nothing(tmp_path: Path) -> None:
    """``dry_run`` печатает план и не создаёт ``scripts/``."""
    source = make_source(tmp_path)
    skill = make_skill(tmp_path)

    result = sync(skill, source_root=source, dry_run=True, smoke=False)

    assert result.code == 0, result.messages
    assert not (skill / "scripts").exists()
    assert result.copied == []
    assert any("scripts/core/http.py" in message for message in result.messages)


def test_check_is_clean_after_sync(tmp_path: Path) -> None:
    """После синхронизации ``--check`` находит ноль расхождений."""
    source = make_source(tmp_path)
    skill = make_skill(tmp_path)
    assert sync(skill, source_root=source, smoke=False).code == 0

    result = sync(skill, source_root=source, check=True, smoke=False)

    assert result.code == 0, result.messages
    assert result.drift == []


def test_check_detects_drift(tmp_path: Path) -> None:
    """Правка в копии — код 2 и сообщение о конкретном файле."""
    source = make_source(tmp_path)
    skill = make_skill(tmp_path)
    assert sync(skill, source_root=source, smoke=False).code == 0
    copied = skill / "scripts" / "core" / "http.py"
    copied.write_text("VALUE = 99  # правили руками\n", encoding="utf-8", newline="\n")

    result = sync(skill, source_root=source, check=True, smoke=False)

    assert result.code == 2
    assert any("scripts/core/http.py" in item for item in result.drift)


def test_check_detects_missing_file(tmp_path: Path) -> None:
    """Удалённый из копии файл и лишний модуль в ``core/`` — расхождения (код 2)."""
    source = make_source(tmp_path)
    skill = make_skill(tmp_path)
    assert sync(skill, source_root=source, smoke=False).code == 0
    (skill / "scripts" / "core" / "extra.py").write_text("лишний\n", encoding="utf-8", newline="\n")
    (skill / "scripts" / "cli" / "collect.py").unlink()

    result = sync(skill, source_root=source, check=True, smoke=False)

    assert result.code == 2
    assert any("нет файла: scripts/cli/collect.py" in item for item in result.drift)
    assert any("лишний файл" in item and "scripts/core/extra.py" in item for item in result.drift)


def test_target_without_skill_md_fails(tmp_path: Path) -> None:
    """Каталог без ``SKILL.md`` — код 1 и понятное сообщение."""
    skill = tmp_path / "not-a-skill"
    skill.mkdir()

    result = sync(skill, source_root=make_source(tmp_path), smoke=False)

    assert result.code == 1
    assert any("SKILL.md" in message for message in result.messages)
    assert not (skill / "scripts").exists()


def test_secrets_tests_and_pycache_are_not_copied(tmp_path: Path) -> None:
    """``api_keys.json``, ``tests/``, ``data/`` и ``__pycache__`` в копию не попадают."""
    source = make_source(tmp_path)
    skill = make_skill(tmp_path)

    result = sync(skill, source_root=source, smoke=False)

    assert result.code == 0, result.messages
    names = {path.name for path in (skill / "scripts").rglob("*")}
    assert "api_keys.json" not in names
    assert "test_something.py" not in names
    assert not any(path.suffix == ".pyc" for path in (skill / "scripts").rglob("*"))
    assert not (skill / "scripts" / "tests").exists()
    assert not (skill / "scripts" / "data").exists()
    # каталог данных пользователя и ключи не появляются рядом с копией
    assert not (skill / "api_keys.json").exists()


def test_main_exit_code_and_run_py_generation(tmp_path: Path) -> None:
    """``main`` возвращает код 2 при расхождении, ``render_run_py`` — валидный текст."""
    source = make_source(tmp_path)
    skill = make_skill(tmp_path)
    assert main(["--target", str(skill), "--no-smoke"]) == 0
    (skill / "scripts" / "run.py").write_text("print('x')\n", encoding="utf-8", newline="\n")

    assert main(["--target", str(skill), "--check", "--no-smoke"]) == 2

    generated = render_run_py(["collect", "verify"])
    assert generated.index("from cli import") < generated.index("MODULES = {")
    assert generated.count('": ') == 2


@pytest.mark.slow
def test_smoke_runs_copied_cli(tmp_path: Path) -> None:
    """Копия реального дерева запускается: ``python -m scripts.run ... --help``.

    Единственный тест файла с подпроцессом: он доказывает, что синхронизация
    даёт работающий скилл, а не только совпадающие хеши.
    """
    skill = make_skill(tmp_path)

    result = sync(skill, source_root=PROJECT_ROOT, smoke=True)

    assert result.code == 0, result.messages
    assert any("код 0" in message for message in result.messages)
    assert (skill / "scripts" / "cli" / "check_predecessors.py").is_file()
