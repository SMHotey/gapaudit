"""Сбор данных для аудита экосистемы. Требует авторизованный gh CLI (gh auth status).

Правь CONFIG под свою экосистему и запускай: python collect.py
Выдаёт в OUT/: catalog.json, ecosystem_repos.json, popular.json
"""
import json, os, re, subprocess, time, urllib.request, urllib.parse

CONFIG = {
    # ключевое слово экосистемы (mcp, vscode-extension, ...)
    "keyword": "mcp",
    # каталог-реестр: пагинируемый JSON API (курсор в теле ответа). Нужен для плотности.
    "catalog": {
        "url": "https://registry.modelcontextprotocol.io/v0/servers?limit=100",
        "items_key": "servers",
        "cursor_path": ["metadata", "next_cursor"],
    },
    # поисковые запросы для репозиториев самой экосистемы
    "ecosystem_queries": [
        "topic:mcp-server", "topic:mcp", '"model context protocol" in:name,description',
    ],
    # источники популярности: темы GitHub (спрос в более широкой экосистеме)
    "topics": ["developer-tools", "devops", "cli", "database", "observability",
               "testing", "ci", "infrastructure-as-code", "container", "kubernetes",
               "api", "build-tools", "code-quality", "documentation", "terminal",
               "backend", "data-engineering", "security-tools", "git", "monitoring"],
    "min_stars": 800,
    "pages": 2,
    "out_dir": "raw",
}


def dig(d, path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def gh_search(q, pages, min_stars, out, tag):
    """Search API: 30 запросов/минуту — sleep обязателен."""
    for page in range(1, pages + 1):
        query = q if q.startswith("topic:") and "stars:" in q else q
        if not q.startswith("stars:") and "stars:" not in q:
            query = f"{q} stars:>{min_stars}"
        p = subprocess.run(["gh", "api", "-X", "GET", "search/repositories",
                            "-f", f"q={query}", "-f", "sort=stars", "-f", "order=desc",
                            "-f", "per_page=100", "-f", f"page={page}"],
                           capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            print("FAIL", q, p.stderr[:150])
            return
        items = json.loads(p.stdout).get("items", [])
        for r in items:
            fn = r["full_name"]
            if fn in out:
                out[fn].setdefault("found_by", []).append(tag)
                continue
            out[fn] = {"full_name": fn, "stars": r.get("stargazers_count", 0),
                       "desc": (r.get("description") or "")[:300],
                       "topics": r.get("topics", []), "lang": r.get("language"),
                       "pushed": r.get("pushed_at"), "created": r.get("created_at"),
                       "forks": r.get("forks_count", 0), "archived": r.get("archived"),
                       "url": r.get("html_url"), "found_by": [tag]}
        if len(items) < 100:
            return
        time.sleep(2.5)


def pull_catalog(cfg):
    """Пагинируем ДО КОНЦА курсора: частичная выборка искажает плотность."""
    items, cursor, pages = [], None, 0
    while pages < 400:
        url = cfg["url"] + (("&cursor=" + urllib.parse.quote(cursor)) if cursor else "")
        data = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(url, timeout=45) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(2)
        if not data:
            print("catalog: обрыв на странице", pages)
            break
        batch = data.get(cfg["items_key"]) or []
        items.extend(batch)
        pages += 1
        cursor = dig(data, cfg["cursor_path"])
        if not batch or not cursor:
            break
        if pages % 25 == 0:
            print("  catalog pages:", pages, "items:", len(items))
    return items


def main():
    out = CONFIG["out_dir"]
    os.makedirs(out, exist_ok=True)

    print("1/3 каталог...")
    cat = pull_catalog(CONFIG["catalog"])
    json.dump(cat, open(os.path.join(out, "catalog.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("   записей:", len(cat))

    print("2/3 репозитории экосистемы...")
    eco = {}
    for q in CONFIG["ecosystem_queries"]:
        gh_search(q, 2, 0, eco, q)
    json.dump(eco, open(os.path.join(out, "ecosystem_repos.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("   репозиториев:", len(eco))

    print("3/3 популярные репозитории по темам...")
    pop = {}
    for t in CONFIG["topics"]:
        gh_search(f"topic:{t}", CONFIG["pages"], CONFIG["min_stars"], pop, t)
        print(f"   {t}: всего {len(pop)}")
    json.dump(pop, open(os.path.join(out, "popular.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("готово. популярных:", len(pop))


if __name__ == "__main__":
    main()
