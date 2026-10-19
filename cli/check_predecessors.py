"""Проверка кандидатов и предшественников — CLI-обёртка (шаг 1f).

Порт ``legacy/check_predecessors.py``: те же режимы, флаги и коды выхода
(0 — обычный прогон, 1 — нечего проверять, 2 — ``--strict``-гейт), но логика
живёт в ``core.packages``/``core.diffing``, кэш и настройки — в ``cli/_common``.

Режимы:
  (по умолчанию)        таблица: версия, дата, скачивания, MCP-поверхность, вердикт
  --check-name ИМЯ      свободно ли имя в PyPI/npm (проверка перед публикацией)
  --from FILE           имена из requirements.txt / package.json / pyproject.toml
  --diff FILE           что изменилось с прошлого прогона (мониторинг конкурентов)
  --similar ИМЯ         похожие имена среди популярных (типосквоттинг, supply chain)

Запуск:
  python -m cli.check_predecessors gerenuk redherring ci-triage
  python -m cli.check_predecessors --top 30 --csv out.csv
  python -m cli.check_predecessors --from requirements.txt --diff raw/predecessors.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from typing import Any

from cli._common import (
    build_argparser,
    build_job_emitter,
    emit_done,
    emit_error,
    emit_hello,
    exit_code,
    load_runtime,
    print_table,
    setup_gh_or_warn,
    write_json,
)
from core.diffing import VERDICT_ABSENT, VERDICT_ALIVE, diff_observations
from core.packages import PackageCheckConfig, check, check_many, names_from_file, similar_names
from core.registry import RegistryClient

__all__ = ["main"]

#: Кандидаты на файл топа для ``--top``, как в legacy (cwd, dev-режим, каталог данных).
GAP_TOOLS_CANDIDATES = ("raw/gap_tools.json", "../data/gap_tools.json", "gap_tools.json")

#: Подписи событий сравнения — как печатал legacy ``diff_runs()``.
KIND_LABELS = {
    "new": "НОВОЕ",
    "missing": "ПРОПАЛО",
    "release": "РЕЛИЗ",
    "flag_appeared": "MCP ПОЯВИЛСЯ",
    "flag_removed": "MCP УБРАН?",
    "adoption_change": "ADOPTION",
    "verdict_change": "ВЕРДИКТ",
}

#: Файл результата по умолчанию — как в legacy.
DEFAULT_JSON = "raw/predecessors.json"

#: Имён на чанк: после каждого чанка под ``--job-json`` уходит progress-событие.
#: ``check_many`` параллелит внутри чанка, а очередь получает прогресс достаточно
#: часто, не разменивая параллелизм на «по одному имени».
CHECK_CHUNK = 5


def observation(row: dict) -> dict:
    """Строка CLI -> форма наблюдения ``core.diffing`` (контракт таблицы ``observations``)."""
    return {
        "name": row.get("name"),
        "version": row.get("ver"),
        "released_at": row.get("date"),
        "adoption_week": row.get("week"),
        "flag": bool(row.get("mcp")),
        "verdict": row.get("verdict"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser(
        prog="check_predecessors.py",
        description="Проверка кандидатов и предшественников: npm/PyPI, adoption, MCP-поверхность",
    )
    parser.add_argument("names", nargs="*", help="имена пакетов или owner/repo")
    parser.add_argument("--top", type=int, default=0, help="взять топ-N из gap_tools.json")
    parser.add_argument("--from", dest="from_file", metavar="FILE",
                        help="requirements.txt / package.json / pyproject.toml")
    parser.add_argument("--check-name", nargs="+", metavar="ИМЯ",
                        help="свободно ли имя в PyPI/npm (для нового продукта)")
    parser.add_argument("--similar", metavar="ИМЯ", help="похожие имена (типосквоттинг)")
    parser.add_argument("--diff", metavar="PREV.json", help="сравнить с прошлым прогоном")
    parser.add_argument("--json", dest="json_file", metavar="FILE",
                        help="куда сохранить результат (алиас --json-out)")
    parser.add_argument("--csv", metavar="FILE", help="выгрузить таблицу в CSV")
    parser.add_argument("--strict", action="store_true",
                        help="код выхода 2, если есть ЖИВО или MCP=да (гейт для cron/CI)")
    args = parser.parse_args(argv)

    # JSON-lines: hello — первое сообщение и первый stdout до любых таблиц.
    if args.job_json:
        emit_hello()

    try:
        settings, http = load_runtime(args)
    except ValueError as error:
        _fail(args, error)
        return 2

    # Заголовок колонки признака: меняется вместе с --keyword, как в legacy.
    label = args.keyword[0][:6].upper() if args.keyword else "MCP"
    cfg = PackageCheckConfig(
        verdict=settings.to_verdict_config(),
        keyword_label=label,
        keywords=settings.keywords,
    )
    client = RegistryClient(http, keywords=settings.keywords)

    try:
        return _run(args, settings, http, client, cfg, label)
    except Exception as error:  # noqa: BLE001 - задача обязана отчитаться ошибкой
        # Без --job-json поведение прежнее: исключение пробрасывается (CLI падает
        # с трейсбеком, код выхода ненулевой). С флагом — протокол + код 2.
        if not args.job_json:
            raise
        _fail(args, error)
        return 2
    finally:
        if http.cache is not None:
            http.cache.save()


def _fail(args: argparse.Namespace, error: BaseException) -> None:
    """Сообщить об ошибке: строка в stderr + ``{"type": "error"}`` под --job-json."""
    print(f"[gapaudit] {error}", file=sys.stderr)
    if args.job_json:
        emit_error(str(error))


def _say(args: argparse.Namespace, text: str, *, end: str = "\n") -> None:
    """Печать для человека; под --job-json — в stderr (stdout занят протоколом)."""
    print(text, end=end, file=sys.stderr if args.job_json else sys.stdout)


# --- режимы ---------------------------------------------------------------------


def _run(
    args: argparse.Namespace,
    settings: Any,
    http: Any,
    client: RegistryClient,
    cfg: PackageCheckConfig,
    label: str,
) -> int:
    if args.check_name:
        for name in args.check_name:
            result = check(name, client, cfg=cfg)
            if not args.job_json:
                _print_availability(result, name)
        return 0

    if args.similar:
        hits = similar_names(args.similar, client)
        if not args.job_json:
            print(f"Похожие на «{args.similar}» (расстояние Левенштейна ≤ 2):")
            for candidate, distance in hits:
                print(f"  d={distance}  {candidate}")
            if not hits:
                print("  ничего похожего не найдено")
            print("Если похожее имя есть, а оно популярно — это кандидат на типосквоттинг:")
            print("сверяй точное имя перед установкой.")
        return 0

    names = list(args.names)
    if args.from_file:
        extra = names_from_file(args.from_file, fix_toml=True)
        if not args.quiet:
            _say(args, f"из {args.from_file} взято имён: {len(extra)}")
        names += extra
    if args.top:
        # Топ берёт имена из GitHub-файла, поэтому здесь gh и проверяем (один раз).
        setup_gh_or_warn()
        names += _top_names(args.top)

    names = sorted(dict.fromkeys(names))
    if not names and args.diff:
        # --diff без позиционных имён (шаг 3c): задача monitor.diff запускается
        # с одним --diff=<файл>, имён в argv нет — берём их из прошлого прогона.
        # До этого CLI отвечал «нечего проверять» и не читал baseline вообще,
        # поэтому ни одна задача мониторинга не доходила до событий.
        names = sorted(dict.fromkeys(_names_from_baseline(args.diff)))
    if not names:
        message = "нечего проверять: передай имена, --from FILE, --top N, --check-name или --similar"
        _say(args, message)
        if args.job_json:
            # Под протоколом пустой список — ошибка задачи, а не «успех с нулём
            # строк»: иначе очередь запишет пустой result.json и покажет done.
            emit_error(message)
            return 2
        return 1

    rows = _check_all(names, client, cfg, args)
    if not args.quiet and not args.job_json:
        _print_table(rows, label)
        _print_summary(rows, label)

    # Сравнение считается ДО записи JSON (шаг 3b): под --job-json в result.json
    # должны уехать события, а не наблюдения — очередь задач читает файл и
    # пишет его в таблицу events. Без --job-json файл остаётся прежним
    # (наблюдения), потому что он же служит baseline для следующего --diff.
    diff_events: list[dict] | None = None
    diff_error: str | None = None
    if args.diff and os.path.exists(args.diff):
        diff_events, diff_error = _diff_events(args.diff, rows, settings)

    json_path = args.json_file or args.json_out or DEFAULT_JSON
    payload: Any = rows
    if args.job_json and args.diff:
        payload = {"events": diff_events or [], "observations": rows}
        if diff_error is not None:
            payload["error"] = diff_error
    write_json(json_path, payload)
    _say(args, f"\nСОХРАНЕНО: {json_path}  (пригоден для --diff в следующий раз)")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        _say(args, f"CSV: {args.csv}")

    if args.diff and os.path.exists(args.diff) and not args.job_json:
        _print_events(diff_events or [], args.diff, diff_error)

    has_live = any(row["verdict"] == VERDICT_ALIVE for row in rows)
    has_flag = any(row["mcp"] for row in rows)
    if exit_code(args.strict, has_live, has_flag) == 2:
        _say(args, "STRICT: есть живые проекты или MCP-поверхность → код выхода 2")
    if args.job_json:
        emit_done()
    return exit_code(args.strict, has_live, has_flag)


def _names_from_baseline(path: str) -> list[str]:
    """Имена пакетов из прошлого прогона — fallback ``--diff`` без позиционных имён.

    Baseline — файл, который CLI пишет сам: без ``--job-json`` это список
    наблюдений, под ``--job-json`` — объект ``{"events", "observations"}``.
    Старый (до 3b) файл задачи мог называть наблюдения ``items`` — ключ тоже
    принимаем, чтобы фикс работал и на уже накопленных файлах.

    Нечитаемый или битый файл не валит прогон: причина уходит в stderr, список
    остаётся пустым, и дальше срабатывает обычная проверка «нечего проверять»
    (под ``--job-json`` — ``emit_error`` + код 2).
    """
    try:
        with open(path, encoding="utf-8") as handle:
            previous = json.load(handle)
        if isinstance(previous, dict):
            previous = previous.get("observations", previous.get("items", []))
        return [
            str(row["name"])
            for row in (previous or [])
            if isinstance(row, dict) and row.get("name")
        ]
    except Exception as error:  # noqa: BLE001 - причина уходит в stderr, не в трейсбек
        print(f"[gapaudit] не удалось прочитать {path}: {error}", file=sys.stderr)
        return []


def _check_all(
    names: list[str],
    client: RegistryClient,
    cfg: PackageCheckConfig,
    args: argparse.Namespace,
) -> list[dict]:
    """Проверить имена чанками по :data:`CHECK_CHUNK`.

    Под ``--job-json`` после каждого чанка уходит ``progress`` — очередь и SSE
    показывают движение, а не «зависло на 0». Без флага — один и тот же путь,
    просто без событий (результат и порядок строк не меняются).
    """
    on_progress = build_job_emitter() if args.job_json else None
    total = len(names)
    rows: list[dict] = []
    for start in range(0, total, CHECK_CHUNK):
        chunk = names[start : start + CHECK_CHUNK]
        rows.extend(check_many(chunk, client, cfg=cfg))
        if on_progress is not None:
            done = min(start + CHECK_CHUNK, total)
            on_progress(done / total, f"check {done}/{total}")
    return rows


def _top_names(top: int) -> list[str]:
    """Имена из ``gap_tools.json``: первый существующий кандидат, топ-N по порядку файла."""
    for candidate in GAP_TOOLS_CANDIDATES:
        if not os.path.exists(candidate):
            continue
        with open(candidate, encoding="utf-8") as handle:
            items = json.load(handle)
        return [item["full_name"].split("/")[1] for item in items[:top]]
    return []


def _print_availability(result: dict, name: str) -> None:
    """Свободно ли имя — как в legacy ``name_availability()``."""
    if result["verdict"] == VERDICT_ABSENT:
        print(f"СВОБОДНО    {name} — нет ни в PyPI, ни в npm")
        return
    downloads = result["week"] if result["week"] is not None else result["month"]
    unit = "нед" if result["week"] is not None else "мес"
    print(f"ЗАНЯТО      {name} — {result['src']}, {result['ver']}, {result['date']}, "
          f"{downloads or '?'} скач/{unit}")
    if result.get("summary"):
        print(f"            {result['summary']}")


def _print_table(rows: list[dict], label: str) -> None:
    headers = ["имя", "источник", "версия", "дата", "мес", "нед", label, "вердикт"]
    table = [
        [
            str(row["name"])[:29],
            row["src"],
            row["ver"] or "-",
            row["date"] or "-",
            "-" if row["month"] is None else str(row["month"]),
            "-" if row["week"] is None else str(row["week"]),
            "да" if row["mcp"] else "нет",
            row["verdict"],
        ]
        for row in rows
    ]
    print_table(headers, table, aligns=["l", "l", "l", "l", "r", "r", "r", "l"])
    # Строка находки обязана быть видна рядом с вердиктом (DEVPLAN §14).
    for row in rows:
        if row.get("summary"):
            print(f"  {row['name']}: {row['summary']}")


def _print_summary(rows: list[dict], label: str) -> None:
    """Итоги прогона — как в legacy ``print_summary()``."""
    counts = Counter(row["verdict"] for row in rows)
    mcp_yes = sum(1 for row in rows if row["mcp"])
    print("\nИТОГ: " + ", ".join(f"{key} {value}" for key, value in counts.most_common()))
    print(f"Признак «{label}» в самом пакете: да у {mcp_yes} из {len(rows)}")
    best = [row["name"] for row in rows if row["verdict"] == VERDICT_ALIVE and not row["mcp"]]
    if best:
        print(f"ЛУЧШИЕ КАНДИДАТЫ (живые, {label} нет): " + ", ".join(best[:10]))
    dead = [row["name"] for row in rows if row["verdict"] == VERDICT_ABSENT]
    if dead:
        print("ПУСТО (пакета нет ни в PyPI, ни в npm): " + ", ".join(dead[:10]))
    print(f"Правило: «{label}=нет» = у пакета нет этого признака — это и есть незанятая часть")
    print("ниши. МЁРТВО/СЛАБО = 'опубликовано и не принято': вход возможен, но выигрыш в качестве")
    print("и adoption, а не в новизне. Признак задаётся флагом --keyword (mcp, helm, otel, deprecated).")


def _diff_events(
    path: str, rows: list[dict], settings: Any
) -> tuple[list[dict] | None, str | None]:
    """События сравнения с прошлым прогоном: ``(events, None)`` или ``(None, причина)``.

    Вынесено из печати (шаг 3b): тем же списком событий теперь отвечает
    ``--json-out`` под ``--job-json`` — очередь задач читает файл результата и
    пишет события в таблицу ``events``.

    Ошибка чтения прошлого файла — причина строкой, а не исключение: прогон из
    битого baseline не валится (поведение legacy). Baseline, записанный
    result.json задачи (шаг 3b) — объект ``{"events", "observations"}``:
    сравнивать с ним можно, поэтому наблюдения достаются из ``observations``.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            prev = json.load(handle)
        if isinstance(prev, dict):
            prev = prev.get("observations") or []
        events = diff_observations(
            [observation(row) for row in prev],
            [observation(row) for row in rows],
            cfg=settings.to_verdict_config(),
        )
    except Exception as error:  # битый прошлый файл не должен валить прогон
        return None, str(error)
    return events, None


def _print_events(events: list[dict], path: str, error: str | None) -> None:
    """Сравнение с прошлым прогоном — события ``core.diffing`` с legacy-подписями."""
    if error is not None:
        print(f"не удалось сравнить с {path}: {error}")
        return
    print(f"\nИЗМЕНЕНИЯ с {path} ({len(events)}):")
    for event in events:
        kind = KIND_LABELS.get(event["kind"], event["kind"])
        print(f"  {kind:<14} {event['name']:<28} {event['detail']}")
    if not events:
        print("  ничего не изменилось")


if __name__ == "__main__":
    sys.exit(main())
