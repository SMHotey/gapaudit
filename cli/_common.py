"""Общая инфраструктура CLI-обёрток (шаг 1f).

Здесь живёт всё, что одинаково у шести скриптов: разбор общих флагов
(``--data-dir``, ``--settings``, ``--cache-path``, ``--keyword``, ``--json-out``,
``--no-cache``, ``--quiet``), сборка рантайма (настройки файл -> окружение ->
CLI + :class:`core.http.HttpClient` с файловым кэшем), печать таблиц, атомарная
запись JSON, коды выхода и проверка ``gh auth status``.

Правила слоя ``cli`` (DEVPLAN §3, §4): только разбор аргументов, форматирование
и коды выхода — вся логика в ``core``. Модуль ничего не печатает на импорте и
не трогает файлы: кэш и настройки поднимаются в :func:`load_runtime`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from core import http as http_module
from core.cache import DictCache, FileCache
from core.http import HttpClient, default_throttle
from core.settings import (
    ENV_CACHE_TTL_H,
    ENV_DATA_DIR,
    ENV_KEYWORDS,
    ENV_MAX_JOBS,
    SETTINGS_FILENAME,
    Settings,
    from_env,
    load as load_settings,
    resolve_data_dir,
)
from core.github import GhClient

__all__ = [
    "build_argparser",
    "exit_code",
    "load_runtime",
    "print_table",
    "setup_gh_or_warn",
    "write_json",
]

#: Единая строка предупреждения для скриптов, которым нужен авторизованный gh.
GH_WARN = (
    "[gapaudit] gh не авторизован — доступно только check_predecessors, "
    "лимиты 60/ч (авторизуйтесь: gh auth login)"
)


def build_argparser(*, prog: str, description: str) -> argparse.ArgumentParser:
    """argparse с общими флагами всех шести скриптов.

    ``prog`` — имя скрипта для ``--help``; общие флаги складываются в отдельную
    группу, чтобы в справке они не смешивались с флагами метода.
    """
    parser = argparse.ArgumentParser(prog=prog, description=description)
    common = parser.add_argument_group("общие параметры (cli/_common.py)")
    common.add_argument(
        "--data-dir", metavar="PATH",
        help="каталог данных (переопределяет Settings.data_dir и GAPAUDIT_DATA_DIR)",
    )
    common.add_argument(
        "--settings", metavar="PATH",
        help=f"файл настроек (по умолчанию <data_dir>/{SETTINGS_FILENAME})",
    )
    common.add_argument(
        "--cache-path", metavar="PATH",
        help="файл HTTP-кэша (по умолчанию <data_dir>/cache.json)",
    )
    common.add_argument(
        "--keyword", action="append", metavar="СЛОВО",
        help="признак ниши; можно повторять для синонимов (переопределяет Settings.keywords)",
    )
    common.add_argument(
        "--json-out", metavar="FILE",
        help="куда записать сводный JSON (сравнение во времени, потребление из UI)",
    )
    common.add_argument("--no-cache", action="store_true", help="обойти кэш (сеть принудительно, без записи)")
    common.add_argument("--quiet", action="store_true", help="не печатать таблицу, только JSON/итог")
    return parser


def load_runtime(args: argparse.Namespace) -> tuple[Settings, HttpClient]:
    """Собрать настройки и HTTP-клиента: файл -> окружение -> CLI.

    Порядок наложения строго «файл, затем окружение, затем CLI» (DEVPLAN §4):
    UI меняет файл, CI — окружением, разовый прогон — флагами.

    ``ValueError`` от guard'а ``resolve_data_dir`` (путь указал на корень проекта)
    пробрасывается — обрабатывает вызывающий скрипт.
    """
    data_dir_hint = (getattr(args, "data_dir", None) or "").strip()
    env_data_dir = (os.environ.get(ENV_DATA_DIR) or "").strip()

    if getattr(args, "settings", None):
        settings_path = Path(args.settings)
    else:
        base = data_dir_hint or env_data_dir
        root = Path(base).expanduser() if base else resolve_data_dir(Settings())
        settings_path = root / SETTINGS_FILENAME

    settings = load_settings(settings_path)
    _apply_env(settings)

    if data_dir_hint:
        settings.data_dir = data_dir_hint
    keywords = getattr(args, "keyword", None)
    if keywords:
        settings.keywords = tuple(word.lower() for word in keywords)

    data_dir = resolve_data_dir(settings)
    cache_path = getattr(args, "cache_path", None) or str(data_dir / "cache.json")

    if getattr(args, "no_cache", False):
        # Обход кэша: память вместо файла — сеть принудительно, диск не трогаем.
        client = HttpClient(cache=DictCache(settings.cache_ttl_hours), throttle=default_throttle())
    else:
        # Вызов через модуль, чтобы тесты могли подменить core.http.default_client.
        client = http_module.default_client(cache_path)
        if isinstance(client.cache, FileCache):
            client.cache.ttl_hours = float(settings.cache_ttl_hours)
    return settings, client


def print_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    aligns: list[str] | None = None,
) -> None:
    """Печатает простую текстовую таблицу (как в legacy).

    ``aligns`` — выравнивание по столбцам: ``'l'``/``'r'``/``'c'``, по умолчанию
    все ``'l'``. Ширина столбца — по max(заголовок, ячейки) + 2.
    """
    if not headers:
        return
    scheme = list(aligns or [])
    scheme += ["l"] * (len(headers) - len(scheme))

    widths: list[int] = []
    for index, header in enumerate(headers):
        width = len(str(header))
        for row in rows:
            if index < len(row) and row[index] is not None:
                width = max(width, len(str(row[index])))
        widths.append(width + 2)

    def fmt(cells: list[str]) -> str:
        parts: list[str] = []
        for index, cell in enumerate(cells):
            text = "" if cell is None else str(cell)
            align = scheme[index] if index < len(scheme) else "l"
            if align == "r":
                parts.append(text.rjust(widths[index]))
            elif align == "c":
                parts.append(text.center(widths[index]))
            else:
                parts.append(text.ljust(widths[index]))
        return "".join(parts).rstrip()

    print(fmt(headers))
    for row in rows:
        print(fmt(row))


def write_json(path: str | Path, data: Any) -> None:
    """Записать JSON атомарно: временный файл рядом + ``os.replace``.

    ``indent=1``, ``ensure_ascii=False`` — как в legacy-файлах. ``newline="\\n"``:
    на Windows текстовый режим иначе дал бы CRLF (AGENTS.md, gotcha 17).
    Родительский каталог создаётся.
    """
    target = Path(path)
    directory = target.parent if str(target.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)

    handle_fd, tmp_path = tempfile.mkstemp(prefix=".gapaudit-", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def exit_code(strict: bool, has_live: bool, has_flag: bool) -> int:
    """Код выхода ``--strict``-гейта: ``2``, если строгий режим и есть срабатывание."""
    return 2 if strict and (has_live or has_flag) else 0


def setup_gh_or_warn() -> tuple[bool, str]:
    """Проверить ``gh auth status``; без авторизации — одна строка в stderr.

    Не падает и не блокирует запуск: check_predecessors живёт без gh, а скрипты,
    которым он нужен, покажут пустые находки и предупреждение. Возвращает
    ``(авторизован, сообщение)``.
    """
    ok, _text = GhClient().auth_status()
    if ok:
        return True, ""
    print(GH_WARN, file=sys.stderr)
    return False, GH_WARN


# --- внутреннее ---------------------------------------------------------------


def _apply_env(settings: Settings) -> None:
    """Наложить окружение на настройки из файла: что задано в env, то перекрывает.

    ``from_env`` возвращает «дефолты + заданное», поэтому значения сравниваются с
    переменными напрямую: незаданная переменная не должна затирать файл дефолтом.
    """
    source = os.environ
    env = from_env()
    if (source.get(ENV_DATA_DIR) or "").strip():
        settings.data_dir = env.data_dir
    if (source.get(ENV_CACHE_TTL_H) or "").strip():
        settings.cache_ttl_hours = env.cache_ttl_hours
    if (source.get(ENV_KEYWORDS) or "").strip():
        settings.keywords = env.keywords
    if (source.get(ENV_MAX_JOBS) or "").strip():
        settings.max_parallel_jobs = env.max_parallel_jobs
