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