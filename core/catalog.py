"""Сборка каталога проектов по поисковым фразам (шаг 1d).

Порт legacy ``catalog_build.py``: те же источники (GitHub Search по описанию и README,
npm search, официальный реестр MCP), та же нормализация строки каталога, та же склейка
дублей между источниками. Отличия — по правилам переезда в ``core`` (DEVPLAN §4):

* ничего не печатает и не пишет: ``report()`` отдаёт словарь, markdown — строку, файлы
  и таблицу рисует вызывающий (CLI-обёртка шага 1f, UI);
* клиенты инжектируются (:class:`core.github.GhClient`, :class:`core.registry.RegistryClient`,
  :class:`core.http.HttpClient` для реестра MCP) — ни сети, ни кэша на уровне модуля;
* паузы между страницами держит троттлинг клиентов, ``time.sleep`` здесь нет;
* прогресс идёт колбэком ``on_progress(0..1, сообщение)``.

Строка каталога (её понимают все источники и ``dedupe()`` без спецобработки):
``source``, ``id``, ``url``, ``desc``, ``stars``, ``lang``, ``pushed``, ``topics``,
``query``; после склейки добавляется ``sources`` — список источников одной находки.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Callable

from core.diffing import days_since
from core.github import GhClient
from core.mcp_registry import DEFAULT_LIMIT, search as mcp_search
from core.registry import RegistryClient

__all__ = [
    "DEFAULT_PAGES",
    "GH_PER_PAGE",
    "NOISE_WORDS",
    "NPM_SIZE",
    "SOURCE_ORDER",
    "build_catalog",
    "dedupe",
    "gh_search",
    "is_noise",
    "relevant",
    "registry_search",
    "report",
    "short_name",
    "to_markdown",
    "words_of",
]

#: Сколько репозиториев отдаёт одна страница GitHub Search (как в legacy).
GH_PER_PAGE = 100
#: Сколько страниц на фразу по умолчанию (как в legacy).
DEFAULT_PAGES = 2
#: Размер выборки npm search для каталога (как в legacy).
NPM_SIZE = 250
#: Порядок источников в сборке — он же порядок строк в каталоге.
SOURCE_ORDER = ("github", "npm", "registry")

#: Хвосты имён, отсекаемые при склейке дублей (как в legacy).
SHORT_NAME_SUFFIXES = ("-py", "_py", ".py", "-js", "-ts", "-mcp", "-server", "-cli", "-core")
#: Обрезка описания в строке каталога — как в legacy.
DESC_LIMIT = 200

#: Свежесть в отчёте: до 90 дней — «живой», до 365 — «за год».
FRESH_DAYS = 90
YEAR_DAYS = 365

#: Стоп-слова: подборки, курсы, учебные репозитории — это списки, а не проекты.
NOISE_WORDS = (
    "awesome",
    "курс",
    "course",
    "cheatsheet",
    "шпаргалка",
    "tutorial",
    "boilerplate",
    "example",
    "interview",
    "roadmap",
    "resources",
    "list of",
    "collection of",
    "подборка",
    "books",
    "papers",
)


def short_name(name: str | None) -> str:
    """Нормализованное имя для склейки дублей МЕЖДУ источниками.

    ``owner/Проект``, ``project-py``, ``@scope/project`` — всё это одно и то же.
    Порт legacy ``short_name()``: имя после последнего ``/``, отсечение хвостов
    (их может быть несколько подряд — цикл, а не ``if/elif``), затем выбрасывание
    всего, кроме ``[a-z0-9]``. Кириллица при этом исчезает целиком: legacy-регексп
    её не пропускает, и поведение сохранено намеренно — иначе паритетный тест
    с legacy разойдётся, а имена проектов в каталоге латиницей.
    """
    lowered = (name or "").lower().split("/")[-1]
    for suffix in SHORT_NAME_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
    return re.sub(r"[^a-z0-9]", "", lowered)


def dedupe(rows: list[dict]) -> list[dict]:
    """Склеить дубли внутри источника и между источниками — порт legacy ``dedupe()``.

    Один проект живёт и на GitHub, и в npm, и в реестре MCP: в каталоге он должен
    быть один раз, но знать про него нужно всё сразу — звёзды, ссылку, язык, теги.
    Ключ склейки — :func:`short_name` от ``id``; строки с пустым ключом отбрасываются.

    Что от чьей строки достаётся (как в legacy): ``stars``/``id``/``url``/``lang`` —
    от строки с большим числом звёзд, ``pushed`` — максимум по датам, ``desc`` — самый
    длинный, ``queries`` — объединение фраз всех вошедших строк.
    """
    merged: dict[str, dict] = {}
    for row in rows:
        key = short_name(row["id"])
        if not key:
            continue
        current = merged.get(key)
        if not current:
            merged[key] = dict(row, sources=[row["source"]])
            continue
        current["sources"].append(row["source"])
        if (row.get("stars") or 0) > (current.get("stars") or 0):
            current.update(
                {
                    "stars": row["stars"],
                    "id": row["id"],
                    "url": row.get("url") or current.get("url"),
                    "lang": row.get("lang") or current.get("lang"),
                }
            )
        if row.get("pushed") and row["pushed"] > (current.get("pushed") or ""):
            current["pushed"] = row["pushed"]
        if len(row.get("desc") or "") > len(current.get("desc") or ""):
            current["desc"] = row["desc"]
        current["queries"] = sorted(
            set(current.get("queries") or [current.get("query", "")]) | {row["query"]}
        )
    return list(merged.values())


def words_of(phrase: str) -> list[str]:
    """Значимые слова фразы — для проверки релевантности выдачи (от 4 символов)."""
    return [word for word in re.findall(r"[a-z0-9]{4,}", (phrase or "").lower())]


def _haystack(row: dict) -> str:
    return ((row.get("id") or "") + " " + (row.get("desc") or "")).lower()


def relevant(row: dict, words: list[str]) -> bool:
    """Релевантен ли проект фразе: значимое слово есть в имени или описании.

    Без этой проверки поиск по описанию тянет всё подряд: в legacy-прогоне по фразе
    «test impact analysis» выдача начиналась с awesome-списков на 520 тыс. звёзд.
    Пустой список слов — «релевантно всё» (как в legacy).
    """
    if not words:
        return True
    haystack = _haystack(row)
    return any(word in haystack for word in words)


def is_noise(row: dict) -> bool:
    """Отсекает подборки, курсы и учебные репозитории — они не проекты, а списки."""
    haystack = _haystack(row)
    return any(word in haystack for word in NOISE_WORDS)


def _age_days(row: dict) -> int | None:
    """Сколько дней назад был push; ``None`` — данных нет (порт ``activity_days()``)."""
    return days_since(row.get("pushed"))


def _sources_of(row: dict) -> list[Any]:
    """Источники строки: после склейки их список, до неё — единственный ``source``."""
    sources = row.get("sources")
    return list(sources) if sources else [row.get("source")]


def report(rows: list[dict], *, top: int = 15) -> dict:
    """Сводка по каталогу — словарь вместо legacy-печати (её оставляем CLI).

    Поля: ``total``, ``by_source``, ``languages`` (по частоте), ``fresh_90d``,
    ``year_365d``, ``abandoned_365d``, ``top_by_stars`` (до ``top`` строк) и ``topics``.

    Отличие от legacy ``report()``: там свежесть считалась как
    ``(activity_days(...) or 9999) <= 90``, из-за чего push сегодняшнего дня (возраст 0)
    выглядел как «данных нет» — проект попадал в заброшенные сразу после релиза.
    Здесь ноль — валидный возраст; «нет данных» — только ``None``.
    """
    total = len(rows)
    by_source = Counter(source for row in rows for source in _sources_of(row))
    languages = Counter(row.get("lang") for row in rows if row.get("lang"))
    ages = [_age_days(row) for row in rows]
    fresh = sum(1 for age in ages if age is not None and age <= FRESH_DAYS)
    year = sum(1 for age in ages if age is not None and age <= YEAR_DAYS)
    with_stars = sorted(
        (row for row in rows if row.get("stars")), key=lambda row: -row["stars"]
    )
    topics = Counter(topic for row in rows for topic in (row.get("topics") or []))

    return {
        "total": total,
        "by_source": dict(by_source.most_common()),
        "languages": dict(languages.most_common()),
        "fresh_90d": fresh,
        "year_365d": year,
        "abandoned_365d": total - year,
        "top_by_stars": [
            {
                "stars": row.get("stars"),
                "id": row.get("id"),
                "lang": row.get("lang"),
                "pushed": row.get("pushed"),
                "desc": row.get("desc"),
            }
            for row in with_stars[:top]
        ],
        "topics": dict(topics.most_common()),
    }


def to_markdown(rows: list[dict], title: str) -> str:
    """Каталог в читаемом виде: виден и человеку, и агенту без питона.

    Порт legacy ``to_markdown()``: строки отсортированы по звёздам, столбцы
    «Звёзды | Проект | Язык | Push | Источники | Описание», описания обрезаны до 120
    символов, ``|`` внутри описания заменяется на ``/`` (иначе таблица разъедется).
    """
    ordered = sorted(rows, key=lambda row: -(row.get("stars") or 0))
    out = [
        f"# Каталог: {title}",
        "",
        f"Всего проектов: {len(ordered)}",
        "",
        "| Звёзды | Проект | Язык | Push | Источники | Описание |",
        "|---:|---|---|---|---|---|",
    ]
    for row in ordered:
        age = _age_days(row)
        out.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                row.get("stars") if row.get("stars") else "-",
                row.get("id") or "-",
                row.get("lang") or "-",
                f"{age}д" if age is not None else "-",
                ",".join(row.get("sources", [])),
                (row.get("desc") or "").replace("|", "/")[:120],
            )
        )
    return "\n".join(out) + "\n"


def gh_search(
    gh_client: GhClient,
    phrase: str,
    *,
    pages: int = DEFAULT_PAGES,
    min_stars: int = 0,
    on_page: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """GitHub Search по описанию и README с пагинацией — порт legacy ``gh_search()``.

    Запрос тот же: ``"<фраза>" in:description,readme`` плюс ``stars:>=N``, сортировка
    по звёздам. Обход останавливается, когда страница неполная (меньше
    :data:`GH_PER_PAGE`) или когда ``gh`` отдал ошибку (``total is None``).
    ``on_page(номер_страницы, всего_строк)`` — колбэк прогресса.
    """
    query = f'"{phrase}" in:description,readme' + (f" stars:>={min_stars}" if min_stars else "")
    rows: list[dict] = []
    for page in range(1, max(1, int(pages)) + 1):
        total, items = gh_client.search_repositories(query, per_page=GH_PER_PAGE, page=page)
        if total is None:
            break
        for item in items:
            rows.append(
                {
                    "source": "github",
                    "id": item.get("full_name"),
                    "url": item.get("html_url"),
                    "desc": str(item.get("description") or "")[:DESC_LIMIT],
                    "stars": item.get("stargazers_count"),
                    "lang": item.get("language"),
                    "pushed": str(item.get("pushed_at") or "")[:10],
                    "topics": item.get("topics") or [],
                    "query": phrase,
                }
            )
        if on_page is not None:
            on_page(page, len(rows))
        if len(items) < GH_PER_PAGE:
            break
    return rows


def registry_search(http: Any, phrase: str) -> list[dict]:
    """Реестр MCP по фразе — обёртка над :func:`core.mcp_registry.search`.

    Добавляет к каждой строке ``query`` — так же, как это делают остальные источники,
    иначе :func:`dedupe` не сможет собрать объединение фраз.
    """
    return [dict(row, query=phrase) for row in mcp_search(http, phrase, limit=DEFAULT_LIMIT)]


def build_catalog(
    phrases: list[str],
    *,
    gh_client: GhClient,
    registry_client: RegistryClient,
    mcp_registry_http: Any,
    pages: int = DEFAULT_PAGES,
    min_stars: int = 0,
    sources: tuple[str, ...] = SOURCE_ORDER,
    merge_rows: list[dict] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> dict:
    """Собрать каталог по фразам — порт legacy ``catalog_build.main()`` без печати и файлов.

    Порядок: строки из ``merge_rows`` (уже собранный каталог) + выдача по каждой фразе
    из выбранных источников -> :func:`dedupe` -> фильтр шума (:func:`is_noise`) ->
    фильтр релевантности (:func:`relevant` по словам всех фраз) -> :func:`report`.
    Возвращает ``{"queries": [...], "stats": report(...), "items": [...]}`` — форма
    legacy-файла каталога, поэтому ``--json-out`` CLI и БД шага 2 читают её как есть.

    Фразы дедуплицируются с сохранением порядка (``dict.fromkeys``, как в legacy).
    ``on_progress`` вызывается после каждого шага источника; прогресс — доля шагов.
    """
    unique_phrases = list(dict.fromkeys(phrases))
    enabled = [name for name in SOURCE_ORDER if name in sources]
    rows: list[dict] = list(merge_rows or [])
    total_steps = len(unique_phrases) * len(enabled)
    done = 0

    for index, phrase in enumerate(unique_phrases, start=1):
        for source in enabled:
            if source == "github":
                batch = gh_search(gh_client, phrase, pages=pages, min_stars=min_stars)
            elif source == "npm":
                batch = registry_client.npm_search_full(phrase, size=NPM_SIZE)
            else:
                batch = registry_search(mcp_registry_http, phrase)
            rows += batch
            done += 1
            if on_progress is not None:
                on_progress(
                    done / total_steps if total_steps else 1.0,
                    f"фраза {index}/{len(unique_phrases)}: «{phrase}» → {source}: {len(batch)}",
                )

    words = [word for phrase in unique_phrases for word in words_of(phrase)]
    rows = dedupe(rows)
    rows = [row for row in rows if not is_noise(row) and relevant(row, words)]
    return {"queries": unique_phrases, "stats": report(rows), "items": rows}
