"""Точечная проверка кандидатов — CLI-обёртка (шаг 1f).

Порт ``legacy/verify.py``: те же вердикты (НЕТ / ЗАГЛУШКА / СЛАБЫЙ / ЖИВОЙ) и та же
механика (поиск по имени инструмента + репозитории организации вендора), но вызовы
идут через ``core.github`` (``GhClient``, ``relevant_items``, ``org_candidates``).

Пороги вердикта (``STUB_STARS``/``LIVE_STARS`` и дни) — константы метода, а не
настройки (DEVPLAN §14): они описывают методологию проверки, а не вкус пользователя.
Пауза ``time.sleep(2.5)`` legacy заменена троттлингом ``GhClient`` (Search — 30/мин).

Запуск:
  python -m cli.verify                # возьмёт raw/gap_tools.json (топ-25)
  python -m cli.verify owner/repo ... # или явный список
  python -m cli.verify --keyword helm traefik/traefik
"""

from __future__ import annotations

import argparse
import json
import sys

from cli._common import build_argparser, load_runtime, setup_gh_or_warn, write_json
from core.github import GhClient, age_days, org_candidates, relevant_items

__all__ = ["main", "verdict"]

#: Пороги вердикта — константы метода (DEVPLAN §14), не Settings.
STUB_STARS = 25  # < — заглушка
STUB_DAYS = 365  # push старше — заглушка
LIVE_STARS = 100  # >= вместе с LIVE_DAYS — живой
LIVE_DAYS = 180
TOP_K = 25  # сколько брать из gap_tools.json по умолчанию
SEARCH_PER_PAGE = 8

#: Файлы, как в legacy (относительно cwd).
GAP_TOOLS = "raw/gap_tools.json"
DEFAULT_JSON = "raw/verify.json"


def verdict(best: dict | None) -> str:
    """Вердикт по лучшей находке — порт legacy ``verdict()``."""
    if not best:
        return "НЕТ"
    stars, age = best["stargazers_count"], age_days(best.get("pushed_at"))
    if stars < STUB_STARS or age > STUB_DAYS:
        return "ЗАГЛУШКА"
    if stars >= LIVE_STARS and age <= LIVE_DAYS:
        return "ЖИВОЙ"
    return "СЛАБЫЙ"


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser(
        prog="verify.py",
        description="Точечная проверка кандидатов: НЕТ / МЁРТВАЯ ЗАГЛУШКА / СЛАБЫЙ / ЖИВОЙ",
    )
    parser.add_argument("targets", nargs="*", help="цели owner/repo; без них — топ из gap_tools.json")
    parser.add_argument("--top", type=int, default=TOP_K, help="сколько брать из gap_tools.json")
    args = parser.parse_args(argv)

    try:
        settings, http = load_runtime(args)
    except ValueError as error:
        print(f"[gapaudit] {error}", file=sys.stderr)
        return 2

    keyword = (args.keyword[0] if args.keyword else "mcp").lower()
    # Без gh verify не работает: предупреждаем, но не блокируем — пустая выдача тоже ответ.
    setup_gh_or_warn()

    if args.targets:
        targets = list(args.targets)
        base: dict[str, dict] = {}
    else:
        try:
            with open(GAP_TOOLS, encoding="utf-8") as handle:
                gap_tools = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"[gapaudit] не удалось прочитать {GAP_TOOLS}: {error}", file=sys.stderr)
            return 1
        gap_tools.sort(key=lambda row: -(row.get("stars") or 0))
        targets = [row["full_name"] for row in gap_tools[: args.top]]
        base = {row["full_name"]: row for row in gap_tools}

    gh = GhClient()
    res: dict[str, dict] = {}
    for target in targets:
        name = target.split("/")[1].lower()
        total, items = gh.search_repositories(f"{name} {keyword}", per_page=SEARCH_PER_PAGE)
        hits = relevant_items(items, name, keyword, exclude_full_name=target)
        # плюс репозитории организации вендора: сервер может называться по назначению
        org_hits = org_candidates(target.split("/")[0], keyword, gh.runner, exclude_full_name=target)
        for repo in org_hits:
            repo["full_name"] += "  «org»"
        merged = org_hits + hits
        merged.sort(
            key=lambda repo: (
                keyword in str(repo.get("full_name") or "").lower(),
                repo.get("stargazers_count") or 0,
            ),
            reverse=True,
        )
        best = merged[0] if merged else None
        found = verdict(best)
        res[target] = {
            "stars": base.get(target, {}).get("stars"),
            "desc": base.get(target, {}).get("desc", ""),
            "search_total": total,
            "verdict": found,
            "best": None if not best else {
                "full_name": best["full_name"],
                "stars": best["stargazers_count"],
                "pushed": best.get("pushed_at"),
                "age_days": age_days(best.get("pushed_at")),
            },
        }
        if best:
            print(f'{target[:40]:<41} {found:<10} -> {best["full_name"][:38]:<39} '
                  f'{best["stargazers_count"]:>5}★ {age_days(best.get("pushed_at")):>5}д')
        else:
            print(f'{target[:40]:<41} {found:<10} -> (найдено {total} результатов, '
                  f'релевантных {keyword} нет)')

    json_path = args.json_out or DEFAULT_JSON
    write_json(json_path, res)

    counts: dict[str, int] = {}
    for item in res.values():
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    print("\nитог:", counts)
    print("ЗАГЛУШКА и НЕТ — это пустые ниши; ЖИВОЙ — не лезь без причины.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
