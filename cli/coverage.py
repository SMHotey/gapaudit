"""Сопоставление покрытия + фильтр не-инструментов + карта плотности.
Вход: raw/catalog.json, raw/ecosystem_repos.json, raw/popular.json (см. collect.py)
Выход: raw/gaps.json, raw/gap_tools.json, raw/saturation.json

ВАЖНО: матчинг по тексту — это прокси. Кандидатов ОБЯЗАТЕЛЬНО прогонять через verify.py.
"""
import json, os, re, collections

RAW = "raw"
L = lambda n: json.load(open(os.path.join(RAW, n), encoding="utf-8"))

# родовые имена: дают ложное «покрыто», выбрасываем
GENERIC = {"api", "core", "cli", "app", "web", "docs", "test", "ui", "sdk", "lib",
           "tool", "server", "client", "code", "data", "dev", "lab", "labs", "team",
           "cloud", "go", "now", "next", "node", "rust", "open", "src", "main", "base",
           "kit", "free", "pro", "nextjs"}

# классы задач для карты плотности — правь под экосистему
CAPS = {
    "флаки-тесты": ["flaky"], "выбор тестов по диффу": ["test impact", "test selection"],
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

BAD_NAME = re.compile(r"awesome|course|book|interview|tutorial|learn|guide|cheatsheet|papers|"
                      r"exercise|\bdays\b|\bnotes\b|roadmap|resources|from-scratch|beginners|"
                      r"dotfiles|-zh$|samples?$|curriculum|bootcamp|collection|examples?$", re.I)
BAD_DESC = re.compile(r"a (collective|curated|list|collection) of|curated list|awesome |list of |"
                      r"course|learn |learning |tutorial|curriculum|cheat ?sheet|\d+ (weeks|days)|"
                      r"study|roadmap|resources for|papers|textbook|for beginners|interview", re.I)
BAD_LANG = {"", "markdown", "jupyter notebook", "tex", "html", "css", "scss", "viml",
            "vim script", "emacs lisp", "restructuredtext"}
TOOLISH = re.compile(r"cli|command.line|server|api|self.hosted|automation|devops|infra|kubernetes|"
                     r"docker|database|monitoring|observability|deploy|proxy|gateway|cms|backend|"
                     r"search|browser|editor|testing|ci\b|tool|platform|engine|runtime|"
                     r"scheduler|messaging|queue|storage|network|security|terminal|shell|package", re.I)


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def build_corpus(cat, eco):
    parts = []
    for e in cat:
        s = e.get("server", e)
        parts.append(" ".join(str(s.get(k, "")) for k in ("name", "description", "title")))
    for r in eco.values():
        parts.append(r["full_name"] + " " + r["desc"] + " " + " ".join(r.get("topics", [])))
    return " " + norm(" ".join(parts)) + " "


def covered(word, blob):
    w = re.sub(r"[^a-z0-9]+", "", word.lower())
    if len(w) < 4 or w in GENERIC:
        return None
    return re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", blob) is not None


def is_tool(r):
    if BAD_NAME.search(r["full_name"]):
        return False
    if BAD_DESC.search(r["desc"] or ""):
        return False
    if (r["lang"] or "").lower() in BAD_LANG and not TOOLISH.search(r["desc"] or ""):
        return False
    if not TOOLISH.search((r["desc"] or "") + " " + " ".join(r.get("topics", []))):
        return False
    return True


def main():
    cat = L("catalog.json")
    eco = L("ecosystem_repos.json")
    pop = L("popular.json")
    blob = build_corpus(cat, eco)
    print("catalog:", len(cat), "ecosystem repos:", len(eco), "popular:", len(pop))

    gaps, cov = [], []
    for r in pop.values():
        hit = covered(r["full_name"].split("/")[1], blob)
        if hit is None:
            continue
        (cov if hit else gaps).append(r)
    gaps.sort(key=lambda r: -r["stars"])
    print(f"покрыто:{len(cov)}  возможно пусто:{len(gaps)}")

    tools = [r for r in gaps if is_tool(r)]
    print("после фильтра не-инструментов:", len(tools))
    json.dump({"gaps": gaps, "covered": cov}, open(os.path.join(RAW, "gaps.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(tools, open(os.path.join(RAW, "gap_tools.json"), "w", encoding="utf-8"), ensure_ascii=False)

    bodies = [norm(" ".join(str(e.get("server", e).get(k, "")) for k in ("name", "description", "title"))) for e in cat]
    dens = {}
    for k, kws in CAPS.items():
        dens[k] = sum(1 for t in bodies if any(kw in t for kw in kws))
    json.dump(dens, open(os.path.join(RAW, "saturation.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("\n=== ПЛОТНОСТЬ КЛАССОВ ЗАДАЧ (пустые — вверх) ===")
    for k, n in sorted(dens.items(), key=lambda x: x[1]):
        print(f"{n:>5}  {k}")
    print("\n=== ТОП КАНДИДАТОВ (проверить verify.py) ===")
    for r in tools[:40]:
        print(f'{r["stars"]:>7} {r["full_name"][:45]:<46} {r["desc"][:70]}')


if __name__ == "__main__":
    main()
