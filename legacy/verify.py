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
