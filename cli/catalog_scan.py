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
