"""Синхронизация ``core/`` и CLI-обёрток в скилл ``ecosystem-gap-audit`` (шаг P4d).

Истина кода — этот проект (``C:\\Users\\malik\\gapaudit``), скилл — автономная
копия: один каталог, никаких импортов из проекта. Скилл живёт по правилам
Hermes (``SKILL.md`` в корне, код в ``scripts/``), поэтому копирование — это
не «git subtree», а явное зеркало + одна сгенерированная точка входа.

Что копируется:

* ``core/**/*.py``        -> ``<skill>/scripts/core/``;
* ``cli/*.py``            -> ``<skill>/scripts/cli/``;
* генерируются ``<skill>/scripts/__init__.py`` и ``<skill>/scripts/run.py`` —
  точка входа ``python -m scripts.run <имя>`` (сами обёртки не правятся:
  они импортируют ``from core.* import ...``, а ``run.py`` кладёт каталог
  ``scripts/`` первым в ``sys.path``).

Не копируется никогда: ``__pycache__``, ``.pyc``, ``tests/``, ``data/``,
``api_keys.json``, ``.venv`` — ключи и рабочие данные остаются вне скилла
(AGENTS gotcha 60: репозиторий публичный).

Запуск::

    python -m tools.sync_skill --target <path-to-skill> [--dry-run] [--check]
                               [--no-smoke]

Коды выхода: 0 — всё синхронизировано (или расхождений нет), 1 — ошибка
(нет SKILL.md, нечего копировать, копия не запускается), 2 — ``--check`` и
есть расхождения.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "INIT_PY",
    "PROJECT_ROOT",
    "SyncResult",
    "build_parser",
    "main",
    "render_run_py",
    "sync",
]

#: Корень проекта: от него берутся ``core/`` и ``cli/``; каталог не зависит от cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Каталоги проекта, попадающие в скилл: (источник, приёмник внутри ``scripts/``).
TREES: tuple[tuple[str, str], ...] = (("core", "core"), ("cli", "cli"))

#: Служебные каталоги, которые не копируются даже внутри ``core/``/``cli/``.
SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})

#: Файлы-секреты: их не должно быть в копии, даже если кто-то положил их рядом.
SKIP_NAMES = frozenset({"api_keys.json"})

#: Модули ``cli/``, которые не являются командами (инфраструктура и пакет).
NOT_COMMANDS = frozenset({"_common", "__init__"})

#: Проверочная команда: копия обязана запускаться без проекта.
SMOKE_COMMAND = ("check_predecessors", "--help")

INIT_PY = '''"""Копия ``core/`` и ``cli/`` проекта GapAudit (генерируется tools/sync_skill.py)."""

from __future__ import annotations
'''

RUN_PY_HEAD = '''"""Точка входа CLI скилла ecosystem-gap-audit.

Файл генерируется ``tools/sync_skill.py`` проекта GapAudit — правка вручную
бессмысленна: следующая синхронизация его перезапишет.

Копия ``core/`` и ``cli/`` лежит рядом, в этом же каталоге ``scripts/``.
Запуск (из каталога скилла)::

    python -m scripts.run <имя> [аргументы]

Имена команд — те же, что у проекта: ``catalog_build``, ``catalog_scan``,
``check_predecessors``, ``collect``, ``coverage``, ``niche``, ``signals``,
``verify``. Флаги и коды выхода не меняются: это копия, а не форк.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Копия ``core/`` и ``cli/`` лежит в этом же каталоге: обёртки импортируют
# ``from core.* import ...`` и должны видеть копию скилла, а не одноимённый
# пакет из site-packages.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli import (  # noqa: E402 - импорт обязан идти после правки sys.path
'''

RUN_PY_TAIL = '''
)

#: Имя команды -> модуль CLI (имена совпадают с модулями ``cli/``).
MODULES = {
'''

RUN_PY_END = '''}


def main(argv: list[str] | None = None) -> int:
    """``python -m scripts.run <имя> [аргументы]`` -> код выхода команды."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["-h"], ["--help"]):
        print("usage: python -m scripts.run <имя> [аргументы]")
        print("команды: " + ", ".join(sorted(MODULES)))
        return 0
    if not args:
        print("usage: python -m scripts.run <имя> [аргументы]", file=sys.stderr)
        print("команды: " + ", ".join(sorted(MODULES)), file=sys.stderr)
        return 2
    name, rest = args[0], args[1:]
    module = MODULES.get(name)
    if module is None:
        print(
            f"неизвестная команда {name!r}; доступно: " + ", ".join(sorted(MODULES)),
            file=sys.stderr,
        )
        return 2
    return int(module.main(rest) or 0)


if __name__ == "__main__":
    sys.exit(main())
'''


@dataclass
class SyncResult:
    """Итог синхронизации: код выхода, сообщения, что скопировано и где расхождения."""

    code: int
    messages: list[str] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True — код 0 (синхронизировано или расхождений нет)."""
        return self.code == 0


def render_run_py(modules: Sequence[str]) -> str:
    """Текст ``scripts/run.py`` под список модулей-команд (отсортированный)."""
    imports = "".join(f"    {name},\n" for name in modules)
    entries = "".join(f'    "{name}": {name},\n' for name in modules)
    return RUN_PY_HEAD + imports + RUN_PY_TAIL + entries + RUN_PY_END


def sync(
    target: str | Path,
    *,
    source_root: str | Path = PROJECT_ROOT,
    dry_run: bool = False,
    check: bool = False,
    smoke: bool = True,
) -> SyncResult:
    """Скопировать ``core/`` и ``cli/`` проекта в скилл ``target``.

    ``check`` — только сравнить (код 2 при расхождениях), ``dry_run`` —
    показать план (код 0). ``smoke`` — после копии/успешной проверки запустить
    ``python -m scripts.run check_predecessors --help`` в каталоге скилла:
    копия, которая не запускается, — это не синхронизация.
    """
    root = Path(source_root).expanduser()
    skill = Path(target).expanduser()
    scripts = skill / "scripts"

    if not skill.is_dir():
        return SyncResult(1, [f"каталог скилла не найден: {skill}"])
    if not (skill / "SKILL.md").is_file():
        return SyncResult(1, [f"в {skill} нет SKILL.md — это не каталог скилла"])

    planned = _planned_files(root)
    if not planned:
        return SyncResult(1, [f"нечего копировать: в {root} нет core/ или cli/"])
    texts = _planned_texts(root)

    drift = _drift(scripts, planned, texts)
    result = SyncResult(0, [], [], drift)
    if drift:
        result.messages.append(f"расхождения с проектом: {len(drift)}")
        result.messages.extend(f"  {item}" for item in drift)

    if check:
        if drift:
            result.code = 2
            return result
        if smoke:
            ok, message = _smoke_skill(skill)
            result.messages.append(message)
            result.code = 0 if ok else 1
        return result

    if dry_run:
        for relative in sorted(planned) + sorted(texts):
            result.messages.append(f"было бы скопировано: scripts/{relative}")
        result.messages.append(f"всего файлов: {len(planned) + len(texts)} (dry-run: не копировал)")
        return result

    for relative, source in sorted(planned.items()):
        destination = scripts / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # copyfile, а не copy2: байты как есть, без метаданных источника.
        shutil.copyfile(source, destination)
        result.copied.append(relative)
    for relative, text in sorted(texts.items()):
        destination = scripts / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_text(destination, text)
        result.copied.append(relative)
    result.messages.append(f"скопировано файлов: {len(result.copied)} -> {scripts}")

    if smoke:
        ok, message = _smoke_skill(skill)
        result.messages.append(message)
        if not ok:
            result.code = 1
    return result


def build_parser() -> argparse.ArgumentParser:
    """Аргументы: ``--target`` обязателен, ``--dry-run``/``--check`` исключают друг друга."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.sync_skill",
        description="Синхронизация core/ и cli/ в скилл ecosystem-gap-audit",
    )
    parser.add_argument(
        "--target",
        metavar="PATH",
        required=True,
        help="каталог скилла (в нём обязан лежать SKILL.md)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="показать план, ничего не копировать")
    mode.add_argument(
        "--check", action="store_true", help="только сравнить с проектом (код 2 при расхождениях)"
    )
    parser.add_argument(
        "--no-smoke",
        action="store_true",
        help="не запускать проверочную команду python -m scripts.run check_predecessors --help",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа: ``python -m tools.sync_skill --target ... [--dry-run|--check]``."""
    args = build_parser().parse_args(argv)
    result = sync(args.target, dry_run=args.dry_run, check=args.check, smoke=not args.no_smoke)
    for message in result.messages:
        print(message)
    return result.code


# --- внутреннее ----------------------------------------------------------------


def _planned_files(root: Path) -> dict[str, Path]:
    """Относительный путь в ``scripts/`` -> файл проекта (только ``.py``)."""
    planned: dict[str, Path] = {}
    for source_dir, destination_dir in TREES:
        for relative, path in _tree_files(root / source_dir).items():
            planned[f"{destination_dir}/{relative}"] = path
    return planned


def _planned_texts(root: Path) -> dict[str, str]:
    """Сгенерированные файлы ``scripts/``: точка входа и ``__init__.py``."""
    return {
        "__init__.py": INIT_PY,
        "run.py": render_run_py(_command_names(root / "cli")),
    }


def _command_names(cli_dir: Path) -> list[str]:
    """Имена модулей-команд в ``cli/`` (без ``_common`` и ``__init__``)."""
    names = {path.stem for path in _tree_files(cli_dir).values()}
    return sorted(name for name in names if name not in NOT_COMMANDS)


def _tree_files(root: Path) -> dict[str, Path]:
    """``*.py`` под ``root``: относительный posix-путь -> файл.

    Служебные каталоги и файлы-секреты отбрасываются здесь, а не на этапе
    копирования: тогда и ``--check``, и ``--dry-run`` видят ровно тот набор,
    который попадёт в скилл.
    """
    files: dict[str, Path] = {}
    if not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix != ".py":
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.name in SKIP_NAMES:
            continue
        files[relative.as_posix()] = path
    return files


def _drift(
    scripts: Path, planned: dict[str, Path], texts: dict[str, str]
) -> list[str]:
    """Список расхождений копии с проектом (пусто — копия совпадает).

    Сравниваются байты: ``core``/``cli`` копируются как есть, а ``run.py``
    считается по списку модулей — поэтому «файл есть, но текст другой» ловится
    без разбора AST.
    """
    expected: dict[str, bytes] = {
        relative: path.read_bytes() for relative, path in planned.items()
    }
    for relative, text in texts.items():
        expected[relative] = text.encode("utf-8")

    actual: dict[str, Path] = {}
    for _, destination_dir in TREES:
        for relative, path in _tree_files(scripts / destination_dir).items():
            actual[f"{destination_dir}/{relative}"] = path
    for relative in texts:
        path = scripts / relative
        if path.is_file():
            actual[relative] = path

    drift: list[str] = []
    for relative, body in expected.items():
        existing = actual.get(relative)
        if existing is None:
            drift.append(f"нет файла: scripts/{relative}")
        elif existing.read_bytes() != body:
            drift.append(f"отличается: scripts/{relative}")
    for relative in actual:
        if relative not in expected:
            drift.append(f"лишний файл в копии: scripts/{relative}")
    return sorted(drift)


def _smoke_skill(skill: Path) -> tuple[bool, str]:
    """Запустить копию: ``python -m scripts.run check_predecessors --help``.

    ``cwd`` — каталог скилла (``-m scripts.run`` ищет пакет оттуда), ``PYTHONPATH``
    дублирует его же для страховки. ``PYTHONIOENCODING=utf-8``: справка на
    русском, а на Windows консоль по умолчанию cp1251.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(skill)
    command = [sys.executable, "-m", "scripts.run", *SMOKE_COMMAND]
    try:
        proc = subprocess.run(
            command,
            cwd=str(skill),
            capture_output=True,
            env=env,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:  # noqa: BLE001 - копия не запустилась
        return False, f"проверка копии не прошла: {error}"
    if proc.returncode == 0:
        return True, "проверка копии: python -m scripts.run check_predecessors --help — код 0"
    tail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip().splitlines()[-3:]
    return False, "проверка копии не прошла: " + " | ".join(tail)


def _write_text(path: Path, text: str) -> None:
    """Записать текст с LF (Windows иначе даёт CRLF — AGENTS gotcha 17)."""
    path.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.exit(main())
