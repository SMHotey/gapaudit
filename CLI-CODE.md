# Полный код CLI (копия скриптов скилла ecosystem-gap-audit)

Собрано: 2026-10-18 22:49 · файлов: 6 · строк: 1228 · размер: 57.3 КБ

Документ сгенерирован (`tools/dump_code.py`) — правки вносить в исходные файлы, а не сюда. Контрольная сумма (sha256, 12 знаков) дана, чтобы можно было сверить версии при следующей сборке.

## Сводка

| Файл | Строк | Байт | sha256 | Назначение |
|---|---:|---:|---|---|
| `catalog_build.py` | 286 | 14072 | `e98061593acc` | Сборка каталога проектов ИЗ ПОИСКОВЫХ ЗАПРОСОВ (по описанию, а не по имени). |
| `catalog_scan.py` | 126 | 6892 | `dda476f4c55e` | ШАБЛОН потокового обхода каталога одним проходом: размер, уникальные имена, плотность |
| `check_predecessors.py` | 405 | 18382 | `05eadcb96d13` | Проверка кандидатов и предшественников: npm, PyPI, adoption, MCP-поверхность. |
| `collect.py` | 124 | 5311 | `170fa815fff7` | Сбор данных для аудита экосистемы. Требует авторизованный gh CLI (gh auth status). |
| `coverage.py` | 128 | 6827 | `72e1f9177d0a` | Сопоставление покрытия + фильтр не-инструментов + карта плотности. |
| `verify.py` | 159 | 7190 | `dc8ddb238ff6` | Точечная проверка кандидатов: НЕТ / МЁРТВАЯ ЗАГЛУШКА / СЛАБЫЙ / ЖИВОЙ. |

## Оглавление

1. `catalog_build.py` — Сборка каталога проектов ИЗ ПОИСКОВЫХ ЗАПРОСОВ (по описанию, а не по имени). (286 строк)
2. `catalog_scan.py` — ШАБЛОН потокового обхода каталога одним проходом: размер, уникальные имена, плотность (126 строк)
3. `check_predecessors.py` — Проверка кандидатов и предшественников: npm, PyPI, adoption, MCP-поверхность. (405 строк)
4. `collect.py` — Сбор данных для аудита экосистемы. Требует авторизованный gh CLI (gh auth status). (124 строк)
5. `coverage.py` — Сопоставление покрытия + фильтр не-инструментов + карта плотности. (128 строк)
6. `verify.py` — Точечная проверка кандидатов: НЕТ / МЁРТВАЯ ЗАГЛУШКА / СЛАБЫЙ / ЖИВОЙ. (159 строк)

## `catalog_build.py`

286 строк · 14072 байт · sha256 `e98061593acc`

```python
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
```

## `catalog_scan.py`

126 строк · 6892 байт · sha256 `dda476f4c55e`

```python
"""ШАБЛОН потокового обхода каталога одним проходом: размер, уникальные имена, плотность
классов задач по описаниям, тип поставки, концентрация неймспейсов.

Правь под свою экосистему: BASE (URL каталога с пагинацией), CLASSES (список
подстрока -> ярлык), ключ курсора в ответе и место сохранения результата.
Требует сети. Запускать с `python -u` и перенаправлять вывод в лог: обход длится десятки минут,
прогресс печатается каждые 25 страниц, и без `-u` буферизация съедает промежуточные строки.

Зачем одним проходом: замер размера отдельно от плотности дважды гонять десятки минут
бессмысленно, а `search`-параметр реестра для плотности не годится (возвращает 0-1 почти
на всё — проверено). Продолжает с курсора чекпоинта, пишет прогресс каждые 25 страниц.
"""
import json, os, re, time, urllib.request, urllib.error, urllib.parse

WORK = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(WORK, "raw")
os.makedirs(RAW, exist_ok=True)
BASE = "https://registry.modelcontextprotocol.io/v0/servers?limit=100"

# классы задач: подстрока -> человекочитаемый ярлык (порядок как в плане)
CLASSES = [
    ("kubernetes", "kubernetes"), ("code search", "поиск/индексация кода"),
    ("sql", "SQL/аналитика"), ("memory", "память агента"),
    ("payment", "платежи/финансы"), ("prompt", "промпты/скиллы"),
    ("eval", "eval агентов"), ("pdf", "PDF/office"), ("schema", "схемы БД"),
    ("observability", "observability"), ("trace", "трассировка"),
    ("sbom", "SBOM/supply chain"), ("accessibility", "доступность"),
    ("i18n", "i18n"), ("flaky", "флаки-тесты"), ("incident", "инциденты"),
    ("postmortem", "постмортемы"), ("terraform", "Terraform/IaC"),
    ("cloud cost", "стоимость облака"), ("finops", "FinOps"),
    ("monorepo", "монорепа"), ("dead code", "мёртвый код"),
    ("codemod", "codemod"), ("flamegraph", "фламеграфы"),
    ("profiler", "профилировщик"), ("heap", "heap/утечки"),
    ("stack trace", "стектрейсы"), ("code review", "код-ревью"),
    ("pull request review", "ревью PR"), ("design token", "дизайн-токены"),
    ("impact analysis", "влияние зависимостей"), ("context budget", "контекстный бюджет"),
    ("token budget", "бюджет токенов"), ("documentation", "документация"),
    ("e2e", "e2e-тесты"), ("test generation", "генерация тестов"),
    ("migration", "миграции"), ("query plan", "план запроса"),
    ("secret", "секреты"), ("dbt", "dbt/ETL"), ("browser", "браузер/скрейпинг"),
]


def fetch(url, tries=7):
    delay = 4
    for a in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "gap-audit"}), timeout=45)
            return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
        except Exception:
            pass
        if a < tries - 1:
            print(f"    сбой, повтор через {delay}с ({a+1}/{tries})", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 30)
    return None


ck_path = os.path.join(RAW, "registry_scan_ckpt.json")
if os.path.exists(ck_path):
    ck = json.load(open(ck_path, encoding="utf-8"))
    cursor = ck["cursor"]; entries = ck["entries"]; pages = ck["pages"]
    names = set(ck["names"]); counts = ck["counts"]; kinds = ck["kinds"]
    print(f"резюме с чекпоинта: стр.{pages}, записей {entries}", flush=True)
else:
    cursor, entries, pages = None, 0, 0
    names, counts, kinds = set(), {lab: 0 for _, lab in CLASSES}, {}
    kinds = {"remote": 0, "package": 0, "both": 0, "none": 0}

ns = {}
t0 = time.time()
while True:
    url = BASE + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
    d = fetch(url)
    if not d or not d.get("servers"):
        print("СТОП: нет данных или курсор кончился", flush=True)
        break
    for item in d["servers"]:
        s = item.get("server") or {}
        entries += 1
        nm = s.get("name") or ""
        names.add(nm)
        if "/" in nm:
            nsp = nm.split("/")[0]
            ns[nsp] = ns.get(nsp, 0) + 1
        hay = " ".join([nm, s.get("title") or "", s.get("description") or ""]).lower()
        for needle, label in CLASSES:
            if needle in hay:
                counts[label] += 1
        has_r = bool(s.get("remotes"))
        has_p = bool(s.get("packages"))
        if has_r and has_p:
            kinds["both"] += 1
        elif has_r:
            kinds["remote"] += 1
        elif has_p:
            kinds["package"] += 1
        else:
            kinds["none"] += 1
    pages += 1
    nc = (d.get("metadata") or {}).get("nextCursor")
    if pages % 25 == 0:
        print(f"  стр.{pages} записей={entries} имён={len(names)} "
              f"скорость={entries/(time.time()-t0):.0f} зап/с", flush=True)
        json.dump({"cursor": nc, "entries": entries, "pages": pages, "names": sorted(names),
                   "counts": counts, "kinds": kinds, "ns": ns},
                  open(ck_path, "w", encoding="utf-8"), ensure_ascii=False)
    if not nc:
        print("КУРСОР КОНЧИЛСЯ — каталог пройден полностью", flush=True)
        cursor = None
        break
    cursor = nc
    time.sleep(0.05)

out = {"entries": entries, "pages": pages, "unique_names": len(names),
       "namespaces": len(ns), "top_namespaces": sorted(ns.items(), key=lambda x: -x[1])[:20],
       "class_counts": counts, "delivery_kinds": kinds,
       "seconds": round(time.time() - t0), "finished": cursor is None}
json.dump(out, open(os.path.join(WORK, "..", "data", "registry_full_scan.json"), "w",
                    encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"ИТОГ: записей={entries}, уникальных имён={len(names)}, неймспейсов={len(ns)}", flush=True)
```

## `check_predecessors.py`

405 строк · 18382 байт · sha256 `05eadcb96d13`

```python
"""Проверка кандидатов и предшественников: npm, PyPI, adoption, MCP-поверхность.

Режимы:
  (по умолчанию)        таблица: версия, дата, скачивания, MCP-поверхность, вердикт
  --check-name ИМЯ      свободно ли имя в PyPI/npm (проверка перед публикацией)
  --from FILE           имена из requirements.txt / package.json / pyproject.toml
  --diff FILE           что изменилось с прошлого прогона (мониторинг конкурентов)
  --similar ИМЯ         похожие имена среди популярных (типосквоттинг, supply chain)

Запуск:
  python check_predecessors.py gerenuk redherring ci-triage
  python check_predecessors.py --top 30 --csv out.csv
  python check_predecessors.py --from requirements.txt --diff raw/predecessors.json
"""
import argparse, csv, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "ecosystem-gap-audit"}
PRED_KEYWORDS = ("mcp", "model context protocol")
COL_LABEL = "MCP"          # заголовок колонки; меняется вместе с --keyword
ALIVE_WEEKLY, ALIVE_MONTHLY = 100, 400
DEAD_WEEKLY, DEAD_MONTHLY = 10, 40
WORKERS, CACHE_TTL_H = 6, 12
STALE_DAYS = 540          # нет релизов дольше — «ЗАСТОЙ» даже при высоком adoption
CACHE_PATH, POPULAR_URL = "raw/.pcache.json", (
    "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.min.json")
_CACHE, _CACHE_DIRTY = {}, False


def _load_cache():
    global _CACHE
    if os.path.exists(CACHE_PATH):
        try:
            _CACHE = json.load(open(CACHE_PATH, encoding="utf-8"))
        except Exception:
            _CACHE = {}


def _save_cache():
    if not _CACHE_DIRTY:
        return
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    json.dump(_CACHE, open(CACHE_PATH, "w", encoding="utf-8"))


def get(url, timeout=25, tries=3, raw=False):
    """GET с повторами и кэшем на CACHE_TTL_H часов. Кэш делает повторные прогоны мгновенными."""
    global _CACHE_DIRTY
    key = "GET " + url
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_H * 3600:
        return hit[1][0], hit[1][1]
    delay = 2
    for attempt in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
            body = r.read().decode("utf-8", "replace")
            val = (r.status, body if raw else json.loads(body))
            _CACHE[key] = [time.time(), val]
            _CACHE_DIRTY = True
            return val
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                _CACHE[key] = [time.time(), [e.code, None]]
                _CACHE_DIRTY = True
                return e.code, None            # 404 — валидный ответ: пакета нет
            if e.code == 429 or e.code >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            return e.code, None
        except Exception:
            time.sleep(delay)
            delay = min(delay * 2, 20)
    return None, None


def pypi(name):
    st, d = get("https://pypi.org/pypi/%s/json" % urllib.parse.quote(name))
    if st != 200 or not d:
        return None
    info = d.get("info") or {}
    ver = info.get("version") or "?"
    rel = (d.get("releases") or {}).get(ver) or []
    text = ((info.get("summary") or "") + " " + (info.get("description") or "")).lower()
    return {"ver": ver, "date": (rel[0].get("upload_time") or "?")[:10],
            "summary": (info.get("summary") or "")[:70],
            "has_mcp": any(k in text for k in PRED_KEYWORDS),
            "home": (info.get("home_page") or "")[:80]}


def npm(name):
    st, d = get("https://registry.npmjs.org/%s" % urllib.parse.quote(name, safe="@/"))
    if st != 200 or not d or "error" in d:
        return None
    latest = (d.get("dist-tags") or {}).get("latest") or "?"
    vmeta = (d.get("versions") or {}).get(latest, {})
    text = json.dumps(vmeta).lower()
    return {"ver": latest, "date": ((d.get("time") or {}).get(latest) or "?")[:10],
            "summary": (vmeta.get("description") or "")[:70],
            "has_mcp": any(k in text for k in PRED_KEYWORDS),
            "home": str(vmeta.get("repository") or vmeta.get("homepage") or "")[:80]}


def adoption(name):
    """(за_месяц, за_неделю) — из PyPI, при отсутствии из npm."""
    st, d = get("https://pypistats.org/api/packages/%s/recent" % urllib.parse.quote(name))
    if st == 200 and d:
        dd = d.get("data") or {}
        return dd.get("last_month"), dd.get("last_week")
    st, d = get("https://api.npmjs.org/downloads/point/last-week/%s"
               % urllib.parse.quote(name, safe="@/"))
    if st == 200 and d:
        w = d.get("downloads")
        return (w * 4 if w is not None else None), w
    return None, None


def days_since(date_str):
    """Сколько дней с даты релиза. None — если даты нет."""
    try:
        d = time.strptime(date_str[:10], "%Y-%m-%d")
        return int((time.time() - time.mktime(d)) / 86400)
    except Exception:
        return None


def verdict(P, N, m, w):
    if not P and not N:
        return "ОТСУТСТВУЕТ"
    if w is not None:
        return "ЖИВО" if w >= ALIVE_WEEKLY else ("МЁРТВО" if w < DEAD_WEEKLY else "СЛАБО")
    if m is not None:
        return "ЖИВО" if m >= ALIVE_MONTHLY else ("МЁРТВО" if m < DEAD_MONTHLY else "СЛАБО")
    return "ЕСТЬ (adoption не измерен)"


def check(name):
    P, N = pypi(name), npm(name)
    m, w = adoption(name)
    info = P or N or {}
    v = verdict(P, N, m, w)
    d = days_since(info.get("date") or "")
    if v == "ЖИВО" and d is not None and d > STALE_DAYS:
        v = "ЗАСТОЙ"      # скачивания есть, развития нет: зависимость-зомби
    return {"name": name, "src": "pypi" if P else ("npm" if N else "-"),
            "ver": info.get("ver"), "date": info.get("date"),
            "age_days": d, "summary": info.get("summary"), "home": info.get("home"),
            "mcp": bool((P or {}).get("has_mcp") or (N or {}).get("has_mcp")),
            "month": m, "week": w, "verdict": v}


def check_many(names):
    """Параллельно: 30 имён за секунды вместо минут. Порядок сохраняется."""
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(check, names))


def lev(a, b, cap=3):
    """Расстояние Левенштейна с обрывом: больше cap не интересно."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def names_from_file(path):
    """Имена из requirements.txt / package.json / pyproject.toml (без внешних парсеров)."""
    try:
        text = open(path, encoding="utf-8").read()
    except Exception as e:
        print("не читается %s: %s" % (path, e))
        return []
    if path.endswith("package.json"):
        try:
            d = json.loads(text)
            out = list((d.get("dependencies") or {}) | (d.get("devDependencies") or {}))
            return [n for n in out if not str(n).startswith("@types/")]
        except Exception:
            return []
    if path.endswith(".toml"):
        out = []
        for line in text.splitlines():
            m = re.match(r'^\s*["\']?([A-Za-z0-9_.\-]+)["\']?\s*[=>~!\[]', line.strip())
            if m and not line.strip().startswith("#"):
                out.append(m.group(1))
        return sorted(set(out))
    out = []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if m:
            out.append(m.group(1))
    return sorted(set(out))


def popular_names():
    """Топ PyPI по скачиваниям (внешний датасет) — для поиска похожих имён."""
    st, d = get(POPULAR_URL, timeout=40, tries=2)
    if st != 200 or not d:
        return []
    rows = d.get("rows") if isinstance(d, dict) else None
    if not rows:
        return []
    return [r.get("project") for r in rows if r.get("project")]


def similar_names(name, cap=2):
    """Похожие имена: PyPI-топ + поиск npm по подстроке. Защита от типосквоттинга."""
    hits = {}
    for p in popular_names():
        if p.lower() != name.lower():
            d = lev(name.lower(), p.lower(), cap)
            if d <= cap:
                hits[p] = min(d, hits.get(p, 9))
    st, d = get("https://registry.npmjs.org/-/v1/search?size=25&text=%s"
               % urllib.parse.quote(name))
    if st == 200 and d:
        for o in d.get("objects") or []:
            p = (o.get("package") or {}).get("name")
            if p and p.lower() != name.lower():
                dd = lev(name.lower(), p.lower(), cap)
                if dd <= cap:
                    hits[p] = min(dd, hits.get(p, 9))
    return sorted(hits.items(), key=lambda x: (x[1], x[0]))[:15]


def diff_runs(prev, cur):
    """Что изменилось с прошлого прогона. Возвращает список событий."""
    old = {r["name"]: r for r in prev}
    new = {r["name"]: r for r in cur}
    events = []
    for n, c in new.items():
        o = old.get(n)
        if not o:
            events.append(("НОВОЕ", n, "не было в прошлом прогоне"))
            continue
        if o.get("ver") != c.get("ver"):
            events.append(("РЕЛИЗ", n, f"{o.get('ver')} → {c.get('ver')} ({c.get('date')})"))
        if not o.get("mcp") and c.get("mcp"):
            events.append(("MCP ПОЯВИЛСЯ", n,
                           "вендор закрывает нишу — пересмотреть решение об интеграции"))
        if o.get("mcp") and not c.get("mcp"):
            events.append(("MCP УБРАН?", n, "проверить вручную"))
        ow, cw = o.get("week"), c.get("week")
        if ow and cw and ow > 0:
            delta = (cw - ow) / ow * 100
            if abs(delta) >= 25:
                events.append(("ADOPTION", n, f"{ow} → {cw} за нед ({delta:+.0f}%)"))
        if o.get("verdict") != c.get("verdict"):
            events.append(("ВЕРДИКТ", n, f"{o.get('verdict')} → {c.get('verdict')}"))
    for n in old:
        if n not in new:
            events.append(("ПРОПАЛО", n, "нет в текущем прогоне"))
    return events


def print_table(rows):
    print(f"{'имя':<30}{'источник':<9}{'версия':<12}{'дата':<12}{'мес':>11}{'нед':>10}{COL_LABEL:>6}  вердикт")
    for r in rows:
        m = "-" if r["month"] is None else str(r["month"])
        w = "-" if r["week"] is None else str(r["week"])
        print(f"{r['name'][:29]:<30}{r['src']:<9}{(r['ver'] or '-'):<12}{(r['date'] or '-'):<12}"
              f"{m:>11}{w:>10}{('да' if r['mcp'] else 'нет'):>6}  {r['verdict']}")
        if r.get("summary"):
            print(f"      {r['summary']}")


def print_summary(rows):
    from collections import Counter
    c = Counter(r["verdict"] for r in rows)
    mcp_yes = sum(1 for r in rows if r["mcp"])
    print("\nИТОГ: " + ", ".join(f"{k} {v}" for k, v in c.most_common()))
    print(f"Признак «{COL_LABEL}» в самом пакете: да у {mcp_yes} из {len(rows)}")
    best = [r["name"] for r in rows if r["verdict"] == "ЖИВО" and not r["mcp"]]
    if best:
        print(f"ЛУЧШИЕ КАНДИДАТЫ (живые, {COL_LABEL} нет): " + ", ".join(best[:10]))
    dead = [r["name"] for r in rows if r["verdict"] == "ОТСУТСТВУЕТ"]
    if dead:
        print("ПУСТО (пакета нет ни в PyPI, ни в npm): " + ", ".join(dead[:10]))
    print(f"Правило: «{COL_LABEL}=нет» = у пакета нет этого признака — это и есть незанятая часть")
    print("ниши. МЁРТВО/СЛАБО = 'опубликовано и не принято': вход возможен, но выигрыш в качестве")
    print("и adoption, а не в новизне. Признак задаётся флагом --keyword (mcp, helm, otel, deprecated).")


def name_availability(names):
    """Перед публикацией: свободно ли имя. Занятое имя — менять бренд сейчас, не после релиза."""
    st = []
    for n in names:
        r = check(n)
        st.append(r)
        if r["verdict"] == "ОТСУТСТВУЕТ":
            print(f"СВОБОДНО    {n} — нет ни в PyPI, ни в npm")
        else:
            where = r["src"]
            dl = r["week"] if r["week"] is not None else r["month"]
            unit = "нед" if r["week"] is not None else "мес"
            print(f"ЗАНЯТО      {n} — {where}, {r['ver']}, {r['date']}, {dl or '?'} скач/{unit}")
            if r["summary"]:
                print(f"            {r['summary']}")
    return st


def main():
    ap = argparse.ArgumentParser(
        description="Проверка кандидатов и предшественников: npm/PyPI, adoption, MCP-поверхность")
    ap.add_argument("names", nargs="*", help="имена пакетов или owner/repo")
    ap.add_argument("--top", type=int, default=0, help="взять топ-N из gap_tools.json")
    ap.add_argument("--from", dest="from_file", help="requirements.txt / package.json / pyproject.toml")
    ap.add_argument("--keyword", action="append", metavar="СЛОВО",
                    help="признак для колонки (по умолчанию mcp); можно повторять для синонимов")
    ap.add_argument("--check-name", nargs="+", metavar="ИМЯ",
                    help="свободно ли имя в PyPI/npm (для нового продукта)")
    ap.add_argument("--similar", metavar="ИМЯ", help="похожие имена (типосквоттинг)")
    ap.add_argument("--diff", metavar="PREV.json", help="сравнить с прошлым прогоном")
    ap.add_argument("--json", default="raw/predecessors.json", help="куда сохранить результат")
    ap.add_argument("--csv", metavar="FILE", help="выгрузить таблицу в CSV")
    ap.add_argument("--strict", action="store_true",
                    help="код выхода 2, если есть ЖИВО или MCP=да (гейт для cron/CI)")
    a = ap.parse_args()
    global PRED_KEYWORDS, COL_LABEL
    if a.keyword:
        PRED_KEYWORDS = tuple(k.lower() for k in a.keyword)
        COL_LABEL = a.keyword[0][:6].upper()
    _load_cache()

    if a.check_name:
        name_availability(a.check_name)
        _save_cache()
        return 0

    if a.similar:
        hits = similar_names(a.similar)
        print(f"Похожие на «{a.similar}» (расстояние Левенштейна ≤ 2):")
        for p, d in hits:
            print(f"  d={d}  {p}")
        if not hits:
            print("  ничего похожего не найдено")
        print("Если похожее имя есть, а оно популярно — это кандидат на типосквоттинг:")
        print("сверяй точное имя перед установкой.")
        _save_cache()
        return 0

    names = list(a.names)
    if a.from_file:
        extra = names_from_file(a.from_file)
        print(f"из {a.from_file} взято имён: {len(extra)}")
        names += extra
    if a.top:
        for cand in ("raw/gap_tools.json", "../data/gap_tools.json", "gap_tools.json"):
            if os.path.exists(cand):
                items = json.load(open(cand, encoding="utf-8"))
                names += [i["full_name"].split("/")[1] for i in items[:a.top]]
                break
    names = sorted(dict.fromkeys(names))
    if not names:
        print("нечего проверять: передай имена, --from FILE, --top N, --check-name или --similar")
        return 1

    rows = check_many(names)
    print_table(rows)
    print_summary(rows)

    os.makedirs(os.path.dirname(a.json) or ".", exist_ok=True)
    json.dump(rows, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nСОХРАНЕНО: {a.json}  (пригоден для --diff в следующий раз)")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"CSV: {a.csv}")

    if a.diff and os.path.exists(a.diff):
        try:
            prev = json.load(open(a.diff, encoding="utf-8"))
            events = diff_runs(prev, rows)
            print(f"\nИЗМЕНЕНИЯ с {a.diff} ({len(events)}):")
            for kind, n, what in events:
                print(f"  {kind:<14} {n:<28} {what}")
            if not events:
                print("  ничего не изменилось")
        except Exception as e:
            print(f"не удалось сравнить с {a.diff}: {e}")

    _save_cache()
    if a.strict and any(r["verdict"] == "ЖИВО" or r["mcp"] for r in rows):
        print("STRICT: есть живые проекты или MCP-поверхность → код выхода 2")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

## `collect.py`

124 строк · 5311 байт · sha256 `170fa815fff7`

```python
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
```

## `coverage.py`

128 строк · 6827 байт · sha256 `72e1f9177d0a`

```python
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
```

## `verify.py`

159 строк · 7190 байт · sha256 `dc8ddb238ff6`

```python
"""Точечная проверка кандидатов: НЕТ / МЁРТВАЯ ЗАГЛУШКА / СЛАБЫЙ / ЖИВОЙ.

Запуск:
  python verify.py                # возьмёт raw/gap_tools.json (топ-KEYWORD)
  python verify.py owner/repo ... # или явный список
  python verify.py --keyword "helm chart" traefik/traefik   # другая экосистема

Ключевое слово задаёт вопрос, на который отвечает проверка: «mcp», «vscode extension»,
«helm chart», «terraform provider», «plugin» — всё равно, механика та же. Проверка
перестаёт быть про MCP и становится про “есть ли у инструмента что-то из класса X”.

Вердикты: только по ней, где имя инструмента реально в имени/описании найденного
репозитория — иначе поиск возвращает чужие проекты и ниша ложно «закрыта».
"""
import json, os, re, subprocess, sys, time, datetime

KEYWORD = "mcp"           # ключевое слово экосистемы
STUB_STARS = 25           # < — заглушка
STUB_DAYS = 365           # push старше — заглушка
LIVE_STARS = 100          # >= вместе с LIVE_DAYS — живой
LIVE_DAYS = 180
TOP_K = 25                # сколько брать из gap_tools.json
RAW = "raw"


def age_days(pushed):
    if not pushed:
        return 9999
    try:
        d = datetime.datetime.fromisoformat(pushed.replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - d).days
    except Exception:
        return 9999


def search(q, per_page=8):
    for _ in range(3):
        p = subprocess.run(["gh", "api", "-X", "GET", "search/repositories",
                            "-f", f"q={q}", "-f", "sort=stars", "-f", "order=desc",
                            "-f", f"per_page={per_page}"],
                           capture_output=True, text=True, encoding="utf-8")
        if p.returncode == 0:
            try:
                d = json.loads(p.stdout)
                return d.get("total_count", 0), d.get("items", [])
            except Exception:
                pass
        time.sleep(5)
    return None, []


def org_candidates(owner, target):
    """Репозитории самой организации вендора с ключевым словом в имени/описании.

    Нужны потому, что у крупных проектов репозиторий часто назван по назначению, а не по
    продукту (`grafana/helm-charts`, `Dokploy/mcp`) — поиск по имени продукта их теряет.
    """
    for endpoint in (f"orgs/{owner}/repos", f"users/{owner}/repos"):
        p = subprocess.run(["gh", "api", "-X", "GET", endpoint,
                            "-f", "per_page=100", "-f", "sort=updated"],
                           capture_output=True, text=True, encoding="utf-8")
        if p.returncode == 0:
            try:
                repos = json.loads(p.stdout)
            except Exception:
                continue
            out = []
            for r in repos if isinstance(repos, list) else []:
                full = r.get("full_name", "")
                if full.lower().startswith(target.lower() + "/"):
                    continue
                hay = (full + " " + (r.get("description") or "")).lower()
                if KEYWORD in hay:
                    out.append(r)
            return out
        time.sleep(1)
    return []


def relevant(items, name, target=None):
    pat = r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])"
    out = []
    for i in items:
        # сам инструмент не является сервером для себя — иначе ложный «ЖИВОЙ»
        if target and i["full_name"].lower() == target.lower():
            continue
        hay = (i["full_name"] + " " + (i.get("description") or "")).lower()
        if re.search(pat, hay) and KEYWORD in hay:
            out.append(i)
    # свой сервер важнее: при равенстве звёзд берём тот, где ключевое слово в имени
    out.sort(key=lambda i: (KEYWORD in i["full_name"].lower(), i["stargazers_count"]), reverse=True)
    return out


def verdict(best):
    if not best:
        return "НЕТ"
    st, d = best["stargazers_count"], age_days(best.get("pushed_at"))
    if st < STUB_STARS or d > STUB_DAYS:
        return "ЗАГЛУШКА"
    if st >= LIVE_STARS and d <= LIVE_DAYS:
        return "ЖИВОЙ"
    return "СЛАБЫЙ"


def main():
    global KEYWORD
    argv = sys.argv[1:]
    if "--keyword" in argv:
        i = argv.index("--keyword")
        if i + 1 < len(argv):
            KEYWORD = argv[i + 1].lower()
            del argv[i:i + 2]
    if argv:
        targets = argv
        base = {}
    else:
        gt = json.load(open(os.path.join(RAW, "gap_tools.json"), encoding="utf-8"))
        gt.sort(key=lambda r: -r["stars"])
        targets = [r["full_name"] for r in gt[:TOP_K]]
        base = {r["full_name"]: r for r in gt}

    res = {}
    for t in targets:
        name = t.split("/")[1].lower()
        total, items = search(f"{name} {KEYWORD}")
        hits = relevant(items, name, target=t)
        # плюс репозитории организации вендора: там сервер может называться по назначению
        org_hits = org_candidates(t.split("/")[0], target=t)
        for r in org_hits:
            r["full_name"] += "  «org»"
        merged = org_hits + hits
        merged.sort(key=lambda i: (KEYWORD in i["full_name"].lower(),
                                   i["stargazers_count"]), reverse=True)
        best = merged[0] if merged else None
        v = verdict(best)
        res[t] = {"stars": base.get(t, {}).get("stars"), "desc": base.get(t, {}).get("desc", ""),
                  "search_total": total, "verdict": v,
                  "best": None if not best else {"full_name": best["full_name"],
                            "stars": best["stargazers_count"], "pushed": best.get("pushed_at"),
                            "age_days": age_days(best.get("pushed_at"))}}
        if best:
            print(f'{t[:40]:<41} {v:<10} -> {best["full_name"][:38]:<39} '
                  f'{best["stargazers_count"]:>5}★ {age_days(best.get("pushed_at")):>5}д')
        else:
            print(f'{t[:40]:<41} {v:<10} -> (найдено {total} результатов, релевантных {KEYWORD} нет)')
        time.sleep(2.5)

    json.dump(res, open(os.path.join(RAW, "verify.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    cnt = {}
    for v in res.values():
        cnt[v["verdict"]] = cnt.get(v["verdict"], 0) + 1
    print("\nитог:", cnt)
    print("ЗАГЛУШКА и НЕТ — это пустые ниши; ЖИВОЙ — не лезь без причины.")


if __name__ == "__main__":
    main()
```
