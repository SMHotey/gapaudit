"""Общая инфраструктура CLI-обёрток (шаг 1f).

Здесь живёт всё, что одинаково у шести скриптов: разбор общих флагов
(``--data-dir``, ``--settings``, ``--cache-path``, ``--keyword``, ``--json-out``,
``--job-json``, ``--no-cache``, ``--quiet``), сборка рантайма (настройки файл ->
окружение -> CLI + :class:`core.http.HttpClient` с файловым кэшем), печать таблиц,
атомарная запись JSON, коды выхода и проверка ``gh auth status``.

Протокол очереди задач (шаг 3d-a, DEVPLAN §6): ``--job-json`` включает JSON-lines
в stdout — ``hello``/``progress``/``done``/``error`` через :func:`emit_hello`,
:func:`build_job_emitter`, :func:`emit_done`, :func:`emit_error`. Таблицы и итоги
при этом уходят в stderr (их читает лог задачи, ``app/jobs.py``), поэтому stdout
остаётся чистым протоколом.

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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core import http as http_module
from core.cache import DictCache, FileCache
from core.http import HttpClient, Throttle, Transport, default_throttle
from core.registry import npm_url, pypi_url, pypistats_url
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
    "ENV_DEMO",
    "ENV_FAKE_HTTP",
    "build_argparser",
    "build_job_emitter",
    "emit_done",
    "emit_error",
    "emit_hello",
    "exit_code",
    "job_json_mode",
    "job_json_requested",
    "load_runtime",
    "make_fake_transport_from_fixtures",
    "print_table",
    "setup_gh_or_warn",
    "write_json",
]

#: Единая строка предупреждения для скриптов, которым нужен авторизованный gh.
GH_WARN = (
    "[gapaudit] gh не авторизован — доступно только check_predecessors, "
    "лимиты 60/ч (авторизуйтесь: gh auth login)"
)

#: Переменная окружения тестового бэкдора: HTTP-ответы из ``tests/fixtures/http``.
ENV_FAKE_HTTP = "GAPAUDIT_FAKE_HTTP"

#: Переменная окружения демо-режима (шаг P4d): то же, что флаг ``--demo``.
ENV_DEMO = "GAPAUDIT_DEMO"

#: Каталог фикстур HTTP — в репозитории, не в каталоге данных.
FIXTURES_HTTP_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "http"

#: Настоящий stdout при ``--job-json``: протокол пишется в него, а не в подменённый.
_job_stream: Any = None


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
    common.add_argument(
        "--job-json", action="store_true",
        help="протокол JSON-lines в stdout для очереди задач (таблицы при этом — в stderr)",
    )
    common.add_argument(
        "--demo", action="store_true",
        help="демо-режим: настройки по умолчанию, HTTP — из tests/fixtures, сеть выключена",
    )
    common.add_argument("--no-cache", action="store_true", help="обойти кэш (сеть принудительно, без записи)")
    common.add_argument("--quiet", action="store_true", help="не печатать таблицу, только JSON/итог")
    return parser


def load_runtime(args: argparse.Namespace) -> tuple[Settings, HttpClient]:
    """Собрать настройки и HTTP-клиента: файл -> окружение -> CLI.

    Порядок наложения строго «файл, затем окружение, затем CLI» (DEVPLAN §4):
    UI меняет файл, CI — окружением, разовый прогон — флагами.

    Демо-режим (``--demo`` или ``GAPAUDIT_DEMO=1``, шаг P4d) перебивает всё
    остальное и ничего не читает с диска: настройки — дефолты, транспорт —
    фикстуры, кэш — в памяти, ``gh`` — снимок ответа (``tools.demo_mode``).

    ``ValueError`` от guard'а ``resolve_data_dir`` (путь указал на корень проекта)
    пробрасывается — обрабатывает вызывающий скрипт.
    """
    if _demo_enabled(args):
        return _demo_runtime(args)

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

    if os.environ.get(ENV_FAKE_HTTP) == "1":
        # Тестовый бэкдор (AGENTS gotcha 30): сеть заменена фикстурами, кэш — в
        # памяти, троттлинг выключен. Только для сквозных тестов POST -> CLI -> SSE.
        return settings, HttpClient(
            cache=DictCache(settings.cache_ttl_hours),
            throttle=Throttle(default_per_minute=0),
            transport=make_fake_transport_from_fixtures(),
        )

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


# --- протокол очереди задач (--job-json, шаг 3d-a) -----------------------------


def _write_json_line(payload: dict) -> None:
    """Одна строка JSON-lines в stdout (или в настоящий stdout при ``--job-json``)."""
    stream = _job_stream if _job_stream is not None else sys.stdout
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def emit_hello() -> None:
    """``{"type": "hello", "pid": ...}`` — первое сообщение задачи (DEVPLAN §6)."""
    _write_json_line({"type": "hello", "pid": os.getpid()})


def emit_done() -> None:
    """``{"type": "done"}`` — задача отработала успешно."""
    _write_json_line({"type": "done"})


def emit_error(message: str) -> None:
    """``{"type": "error", "error": ...}`` — ошибка, которую увидит пользователь."""
    _write_json_line({"type": "error", "error": str(message)})


def build_job_emitter() -> Callable[[float, str], None]:
    """Возвращает ``on_progress(progress, message)``, пишущий JSON-lines.

    Прогресс нормализуется в ``[0, 1]``: подпроцесс не должен уметь отправить
    ``1.5``, из-за которого UI показал бы 150%.
    """

    def emit(progress: float, message: str) -> None:
        value = max(0.0, min(1.0, float(progress)))
        _write_json_line({"type": "progress", "progress": value, "message": str(message)})

    return emit


def job_json_requested(argv: list[str] | None = None) -> bool:
    """Есть ли ``--job-json`` среди аргументов (``None`` — ``sys.argv[1:]``)."""
    raw = sys.argv[1:] if argv is None else list(argv)
    return "--job-json" in raw


@contextmanager
def job_json_mode() -> Iterator[None]:
    """Режим JSON-lines: stdout — только протокол, прочий вывод — в stderr.

    Таблицы и итоги CLI под ``--job-json`` уходят в stderr: их читает лог задачи
    (``app/jobs.py`` пишет нераспознанный stdout и весь stderr в ``jobs/<id>.log``),
    а в stdout остаются только строки протокола — иначе таблица шла бы в лог как
    «мусорная» строка stdout и мешала разбору. ``hello`` отправляется сразу при входе.

    Выход из контекста возвращает ``sys.stdout`` на место: тесты зовут ``main()``
    in-process и обязаны видеть свой stdout.
    """
    global _job_stream
    previous = sys.stdout
    _job_stream = previous
    sys.stdout = sys.stderr
    try:
        emit_hello()
        yield
    finally:
        sys.stdout = previous
        _job_stream = None


def make_fake_transport_from_fixtures() -> Transport:
    """Транспорт из ``tests/fixtures/http`` для ``GAPAUDIT_FAKE_HTTP=1``.

    Обслуживается ровно один пакет — ``requests``: на нём построены фикстуры
    ``pypi_requests.json``, ``npm_requests.json``, ``pypistats_requests.json``
    (сняты с живых API в шаге 1d). Любой другой URL — ``(404, b"")``: «пакета
    нет» — валидный ответ для метода, а не повод падать.

    Фикстур нет — ``RuntimeError`` с путём: тихий 404 скрыл бы, что бэкдор
    включён без данных (AGENTS gotcha 30).
    """
    files = {
        "pypi": FIXTURES_HTTP_DIR / "pypi_requests.json",
        "npm": FIXTURES_HTTP_DIR / "npm_requests.json",
        "pypistats": FIXTURES_HTTP_DIR / "pypistats_requests.json",
    }
    missing = [path.name for path in files.values() if not path.is_file()]
    if missing:
        raise RuntimeError(
            f"{ENV_FAKE_HTTP}=1, но нет фикстур HTTP: {', '.join(missing)} "
            f"(ожидались в {FIXTURES_HTTP_DIR})"
        )
    routes = {
        pypi_url("requests"): files["pypi"].read_bytes(),
        npm_url("requests"): files["npm"].read_bytes(),
        pypistats_url("requests"): files["pypistats"].read_bytes(),
    }

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        body = routes.get(url)
        if body is None:
            return 404, b""
        return 200, body

    return transport


# --- внутреннее ---------------------------------------------------------------


def _demo_enabled(args: argparse.Namespace) -> bool:
    """Демо-режим включён флагом ``--demo`` или переменной ``GAPAUDIT_DEMO=1``."""
    return bool(getattr(args, "demo", False)) or os.environ.get(ENV_DEMO) == "1"


def _demo_runtime(args: argparse.Namespace) -> tuple[Settings, HttpClient]:
    """Рантайм демо-режима (шаг P4d): дефолты, фикстуры, память — без диска.

    Импорт локальный: ``tools/demo_mode.py`` живёт в проекте, а копия
    ``cli/_common.py`` в скилле — без него. В скилле демо-режима нет, и
    сообщение об этом должно быть понятным, а не трейсбеком импорта.
    """
    try:
        from tools.demo_mode import (
            install_demo_gh,
            make_demo_http_client,
            make_demo_settings,
        )
    except ModuleNotFoundError as error:  # понятная диагностика вместо трейсбека
        raise SystemExit(
            f"[gapaudit] демо-режим доступен только в проекте GapAudit "
            f"(нет tools/demo_mode.py): {error}"
        ) from error

    install_demo_gh()
    settings = make_demo_settings()
    keywords = getattr(args, "keyword", None)
    if keywords:
        settings.keywords = tuple(word.lower() for word in keywords)
    print(
        f"[gapaudit] демо-режим: данные из фикстур, сеть выключена "
        f"({FIXTURES_HTTP_DIR.parent})",
        file=sys.stderr,
    )
    return settings, make_demo_http_client(settings)


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
