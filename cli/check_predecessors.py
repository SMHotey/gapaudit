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

    try:
        settings, http = load_runtime(args)
    except ValueError as error:
        print(f"[gapaudit] {error}", file=sys.stderr)
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
    finally:
        if http.cache is not None:
            http.cache.save()


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
            _print_availability(check(name, client, cfg=cfg), name)
        return 0

    if args.similar:
        hits = similar_names(args.similar, client)
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
            print(f"из {args.from_file} взято имён: {len(extra)}")
        names += extra
    if args.top:
        # Топ берёт имена из GitHub-файла, поэтому здесь gh и проверяем (один раз).
        setup_gh_or_warn()
        names += _top_names(args.top)

    names = sorted(dict.fromkeys(names))
    if not names:
        print("нечего проверять: передай имена, --from FILE, --top N, --check-name или --similar")
        return 1

    rows = check_many(names, client, cfg=cfg)
    if not args.quiet:
        _print_table(rows, label)
        _print_summary(rows, label)

    json_path = args.json_file or args.json_out or DEFAULT_JSON
    write_json(json_path, rows)
    print(f"\nСОХРАНЕНО: {json_path}  (пригоден для --diff в следующий раз)")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV: {args.csv}")

    if args.diff and os.path.exists(args.diff):
        _print_diff(args.diff, rows, settings)

    has_live = any(row["verdict"] == VERDICT_ALIVE for row in rows)
    has_flag = any(row["mcp"] for row in rows)
    if exit_code(args.strict, has_live, has_flag) == 2:
        print("STRICT: есть живые проекты или MCP-поверхность → код выхода 2")
    return exit_code(args.strict, has_live, has_flag)


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


def _print_diff(path: str, rows: list[dict], settings: Any) -> None:
    """Сравнение с прошлым прогоном — события ``core.diffing`` с legacy-подписями."""
    try:
        with open(path, encoding="utf-8") as handle:
            prev = json.load(handle)
        events = diff_observations(
            [observation(row) for row in prev],
            [observation(row) for row in rows],
            cfg=settings.to_verdict_config(),
        )
    except Exception as error:  # битый прошлый файл не должен валить прогон
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
