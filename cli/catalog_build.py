"""Сборка каталога проектов по поисковым фразам — CLI-обёртка (шаг 1f).

Порт ``legacy/catalog_build.py``: те же источники (GitHub Search по описанию и
README, npm search, реестр MCP), та же склейка дублей и фильтры (``core.catalog``),
но прогресс идёт в stderr колбэком, файлы — по флагам ``--out``/``--md``/``--json-out``.

Коды выхода: 0 — каталог собран (всегда, как в legacy).

Запуск:
  python -m cli.catalog_build "test impact analysis" --extra "flaky tests" "test selection"
  python -m cli.catalog_build "vector database" --min-stars 200 --pages 3 --out raw/cat_vectordb.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cli._common import build_argparser, load_runtime, print_table, setup_gh_or_warn, write_json
from core.catalog import DEFAULT_PAGES, SOURCE_ORDER, build_catalog, to_markdown
from core.github import GhClient
from core.registry import RegistryClient

__all__ = ["main"]

#: Файл каталога по умолчанию — legacy-путь относительно cwd.
DEFAULT_OUT = "raw/catalog_query.json"


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser(
        prog="catalog_build.py",
        description="Сборка каталога проектов по поисковым запросам",
    )
    parser.add_argument("queries", nargs="+", help="фразы для поиска по описанию")
    parser.add_argument("--extra", nargs="+", default=[], help="дополнительные фразы")
    parser.add_argument("--min-stars", type=int, default=0, help="фильтр звёзд для GitHub")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="страниц GitHub на фразу (100 шт/стр.)")
    parser.add_argument("--no-npm", action="store_true")
    parser.add_argument("--no-registry", action="store_true")
    parser.add_argument("--merge", metavar="FILE", help="добавить к уже собранному каталогу")
    parser.add_argument("--out", default=DEFAULT_OUT, help="куда сохранить каталог")
    parser.add_argument("--md", metavar="FILE", help="дополнительно записать каталог в markdown")
    args = parser.parse_args(argv)

    try:
        settings, http = load_runtime(args)
    except ValueError as error:
        print(f"[gapaudit] {error}", file=sys.stderr)
        return 2

    # GitHub обязателен (основной источник), npm/реестр можно выключить.
    setup_gh_or_warn()

    phrases = list(dict.fromkeys(args.queries + args.extra))
    sources = tuple(
        source
        for source in SOURCE_ORDER
        if not ((source == "npm" and args.no_npm) or (source == "registry" and args.no_registry))
    )
    merge_rows = _load_merge(args.merge)

    def on_progress(fraction: float, message: str) -> None:
        print(f"[gapaudit] {fraction:.2f} {message}", file=sys.stderr)

    result = build_catalog(
        phrases,
        gh_client=GhClient(),
        registry_client=RegistryClient(http, keywords=settings.keywords),
        mcp_registry_http=http,
        pages=args.pages,
        min_stars=args.min_stars,
        sources=sources,
        merge_rows=merge_rows,
        on_progress=on_progress,
    )
    items = result["items"]
    stats = result["stats"]

    write_json(args.out, {"queries": result["queries"], "stats": stats, "items": items})
    if not args.quiet:
        _print_report(stats, items)
    print(f"\nСОХРАНЕНО: {args.out} ({len(items)} проектов)")

    if args.md:
        target = Path(args.md)
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" — иначе на Windows в рабочем дереве появится CRLF (AGENTS.md, gotcha 17).
        target.write_text(to_markdown(items, ", ".join(phrases[:3])), encoding="utf-8", newline="\n")
        print(f"MARKDOWN: {args.md}")

    if args.json_out:
        # Сводка без items: её удобно сравнивать во времени и класть в БД шага 2.
        write_json(args.json_out, stats)

    print("Дальше: coverage.py по этому каталогу, check_predecessors.py по найденным именам.")
    if http.cache is not None:
        http.cache.save()
    return 0


def _load_merge(path: str | None) -> list[dict]:
    """Строки из ``--merge``: принимает и legacy-файл ``{"items": [...]}``, и голый список."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            old = json.load(handle)
        rows = old.get("items", old) if isinstance(old, dict) else old
        rows = list(rows or [])
    except Exception as error:
        print(f"не удалось прочитать {path}: {error}")
        return []
    print(f"сливаю с {path}: было {len(rows)}")
    return rows


def _print_report(stats: dict, items: list[dict]) -> None:
    """Сводка по каталогу — печатная форма legacy ``report()``."""
    print(f"\n=== КАТАЛОГ: {stats['total']} проектов ===")
    if stats.get("by_source"):
        print("источники: " + ", ".join(f"{key} {value}" for key, value in stats["by_source"].items()))
    multi = [row for row in items if len(row.get("sources") or []) > 1]
    if multi:
        print(f"найдены сразу в нескольких источниках: {len(multi)}")
    if stats.get("languages"):
        top_langs = list(stats["languages"].items())[:8]
        print("языки: " + ", ".join(f"{key} {value}" for key, value in top_langs))
    print(f"активность: push ≤ 90 дней — {stats['fresh_90d']}, за год — {stats['year_365d']}, "
          f"заброшенных (>365 дней) — {stats['abandoned_365d']}")
    top = stats.get("top_by_stars") or []
    if top:
        print("\nтоп по звёздам:")
        print_table(
            ["★", "проект", "язык", "push", "описание"],
            [
                [str(row["stars"]), str(row["id"])[:38], row["lang"] or "-",
                 row["pushed"] or "-", (row["desc"] or "")[:58]]
                for row in top
            ],
            aligns=["r", "l", "l", "l", "l"],
        )
    if stats.get("topics"):
        print("\nчастые теги: " + ", ".join(
            f"{key} ({value})" for key, value in list(stats["topics"].items())[:10]
        ))


if __name__ == "__main__":
    sys.exit(main())
