"""Сбор данных для аудита экосистемы — CLI-обёртка (шаг 1f).

Порт ``legacy/collect.py``: CONFIG-словарь становится CLI-флагами
(``--keyword``, ``--catalog-url``, ``--ecosystem-query``, ``--topic`` и др.),
три фазы те же:

  1. каталог реестра MCP — до конца курсора (частичная выборка искажает плотность);
  2. репозитории экосистемы — GitHub Search по запросам с ключевым словом;
  3. популярные репозитории по темам GitHub.

Фазы 2-3 требуют авторизованный ``gh``: без него печатается предупреждение,
файлы пишутся пустыми, код выхода 0 (как решено в шаге 1f).

Запуск:
  python -m cli.collect --keyword mcp
  python -m cli.collect --keyword helm --topic kubernetes --min-stars 500
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from cli._common import build_argparser, load_runtime, setup_gh_or_warn, write_json
from core.github import GhClient
from core.mcp_registry import DEFAULT_CURSOR_PATH, DEFAULT_LIMIT, MCP_LIST_URL
from core.settings import resolve_data_dir

__all__ = ["main"]

#: Дефолты legacy-CONFIG: запросы экосистемы и темы популярности.
DEFAULT_ECOSYSTEM_QUERIES = (
    "topic:mcp-server",
    "topic:mcp",
    '"model context protocol" in:name,description',
)
DEFAULT_TOPICS = (
    "developer-tools", "devops", "cli", "database", "observability",
    "testing", "ci", "infrastructure-as-code", "container", "kubernetes",
    "api", "build-tools", "code-quality", "documentation", "terminal",
    "backend", "data-engineering", "security-tools", "git", "monitoring",
)

CATALOG_FILENAME = "catalog.json"
ECOSYSTEM_FILENAME = "ecosystem_repos.json"
POPULAR_FILENAME = "popular.json"

#: Столько страниц каталога максимум — предохранитель, как в legacy.
CATALOG_MAX_PAGES = 400


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser(
        prog="collect.py",
        description="Сбор данных для аудита экосистемы: каталог реестра + экосистема + популярные",
    )
    parser.add_argument("--catalog-url", default=MCP_LIST_URL.format(limit=DEFAULT_LIMIT),
                        help="пагинируемый JSON API каталога (курсор в теле ответа)")
    parser.add_argument("--catalog-items-key", default="servers", metavar="K",
                        help="ключ списка элементов в ответе каталога")
    parser.add_argument("--catalog-cursor-path", nargs="+", default=list(DEFAULT_CURSOR_PATH),
                        metavar="KEY", help="путь до курсора в ответе (по умолчанию metadata nextCursor)")
    parser.add_argument("--ecosystem-query", action="append", default=None, metavar="Q",
                        help="поисковый запрос экосистемы; можно повторять (дефолт — legacy-тройка)")
    parser.add_argument("--topic", action="append", default=None, metavar="T",
                        help="тема GitHub для популярных; можно повторять (дефолт — legacy-двадцатка)")
    parser.add_argument("--min-stars", type=int, default=800, help="минимум звёзд для популярных")
    parser.add_argument("--pages", type=int, default=2, help="страниц GitHub на запрос")
    parser.add_argument("--out-dir", metavar="DIR",
                        help="куда писать trio JSON (по умолчанию <data_dir>/raw)")
    args = parser.parse_args(argv)

    if not args.keyword:
        print("[gapaudit] --keyword обязателен: ключевое слово экосистемы (mcp, vscode-extension, ...)",
              file=sys.stderr)
        return 2

    try:
        settings, http = load_runtime(args)
        data_dir = resolve_data_dir(settings)
    except ValueError as error:
        print(f"[gapaudit] {error}", file=sys.stderr)
        return 2

    out_dir = args.out_dir or str(Path(data_dir) / "raw")
    os.makedirs(out_dir, exist_ok=True)
    ecosystem_queries = tuple(args.ecosystem_query) if args.ecosystem_query else DEFAULT_ECOSYSTEM_QUERIES
    topics = tuple(args.topic) if args.topic else DEFAULT_TOPICS

    print("1/3 каталог...")
    catalog = _pull_catalog(
        http,
        url=args.catalog_url,
        items_key=args.catalog_items_key,
        cursor_path=tuple(args.catalog_cursor_path),
    )
    write_json(os.path.join(out_dir, CATALOG_FILENAME), catalog)
    print(f"   записей: {len(catalog)}")

    gh_ok, _message = setup_gh_or_warn()
    ecosystem: dict[str, dict] = {}
    popular: dict[str, dict] = {}
    if gh_ok:
        gh = GhClient()
        print("2/3 репозитории экосистемы...")
        for query in ecosystem_queries:
            _gh_merge(gh, query, 2, 0, ecosystem, query)
        print(f"   репозиториев: {len(ecosystem)}")

        print("3/3 популярные репозитории по темам...")
        for topic in topics:
            _gh_merge(gh, f"topic:{topic}", args.pages, args.min_stars, popular, topic)
            print(f"   {topic}: всего {len(popular)}")
        print(f"готово. популярных: {len(popular)}")
    else:
        # Предупреждение уже напечатал setup_gh_or_warn(); фазы пропускаем.
        print("2/3 и 3/3 пропущены: нужен авторизованный gh", file=sys.stderr)

    write_json(os.path.join(out_dir, ECOSYSTEM_FILENAME), ecosystem)
    write_json(os.path.join(out_dir, POPULAR_FILENAME), popular)

    summary = {"catalog_n": len(catalog), "ecosystem_n": len(ecosystem), "popular_n": len(popular)}
    if args.json_out:
        write_json(args.json_out, summary)
    print(f"СОХРАНЕНО: {out_dir}")
    if http.cache is not None:
        http.cache.save()
    return 0


def dig(data: Any, path: tuple[str, ...] | list[str]) -> Any:
    """Значение по пути ключей — порт legacy ``dig()``."""
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _pull_catalog(http: Any, *, url: str, items_key: str, cursor_path: tuple[str, ...]) -> list[dict]:
    """Пагинировать ДО КОНЦА курсора: частичная выборка искажает плотность.

    Сетевые повторы держит ``HttpClient``; сбой или статус != 200 — обрыв с
    предупреждением (часть данных лучше данных-нет: legacy печатал то же).
    """
    items: list[dict] = []
    cursor: str | None = None
    pages = 0
    while pages < CATALOG_MAX_PAGES:
        page_url = url + (("&cursor=" + urllib.parse.quote(cursor)) if cursor else "")
        status, data = http.get(page_url, timeout=45)
        if status != 200 or not isinstance(data, dict):
            print(f"catalog: обрыв на странице {pages}")
            break
        batch = data.get(items_key) or []
        items.extend(batch)
        pages += 1
        cursor = dig(data, cursor_path)
        if pages % 25 == 0:
            print(f"  catalog pages: {pages} items: {len(items)}")
        if not batch or not cursor:
            break
    return items


def _gh_merge(
    gh: GhClient,
    query: str,
    pages: int,
    min_stars: int,
    out: dict[str, dict],
    tag: str,
) -> None:
    """GitHub Search, вливается в общий словарь — порт legacy ``gh_search()`` collect.

    ``found_by`` помечает, каким запросом репозиторий найден: так один проект,
    найденный и по теме, и по запросу экосистемы, не считается дважды.
    """
    for page in range(1, pages + 1):
        prepared = query if "stars:" in query else f"{query} stars:>{min_stars}"
        total, items = gh.search_repositories(prepared, per_page=100, page=page)
        if total is None:
            print("FAIL", query)
            return
        for repo in items:
            full_name = repo.get("full_name")
            if not full_name:
                continue
            if full_name in out:
                out[full_name].setdefault("found_by", []).append(tag)
                continue
            out[full_name] = {
                "full_name": full_name,
                "stars": repo.get("stargazers_count", 0),
                "desc": str(repo.get("description") or "")[:300],
                "topics": repo.get("topics", []),
                "lang": repo.get("language"),
                "pushed": repo.get("pushed_at"),
                "created": repo.get("created_at"),
                "forks": repo.get("forks_count", 0),
                "archived": repo.get("archived"),
                "url": repo.get("html_url"),
                "found_by": [tag],
            }
        if len(items) < 100:
            return


if __name__ == "__main__":
    sys.exit(main())
