# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir-сборка GapAudit (шаг P4d, DEVPLAN §12).

Сборка: ``uv pip install -e ".[packaging]"`` один раз, затем::

    uv run pyinstaller gapaudit.spec

Результат — ``dist/gapaudit/``: ``gapaudit.exe`` + ``_internal/`` (onedir, не
onefile: предсказуемая распаковка и загрузка WebView2 важнее одного файла).
Папку при запуске не чистим и DLL из ``_internal`` не переносим — это ровно
тот расклад, который PyInstaller проверяет на чужой машине.

Что попадает в пакет, кроме кода:

* ``web/views``, ``web/static`` — экраны и локальная статика (CDN в проекте нет);
* ``docs/API-KEYS.md`` — документ, который отдаёт ``/docs/API-KEYS.md``;
* ``tests/fixtures`` — только для демо-режима (``--demo``): он обязан работать
  в упакованном приложении так же, как в исходниках, а фикстуры весят ~100 КБ.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

#: Каталог проекта: spec лежит в корне, поэтому SPECPATH — он же.
ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "web" / "views"), "web/views"),
    (str(ROOT / "web" / "static"), "web/static"),
    (str(ROOT / "docs" / "API-KEYS.md"), "docs"),
    (str(ROOT / "tests" / "fixtures"), "tests/fixtures"),
    # Метаданные самого пакета: без них importlib.metadata.version("gapaudit")
    # в frozen падает в PackageNotFoundError, и /api/health отдаёт "0.0.0-dev"
    # (P4d-4: единственный источник версии — импортируемый metadata).
    *copy_metadata("gapaudit"),
]

hiddenimports = [
    # uvicorn подтягивает реализации через строки: без явных имён frozen-сборка
    # падает на "Could not import module ..." уже в рантайме.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # Коллекторы сигналов: перечисляются поимённо, иначе PyInstaller их не увидит
    # (AGENTS gotcha 59 — та же ловушка, что и в pyproject).
    *collect_submodules("core.collectors"),
    # Все cli-модули: в frozen их импортирует desktop._dispatch_cli через
    # importlib.import_module (gotcha 75), а PyInstaller не видит импорты
    # за importlib — без этого списка модуль не попадёт в сборку и задача
    # упадёт с «не удалось импортировать».
    *collect_submodules("cli"),
]

a = Analysis(
    ["desktop.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Ничего из этого приложению не нужно, а весит много.
    excludes=["tkinter", "matplotlib", "pytest", "mypy", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="gapaudit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # GUI-приложение: консольного окна нет. Диагностика — в stderr, если запускать
    # из терминала (сборка остаётся оконной, чтобы двойной клик не открывал консоль).
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="gapaudit",
)
