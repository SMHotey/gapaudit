"""Сопоставление покрытия + фильтр не-инструментов + карта плотности — CLI (шаг 1f).

Порт ``legacy/coverage.py``. Вход: ``raw/{catalog,ecosystem_repos,popular}.json``
(их пишет ``collect.py``); выход: ``raw/{gaps,gap_tools,saturation}.json`` плюс
сводный ``--json-out``.

ВАЖНО: матчинг по тексту — это прокси. Кандидатов ОБЯЗАТЕЛЬНО прогонять через
``verify.py``.

Константы метода (``GENERIC``, ``CAPS``, ``BAD_NAME``, ``BAD_DESC``, ``BAD_LANG``,
``TOOLISH``) и чистые функции (``norm``/``build_corpus``/``covered``/``is_tool``)
живут здесь, а не в ``core``: это методология аудита, а не настройки пользователя
(DEVPLAN §14); в ``core`` переедут, если понадобятся приложению (шаг 5).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from cli._common import (
    build_argparser,
    emit_done,
    emit_error,
    job_json_mode,
    job_json_requested,
    load_runtime,
    print_table,
    write_json,
)
from core.settings import resolve_data_dir

__all__ = ["build_corpus", "covered", "is_tool", "main", "norm"]

#: Родовые имена: дают ложное «покрыто», выбрасываем (как в legacy).
GENERIC = {
    "api", "core", "cli", "app", "web", "docs", "test", "ui", "sdk", "lib",
    "tool", "server", "client", "code", "data", "dev", "lab", "labs", "team",
    "cloud", "go", "now", "next", "node", "rust", "open", "src", "main", "base",
    "kit", "free", "pro", "nextjs",
}

#: Классы задач для карты плотности — методология, правится под экосистему.
CAPS: dict[str, list[str]] = {
    "флаки-тесты": ["flaky"],
    "выбор тестов по диффу": ["test impact", "test selection"],
    "план запроса/индексы": ["query plan", "explain analyze", "slow query", "index advisor"],
    "безопасность миграций": ["schema migration", "migration plan", "migration rollback"],
    "профайлер": ["flamegraph", "profiler", "cpu profile", "pprof", "py-spy"],
    "heap/утечки": ["heap dump", "memory leak", "heap snapshot"],
    "монорепа/граф задач": ["monorepo", "task graph", "turborepo", "bazel", "nx "],
    "мёртвый код/codemod": ["dead code", "codemod", "unused code"],
    "влияние зависимостей": ["impact analysis", "dependency graph", "blast radius"],
    "стектрейс->причина": ["stack trace", "error grouping", "traceback"],
    "SBOM/supply chain": ["sbom", "supply chain", "cyclonedx", "slsa"],
    "секреты": ["secret scanning", "gitleaks", "trufflehog"],
    "стоимость облака": ["cost optimization", "finops", "cloud cost"],
    "IaC drift": ["terraform", "pulumi", "drift detection", "iac"],
    "инциденты/постмортемы": ["incident", "postmortem", "on-call", "alert triage"],
    "breaking changes API": ["breaking change", "openapi diff", "schema diff"],
    "e2e/браузерные тесты": ["playwright", "cypress", "selenium", "e2e test"],
    "доступность": ["accessibility", "wcag", "a11y"],
    "i18n": ["i18n", "localization", "translation"],
    "контекстный бюджет": ["token budget", "context window", "context compression"],
    "память агента": ["memory", "vector store", "embeddings", "rag"],
    "код-ревью": ["code review", "pull request review"],
    "kubernetes": ["kubernetes", "k8s", "helm", "cluster"],
    "SQL-аналитика": ["sql", "warehouse", "analytics"],
}

BAD_NAME = re.compile(
    r"awesome|course|book|interview|tutorial|learn|guide|cheatsheet|papers|"
    r"exercise|\bdays\b|\bnotes\b|roadmap|resources|from-scratch|beginners|"
    r"dotfiles|-zh$|samples?$|curriculum|bootcamp|collection|examples?$",
    re.I,
)
BAD_DESC = re.compile(
    r"a (collective|curated|list|collection) of|curated list|awesome |list of |"
    r"course|learn |learning |tutorial|curriculum|cheat ?sheet|\d+ (weeks|days)|"
    r"study|roadmap|resources for|papers|textbook|for beginners|interview",
    re.I,
)
BAD_LANG = {
    "", "markdown", "jupyter notebook", "tex", "html", "css", "scss", "viml",
    "vim script", "emacs lisp", "restructuredtext",
}
TOOLISH = re.compile(
    r"cli|command.line|server|api|self.hosted|automation|devops|infra|kubernetes|"
    r"docker|database|monitoring|observability|deploy|proxy|gateway|cms|backend|"
    r"search|browser|editor|testing|ci\b|tool|platform|engine|runtime|"
    r"scheduler|messaging|queue|storage|network|security|terminal|shell|package",
    re.I,
)

#: Входные файлы — как в legacy (относительно --raw-dir).
CATALOG_FILE = "catalog.json"
ECOSYSTEM_FILE = "ecosystem_repos.json"
POPULAR_FILE = "popular.json"
GAPS_FILE = "gaps.json"
GAP_TOOLS_FILE = "gap_tools.json"
SATURATION_FILE = "saturation.json"


def norm(text: str) -> str:
    """Нормализация текста для матчинга: только строчные латинские слова."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def build_corpus(cat: list, eco: dict) -> str:
    """Один текстовый корпус из каталога и репозиториев экосистемы."""
    parts = []
    for entry in cat:
        server = entry.get("server", entry) if isinstance(entry, dict) else {}
        parts.append(" ".join(str(server.get(key, "")) for key in ("name", "description", "title")))
    for repo in eco.values():
        parts.append(repo["full_name"] + " " + (repo.get("desc") or "") + " " + " ".join(repo.get("topics", [])))
    return " " + norm(" ".join(parts)) + " "


def covered(word: str, blob: str) -> bool | None:
    """Есть ли слово в корпусе; ``None`` — слово слишком родовое, чтобы судить."""
    cleaned = re.sub(r"[^a-z0-9]+", "", word.lower())
    if len(cleaned) < 4 or cleaned in GENERIC:
        return None
    return re.search(r"(?<![a-z0-9])" + re.escape(cleaned) + r"(?![a-z0-9])", blob) is not None


def is_tool(repo: dict) -> bool:
    """Репозиторий — инструмент, а не курс/подборка/учебные материалы."""
    if BAD_NAME.search(repo["full_name"]):
        return False
    if BAD_DESC.search(repo.get("desc") or ""):
        return False
    if (repo.get("lang") or "").lower() in BAD_LANG and not TOOLISH.search(repo.get("desc") or ""):
        return False
    if not TOOLISH.search((repo.get("desc") or "") + " " + " ".join(repo.get("topics", []))):
        return False
    return True


def _main(argv: list[str] | None = None) -> int:
    parser = build_argparser(
        prog="coverage.py",
        description="Сопоставление покрытия, фильтр не-инструментов, карта плотности",
    )
    parser.add_argument("--raw-dir", metavar="DIR",
                        help="каталог с входными JSON (по умолчанию <data_dir>/raw)")
    parser.add_argument("--top", type=int, default=40, help="сколько кандидатов печатать")
    args = parser.parse_args(argv)

    try:
        settings, http = load_runtime(args)
        data_dir = resolve_data_dir(settings)
    except ValueError as error:
        print(f"[gapaudit] {error}", file=sys.stderr)
        return 2

    raw_dir = args.raw_dir or str(Path(data_dir) / "raw")
    cat, cat_ok = _load(os.path.join(raw_dir, CATALOG_FILE))
    eco, eco_ok = _load(os.path.join(raw_dir, ECOSYSTEM_FILE))
    pop, pop_ok = _load(os.path.join(raw_dir, POPULAR_FILE))
    if not (cat_ok or eco_ok or pop_ok):
        print(f"[gapaudit] нет входных файлов в {raw_dir}: нужен collect.py", file=sys.stderr)
        return 1

    cat_list = list(cat) if isinstance(cat, (list, dict)) else []
    eco_dict = dict(eco) if isinstance(eco, dict) else {}
    pop_dict = dict(pop) if isinstance(pop, dict) else {}

    blob = build_corpus(cat_list, eco_dict)
    print(f"catalog: {len(cat_list)} ecosystem repos: {len(eco_dict)} popular: {len(pop_dict)}")

    gaps: list[dict] = []
    covered_rows: list[dict] = []
    for repo in pop_dict.values():
        full_name = str(repo.get("full_name") or "")
        tail = full_name.split("/")[1] if "/" in full_name else full_name
        hit = covered(tail, blob)
        if hit is None:
            continue
        (covered_rows if hit else gaps).append(repo)
    gaps.sort(key=lambda repo: -(repo.get("stars") or 0))
    print(f"покрыто:{len(covered_rows)}  возможно пусто:{len(gaps)}")

    tools = [repo for repo in gaps if is_tool(repo)]
    print(f"после фильтра не-инструментов: {len(tools)}")

    write_json(os.path.join(raw_dir, GAPS_FILE), {"gaps": gaps, "covered": covered_rows})
    write_json(os.path.join(raw_dir, GAP_TOOLS_FILE), tools)

    bodies = [
        norm(" ".join(
            str(entry.get("server", entry).get(key, "")) for key in ("name", "description", "title")
        ))
        for entry in cat_list
    ]
    density = {
        name: sum(1 for body in bodies if any(word in body for word in words))
        for name, words in CAPS.items()
    }
    write_json(os.path.join(raw_dir, SATURATION_FILE), density)

    print("\n=== ПЛОТНОСТЬ КЛАССОВ ЗАДАЧ (пустые — вверх) ===")
    for name, count in sorted(density.items(), key=lambda item: item[1]):
        print(f"{count:>5}  {name}")

    print("\n=== ТОП КАНДИДАТОВ (проверить verify.py) ===")
    print_table(
        ["★", "репозиторий", "описание"],
        [
            [str(repo["stars"]), str(repo["full_name"])[:45], (repo.get("desc") or "")[:70]]
            for repo in tools[: args.top]
        ],
        aligns=["r", "l", "l"],
    )

    if args.json_out:
        write_json(
            args.json_out,
            {
                "gaps_n": len(gaps),
                "covered_n": len(covered_rows),
                "tools_n": len(tools),
                "density": density,
            },
        )
    if http.cache is not None:
        http.cache.save()
    return 0


def _load(path: str) -> tuple[Any, bool]:
    """Прочитать входной JSON; отсутствие/мусор — (пустое значение, False)."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle), True
    except (OSError, ValueError):
        print(f"[gapaudit] нет файла: {path} (принимаю как пустой)", file=sys.stderr)
        return {}, False


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; под ``--job-json`` — протокол JSON-lines вокруг тела.

    Таблицы при этом уходят в stderr (лог задачи), stdout остаётся чистым
    протоколом; ошибка тела — ``{"type": "error"}`` и код 2 (шаг 3d-a).
    """
    if not job_json_requested(argv):
        return _main(argv)
    with job_json_mode():
        try:
            code = _main(argv)
        except Exception as error:  # noqa: BLE001 - задача обязана отчитаться ошибкой
            emit_error(str(error))
            return 2
        if code == 0:
            emit_done()
        return code


if __name__ == "__main__":
    sys.exit(main())
