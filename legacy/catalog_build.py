"""Сборка каталога проектов ИЗ ПОИСКОВЫХ ЗАПРОСОВ (по описанию, а не по имени).

Зачем: каталог по незнакомой области нельзя выкачать — его надо собрать. Ищешь фразу,
сводишь выдачу из нескольких источников в один файл, снимаешь дубли и получаешь каталог,
пригодный для coverage.py и catalog_scan.py.

Источники:
  github   — `search/repositories` с `in:description,readme` (есть звёзды, язык, дата push)
  npm      — `registry.npmjs.org/-/v1/search` (ищет по имени, описанию, keywords)
  registry — официальный реестр MCP, параметр `search` (матчит узко — проверено)
  PyPI     — публичного поиска по описанию НЕТ: покрывается через github и npm

Запуск:
  python catalog_build.py "test impact analysis" --extra "flaky tests" "test selection"
  python catalog_build.py "vector database" --min-stars 200 --pages 3 --out raw/cat_vectordb.json
"""
import argparse, json, os, re, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from collections import Counter

UA = {"User-Agent": "ecosystem-gap-audit"}
GH_SLEEP = 2.5          # Search API: 30 запросов/минуту
GH_PER_PAGE = 100
NPM_SIZE = 250
REG_LIMIT = 100


def get(url, timeout=30, tries=3):
    delay = 2
    for a in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
            return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None
            if e.code in (429, 500, 502, 503) and a < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            return None
        except Exception:
            if a < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            return None
    return None


def gh_search(phrase, pages=2, min_stars=0):
    """GitHub: по описанию и README. Звёзды, язык, дата последнего push."""
    out = []
    q = f'"{phrase}" in:description,readme' + (f' stars:>={min_stars}' if min_stars else "")
    for page in range(1, pages + 1):
        p = subprocess.run(["gh", "api", "-X", "GET", "search/repositories",
                            "-f", f"q={q}", "-f", "sort=stars", "-f", "order=desc",
                            "-f", f"per_page={GH_PER_PAGE}", "-f", f"page={page}"],
                           capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            print(f"    github «{phrase}» стр.{page}: {p.stderr.strip()[:90]}")
            break
        try:
            d = json.loads(p.stdout)
        except Exception:
            break
        items = d.get("items") or []
        for i in items:
            out.append({"source": "github", "id": i.get("full_name"),
                        "url": i.get("html_url"), "desc": (i.get("description") or "")[:200],
                        "stars": i.get("stargazers_count"), "lang": i.get("language"),
                        "pushed": (i.get("pushed_at") or "")[:10],
                        "topics": i.get("topics") or [], "query": phrase})
        if len(items) < GH_PER_PAGE:
            break
        time.sleep(GH_SLEEP)
    return out


def npm_search(phrase):
    """npm ищет по имени, описанию и keywords — то, что нужно для каталога по фразе."""
    d = get("https://registry.npmjs.org/-/v1/search?size=%d&text=%s"
            % (NPM_SIZE, urllib.parse.quote(phrase)))
    out = []
    for o in (d or {}).get("objects") or []:
        p = o.get("package") or {}
        out.append({"source": "npm", "id": p.get("name"),
                    "url": ((p.get("links") or {}).get("npm") or ""),
                    "desc": (p.get("description") or "")[:200], "stars": None,
                    "lang": "js", "pushed": (p.get("date") or "")[:10],
                    "topics": p.get("keywords") or [], "query": phrase})
    return out


def registry_search(phrase):
    """Официальный реестр MCP: матчит узко, но как дополнительный источник годится."""
    d = get("https://registry.modelcontextprotocol.io/v0/servers?search=%s&limit=%d"
            % (urllib.parse.quote(phrase), REG_LIMIT))
    out = []
    for it in (d or {}).get("servers") or []:
        s = it.get("server") or {}
        out.append({"source": "registry", "id": s.get("name"), "url": "",
                    "desc": (s.get("description") or "")[:200], "stars": None,
                    "lang": None, "pushed": "", "topics": [], "query": phrase})
    return out


def short_name(name):
    """Нормализованное имя для снятия дублей МЕЖДУ источниками.

    `owner/Проект`, `project`, `project-py`, `@scope/project` — всё это одно и то же.
    """
    n = (name or "").lower().split("/")[-1]
    for suf in ("-py", "_py", ".py", "-js", "-ts", "-mcp", "-server", "-cli", "-core"):
        if n.endswith(suf):
            n = n[: -len(suf)]
    return re.sub(r"[^a-z0-9]", "", n)


def dedupe(rows):
    """Снимает дубли внутри источника и склеивает одно и то же между источниками.

    Склейка важна: один проект живёт и на GitHub, и в npm, и его не должно быть трижды
    в каталоге — но известно про него должно быть всё сразу (звёзды + пакет + теги).
    """
    merged = {}
    for r in rows:
        key = short_name(r["id"])
        if not key:
            continue
        cur = merged.get(key)
        if not cur:
            merged[key] = dict(r, sources=[r["source"]])
            continue
        cur["sources"].append(r["source"])
        if (r.get("stars") or 0) > (cur.get("stars") or 0):
            cur.update({"stars": r["stars"], "id": r["id"],
                        "url": r.get("url") or cur.get("url"),
                        "lang": r.get("lang") or cur.get("lang")})
        if r.get("pushed") and r["pushed"] > (cur.get("pushed") or ""):
            cur["pushed"] = r["pushed"]
        if len(r.get("desc") or "") > len(cur.get("desc") or ""):
            cur["desc"] = r["desc"]
        cur["queries"] = sorted(set(cur.get("queries") or [cur.get("query", "")])
                                | {r["query"]})
    return list(merged.values())


def activity_days(pushed):
    """Сколько дней назад был push. Пустое — None."""
    if not pushed:
        return None
    try:
        return int((time.time() - time.mktime(time.strptime(pushed[:10], "%Y-%m-%d"))) / 86400)
    except Exception:
        return None


def report(rows, top=15):
    """Сводка по собранному каталогу: из чего собран, чем жив, что внутри."""
    print(f"\n=== КАТАЛОГ: {len(rows)} проектов ===")
    src = Counter(s for r in rows for s in r.get("sources", [r["source"]]))
    print("источники: " + ", ".join(f"{k} {v}" for k, v in src.most_common()))
    multi = [r for r in rows if len(r.get("sources", [])) > 1]
    if multi:
        print(f"найдены сразу в нескольких источниках: {len(multi)}")
    langs = Counter(r["lang"] for r in rows if r.get("lang"))
    if langs:
        print("языки: " + ", ".join(f"{k} {v}" for k, v in langs.most_common(8)))
    fresh = sum(1 for r in rows if (activity_days(r.get("pushed")) or 9999) <= 90)
    year = sum(1 for r in rows if (activity_days(r.get("pushed")) or 9999) <= 365)
    print(f"активность: push ≤ 90 дней — {fresh}, за год — {year}, "
          f"заброшенных (>365 дней) — {len(rows) - year}")
    withstars = sorted((r for r in rows if r.get("stars")), key=lambda r: -r["stars"])
    if withstars:
        print("\nтоп по звёздам:")
        for r in withstars[:top]:
            d = activity_days(r.get("pushed"))
            print(f"  {r['stars']:>6}★ {r['id'][:38]:<39} {(r.get('lang') or '-'):<12}"
                  f" {(str(d) + 'д') if d is not None else '-':>7}  {(r.get('desc') or '')[:58]}")
    topics = Counter(t for r in rows for t in (r.get("topics") or []))
    if topics:
        print("\nчастые теги: " + ", ".join(f"{k} ({v})" for k, v in topics.most_common(10)))
    return {"total": len(rows), "by_source": dict(src), "languages": dict(langs)}
def to_markdown(rows, title):
    """Каталог в читаемом виде: виден и человеку, и агенту без питона."""
    rows = sorted(rows, key=lambda r: -(r.get("stars") or 0))
    out = [f"# Каталог: {title}", "", f"Всего проектов: {len(rows)}", "",
           "| Звёзды | Проект | Язык | Push | Источники | Описание |",
           "|---:|---|---|---|---|---|"]
    for r in rows:
        d = activity_days(r.get("pushed"))
        out.append("| {} | {} | {} | {} | {} | {} |".format(
            r.get("stars") if r.get("stars") else "-", r.get("id") or "-",
            r.get("lang") or "-", f"{d}д" if d is not None else "-",
            ",".join(r.get("sources", [])), (r.get("desc") or "").replace("|", "/")[:120]))
    return "\n".join(out) + "\n"


NOISE_WORDS = ("awesome", "курс", "course", "cheatsheet", "шпаргалка", "tutorial",
               "boilerplate", "example", "interview", "roadmap", "resources",
               "list of", "collection of", "подборка", "books", "papers")


def words_of(phrase):
    """Значимые слова фразы — для проверки релевантности выдачи."""
    return [w for w in re.findall(r"[a-z0-9]{4,}", phrase.lower())]


def relevant(r, words):
    """Проект релевантен, если в имени или описании есть значимое слово фразы.

    Без этой проверки поиск по описанию тянет всё подряд: в прогоне по фразе
    «test impact analysis» выдача начиналась с awesome-списков на 520 тыс. звёзд.
    """
    if not words:
        return True
    hay = ((r.get("id") or "") + " " + (r.get("desc") or "")).lower()
    return any(w in hay for w in words)


def is_noise(r):
    """Отсекает подборки, курсы и учебные репозитории — они не проекты, а списки."""
    hay = ((r.get("id") or "") + " " + (r.get("desc") or "")).lower()
    return any(n in hay for n in NOISE_WORDS)


def main():
    ap = argparse.ArgumentParser(description="Сборка каталога проектов по поисковым запросам")
    ap.add_argument("queries", nargs="+", help="фразы для поиска по описанию")
    ap.add_argument("--extra", nargs="+", default=[], help="дополнительные фразы")
    ap.add_argument("--min-stars", type=int, default=0, help="фильтр звёзд для GitHub")
    ap.add_argument("--pages", type=int, default=2, help="страниц GitHub на фразу (100 шт/стр.)")
    ap.add_argument("--no-npm", action="store_true")
    ap.add_argument("--no-registry", action="store_true")
    ap.add_argument("--merge", metavar="FILE", help="добавить к уже собранному каталогу")
    ap.add_argument("--out", default="raw/catalog_query.json", help="куда сохранить каталог")
    ap.add_argument("--md", metavar="FILE", help="дополнительно записать каталог в markdown")
    a = ap.parse_args()

    phrases = list(dict.fromkeys(a.queries + a.extra))
    rows = []
    for q in phrases:
        print(f"фраза «{q}»")
        g = gh_search(q, pages=a.pages, min_stars=a.min_stars)
        print(f"  github: {len(g)}")
        rows += g
        if not a.no_npm:
            n = npm_search(q)
            print(f"  npm:    {len(n)}")
            rows += n
        if not a.no_registry:
            r = registry_search(q)
            print(f"  реестр: {len(r)}")
            rows += r
        time.sleep(GH_SLEEP)

    if a.merge and os.path.exists(a.merge):
        try:
            old = json.load(open(a.merge, encoding="utf-8"))
            old = old.get("items", old) if isinstance(old, dict) else old
            print(f"сливаю с {a.merge}: было {len(old)}")
            rows += old
        except Exception as e:
            print(f"не удалось прочитать {a.merge}: {e}")

    rows = dedupe(rows)
    words = [w for q in phrases for w in words_of(q)]
    before = len(rows)
    rows = [r for r in rows if not is_noise(r) and relevant(r, words)]
    print(f"\nфильтр шума: {before} → {len(rows)} (убрано {before - len(rows)})")
    stats = report(rows)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump({"queries": phrases, "stats": stats, "items": rows},
              open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nСОХРАНЕНО: {a.out} ({len(rows)} проектов)")
    if a.md:
        open(a.md, "w", encoding="utf-8").write(to_markdown(rows, ", ".join(phrases[:3])))
        print(f"MARKDOWN: {a.md}")
    print("Дальше: coverage.py по этому каталогу, check_predecessors.py по найденным именам.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
