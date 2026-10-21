"""CLI-обёртка Niche Finder (шаг P5f-1, DEVPLAN §19).

Запуск (как job)::

    python -m cli.niche run --query "vector database"
                             --min-stars 20 --pages 2
                             --signal-kinds jobs --signal-kinds patents
                             --job-json --json-out <file>

Pipeline — четыре шага, каждый следующий читает результат предыдущего:

1. ``catalog.build`` по фразе (:func:`core.catalog.build_catalog`) — проекты,
   чьё описание содержит фразу (не только имена);
2. ``packages.check`` по найденным именам (:func:`core.packages.check_many`) —
   adoption, verdict, признак ниши (``flag``);
3. ``signals.collect`` по именам — только те виды, для которых настроены
   ключи (``core.api_keys.ApiKeysStore``); не настроенный вид пропускается
   целиком (graceful degradation, §18.1);
4. ``gap_score`` по всем найденным пакетам
   (:func:`core.signals.compute_gap_scores_batch`) — один нормализатор
   (min-max) на всю выборку, как у матрицы (§18.3).

Результат — единый ``result.json`` (его читает ``GET /api/niche/result``)::

    {"query": "...",
     "items": [{"name": "chromadb", "registry_name": "chromadb",
                "gap_score": 87.0, "momentum_30d": 12.5,
                "flag": false, "adoption_week": 2100000, "verdict": "ЖИВО",
                "funding_total": 0, "patents_impulse_pct": 5.0,
                "jobs_total": 1200, "search_interest_trend": 12.5,
                "sources": ["github", "npm"], ...}],
     "stats": {"total": N, "found": F, "checked": C, "with_gap": M,
               "signals_used": ["jobs"]}}

``found`` — сколько строк нашёл каталог, ``checked`` — сколько из них реально
проверено (``CHECK_LIMIT``): экран показывает «Проверено 50 из 139».

Подкоманда ``run`` — единственная; она разбирается как необязательный
позиционный аргумент (``nargs="?"``), поэтому работает при любом порядке
флагов вокруг неё: ``_default_runner`` собирает
``python -m cli.niche --job-json --json-out=X run --query ...``, а человек
пишет ``python -m cli.niche run --query ...``. Урок gotcha 62: подкоманда —
первый ПОЗИЦИОННЫЙ токен, и ``--query run`` подкомандой не считается
(argparse сам знает, что ``run`` — значение ``--query``).

Осознанные упрощения (P5f-1, часть снята в P5f-2):

* **momentum читается из ``Store`` (P5f-2).** CLI открывает
  ``<data_dir>/gapaudit.db`` только на чтение истории наблюдений
  (``get_observations_history``) и считает ту же
  :func:`core.collectors.momentum.compute_momentum`, что и матрица — поэтому
  ``momentum_30d`` пакета совпадает с ``/api/matrix``. Базы нет или она битая —
  сигнал честно недоступен (``None``), а не подменён нулём: «нет данных» ≠ «не
  растёт» (graceful degradation, §18.1).
* **cross_ecosystem не собирается.** Его нет в формуле §18.3, а история
  наблюдений не даёт соседей по концепту — сигнал остаётся экранным
  (карточка, матрица).
* **CLI-подпроцесс, а не in-process pipeline**: все задачи проекта — CLI
  (шаг 3d-a), и Niche Finder не исключение.
* **Проверяются первые ``CHECK_LIMIT`` строк каталога** (P5f-2 показывает
  пользователю «проверено N из M»; батчи с паузами — P6, отложен).

Сеть — только в этом подпроцессе (её инициирует пользователь). Тесты
подменяют коллекторы и каталожные клиенты: ни одного запроса (gotcha 61).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from cli._common import (
    build_argparser,
    build_job_emitter,
    emit_done,
    emit_error,
    job_json_mode,
    job_json_requested,
    load_runtime,
    print_table,
    write_json,
)
from core.api_keys import ApiKeysStore
from core.catalog import build_catalog
from core.collectors.funding import compute_funding, default_transport as funding_transport
from core.collectors.jobs import compute_jobs, default_transport as jobs_transport
from core.collectors.momentum import compute_momentum
from core.collectors.patents import compute_patents, default_post_transport as patents_transport
from core.collectors.search_interest import (
    compute_search_interest,
    default_post_transport as search_interest_transport,
)
from core.github import GhClient
from core.packages import check_many
from core.registry import RegistryClient
from core.settings import Settings, resolve_data_dir
from core.signals import compute_gap_scores_batch, default_weights_from_settings

__all__ = ["NICHE_SIGNAL_KINDS", "SOURCES", "main"]

#: Источники каталога — те же три, что у ``catalog.build`` (``core.catalog``).
SOURCES: tuple[str, ...] = ("github", "npm", "registry")

#: Виды сигналов, которые умеет собирать Niche Finder. Дубль
#: ``cli.signals.SIGNAL_KINDS`` осознанно: ``POST /api/niche/find`` проверяет
#: набор сам (``SIGNAL_COLLECT_KINDS`` в ``app/web.py``), а этот модуль — то,
#: что реально исполняется в подпроцессе; «согласие слоёв» держит тест.
NICHE_SIGNAL_KINDS: tuple[str, ...] = ("jobs", "patents", "funding", "search_interest")

#: Вид сигнала -> провайдер ключей (``core.api_keys.PROVIDERS``). У
#: ``search_interest`` источников два (Wordstat + Apify), см.
#: :func:`_collect_search_interest`.
KIND_TO_PROVIDER: dict[str, str] = {
    "jobs": "adzuna",
    "patents": "patentsview",
    "funding": "edgar",
    "search_interest": "yandex_wordstat",
}

#: Пределы аргументов (те же, что у ``POST /api/niche/find``).
PAGES_MIN, PAGES_MAX = 1, 10
LIMIT_MIN, LIMIT_MAX = 1, 500

#: Куда писать результат без ``--json-out`` (как у прочих CLI: raw/).
DEFAULT_JSON = "raw/niche.json"

#: Сколько строк каталога проверять (реестры + сигналы). Потолок — не
#: «на всякий случай»: каждая строка это до трёх запросов к PyPI/npm/pypistats,
#: а троттлинг держит 30–60 запросов/мин на хост. Живой прогон P5f-1 по фразе
#: «test impact analysis»: 130 найденных строк, проверка всех упирается в
#: лимиты реестров — adoption не приходит ни у одной строки и все score равны
#: 100 (ранжирования нет). 50 — те же 50, что дефолтный ``--limit``; более
#: узкая выборка (``--min-stars``) поднимает качество, а не количество.
CHECK_LIMIT = 50

#: Сколько строк показывать в человекочитаемой таблице (полный список — в JSON).
TOP_ROWS = 5


def _build_parser() -> argparse.ArgumentParser:
    """argparse с общими флагами + флаги Niche Finder.

    ``run`` — необязательный позиционный: argparse не путает его со значением
    ``--query`` (``--query run`` — это значение флага), а лишний позиционный
    токен получает ``invalid choice`` и код 2.
    """
    parser = build_argparser(
        prog="niche.py",
        description=(
            "Поиск ниши: каталог по фразе → проверка пакетов → сигналы → Gap Score"
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run",),
        help="подкоманда (единственная: run)",
    )
    parser.add_argument(
        "--query",
        required=True,
        metavar="ФРАЗА",
        help="фраза поиска (по описанию проекта)",
    )
    parser.add_argument(
        "--min-stars", type=int, default=20, metavar="N",
        help="минимум звёзд GitHub (по умолчанию 20)",
    )
    parser.add_argument(
        "--pages", type=int, default=2, metavar="N",
        help=f"страниц GitHub Search на фразу ({PAGES_MIN}..{PAGES_MAX}, по умолчанию 2)",
    )
    parser.add_argument(
        "--sources", nargs="+", choices=SOURCES, default=list(SOURCES), metavar="ИСТОЧНИК",
        help=f"источники каталога: {' '.join(SOURCES)} (по умолчанию все)",
    )
    parser.add_argument(
        "--signal-kinds", nargs="+", choices=NICHE_SIGNAL_KINDS, default=None,
        metavar="ВИД",
        help=(
            "какие сигналы собирать (по умолчанию — ни одного: только adoption, "
            f"flag и score); допустимы: {', '.join(NICHE_SIGNAL_KINDS)}"
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=50, metavar="N",
        help=f"сколько строк оставить в результате ({LIMIT_MIN}..{LIMIT_MAX}, по умолчанию 50)",
    )
    return parser


def _check_bounds(args: argparse.Namespace) -> str | None:
    """Проверить границы числовых флагов; вернуть текст ошибки или ``None``."""
    if not PAGES_MIN <= args.pages <= PAGES_MAX:
        return f"pages: допустимо {PAGES_MIN}..{PAGES_MAX}, пришло {args.pages}"
    if not LIMIT_MIN <= args.limit <= LIMIT_MAX:
        return f"limit: допустимо {LIMIT_MIN}..{LIMIT_MAX}, пришло {args.limit}"
    if args.min_stars < 0:
        return f"min-stars: не может быть отрицательным, пришло {args.min_stars}"
    return None


# --- шаги конвейера -----------------------------------------------------------


def _registry_name(identifier: str) -> str:
    """Имя для запросов в реестры из id строки каталога.

    У GitHub-строки id — полный путь ``owner/repo``, а PyPI/npm знают проект
    под именем репозитория: запрос ``pypi/owner%2Frepo`` всегда 404, и adoption
    (а с ним и половина Gap Score) молча остаётся ``None``. Живой прогон по
    фразе «test impact analysis» (P5f-1): 139 найденных строк, у всех
    ``adoption_week = None``, все score = 100 по единственной компоненте
    ``no_flag`` — ранжирования нет.

    Поэтому для id с косой чертой берётся последний сегмент, а scoped-имена npm
    (``@scope/pkg``) остаются как есть: там косая черта — часть имени, а не
    путь репозитория.
    """
    text = (identifier or "").strip()
    if not text or "/" not in text or text.startswith("@"):
        return text
    return text.rsplit("/", 1)[-1]


def _catalog(config: "_Config", on_progress: Any) -> dict[str, Any]:
    """Шаг 1: каталог по фразе (``core.catalog.build_catalog``)."""
    if on_progress is not None:
        on_progress(0.0, "catalog.build")
    return build_catalog(
        [config.query],
        gh_client=GhClient(),
        registry_client=RegistryClient(config.http, keywords=config.settings.keywords),
        mcp_registry_http=config.http,
        pages=config.pages,
        min_stars=config.min_stars,
        sources=tuple(config.sources),
        on_progress=(
            None
            if on_progress is None
            else lambda fraction, message: on_progress(
                0.25 * fraction, f"catalog.build: {message}"
            )
        ),
    )


def _checks(names: list[str], config: "_Config", on_progress: Any) -> list[dict[str, Any]]:
    """Шаг 2: проверка пакетов (``core.packages.check_many``)."""
    if on_progress is not None:
        on_progress(0.25, "packages.check")
    return check_many(
        names, RegistryClient(config.http, keywords=config.settings.keywords)
    )


def _collect_signals(
    names: list[str], config: "_Config", on_progress: Any
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Шаг 3: сигналы по именам — только настроенные виды.

    Возвращает ``(signals_by_name, kinds_used)``. Вид, чей провайдер не
    настроен, пропускается целиком: коллектор без ключа и так не делает
    запросов, но ``signals_used`` в статистике должен говорить правду —
    «собрано два вида», а не «четыре, из них два пустых».

    Ключи читаются из ``<data_dir>/api_keys.json`` (как в ``cli.signals``), а
    не из параметров задачи: CLI автономен (Железное правило 2).
    """
    kinds = list(dict.fromkeys(config.signal_kinds or []))
    if not kinds:
        return {}, []
    keys = ApiKeysStore(resolve_data_dir(config.settings))
    configured = [kind for kind in kinds if _kind_configured(kind, keys)]
    collected: dict[str, dict[str, Any]] = {name: {} for name in names}

    for index, kind in enumerate(configured):
        if on_progress is not None:
            on_progress(
                0.5 + 0.25 * index / max(1, len(configured)),
                f"signals.collect {kind}",
            )
        for name in names:
            collected[name][kind] = _collect_one(kind, name, keys)

    return collected, configured


def _kind_configured(kind: str, keys: ApiKeysStore) -> bool:
    """Настроен ли провайдер вида (``search_interest`` — хотя бы один источник)."""
    if kind == "search_interest":
        return bool(keys.get("yandex_wordstat") or keys.get("apify"))
    if kind == "funding":
        # У EDGAR не ключ, а User-Agent: он живёт в ``meta`` (P5c).
        return bool(keys.public_meta("edgar").get("user_agent"))
    provider = KIND_TO_PROVIDER.get(kind)
    return bool(provider and keys.get(provider))


def _collect_one(kind: str, name: str, keys: ApiKeysStore) -> dict[str, Any]:
    """Собрать один вид сигнала для одного имени — ``as_dict()`` сигнала."""
    if kind == "jobs":
        credentials = keys.get(KIND_TO_PROVIDER["jobs"]) or {}
        return compute_jobs(
            name,
            app_id=credentials.get("app_id") or None,
            app_key=credentials.get("app_key") or None,
            transport=jobs_transport,
        ).as_dict()
    if kind == "patents":
        credentials = keys.get(KIND_TO_PROVIDER["patents"]) or {}
        return compute_patents(
            name,
            api_key=credentials.get("api_key") or None,
            transport=patents_transport,
        ).as_dict()
    if kind == "funding":
        meta = keys.public_meta(KIND_TO_PROVIDER["funding"])
        return compute_funding(
            name,
            user_agent=meta.get("user_agent") or None,
            transport=funding_transport,
        ).as_dict()
    # search_interest: два источника, как в ``cli.signals`` (P5d).
    wordstat = keys.get("yandex_wordstat") or {}
    apify = keys.get("apify") or {}
    return compute_search_interest(
        name,
        wordstat_token=wordstat.get("token") or None,
        wordstat_client_id=wordstat.get("client_id") or None,
        apify_token=apify.get("api_token") or None,
        apify_actor_id=apify.get("actor_id") or None,
        transport=search_interest_transport,
    ).as_dict()


def _figure(signals: dict[str, Any], kind: str, field: str) -> float | None:
    """Число из собранного сигнала; ``None`` — вид не собирали или он пуст.

    Сигнал с ``error`` (gotcha 66) и ненастроенный источник дают ``None``, а не
    ноль: «ключ есть, API не ответил» не должен понижать Gap Score (§18.3).
    """
    block = signals.get(kind)
    if not isinstance(block, dict) or block.get("error"):
        return None
    value = block.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _search_trend(signals: dict[str, Any]) -> float | None:
    """Тренд search_interest: первый источник без ошибки (как в ``app/web.py``).

    У ``search_interest`` два источника, у каждого свой ``trend_pct``.
    ``configured=False`` и ``error`` пропускаются: их ``None`` — не «интерес
    не растёт», а «данных нет» (§18.3, gotcha 66).
    """
    block = signals.get("search_interest")
    if not isinstance(block, dict):
        return None
    for key in ("wordstat", "google_trends"):
        source = block.get(key)
        if not isinstance(source, dict):
            continue
        if not source.get("configured") or source.get("error"):
            continue
        trend = source.get("trend_pct")
        if isinstance(trend, bool) or not isinstance(trend, (int, float)):
            continue
        return float(trend)
    return None


def _momentum_from_store(names: list[str], settings: Settings) -> dict[str, float | None]:
    """``momentum_30d`` по истории наблюдений из Store (P5f-2).

    База только читается: ``Store`` открывается на ``<data_dir>/gapaudit.db``
    (``resolve_data_dir`` — тот же путь, что у приложения), из неё берётся
    ``get_observations_history`` по каждому имени и считается **та же**
    :func:`compute_momentum`, что в ``/api/matrix``. Поэтому число в Niche
    Finder совпадает с матрицей по смыслу, а не «примерно».

    Три состояния вместо падения (graceful degradation, §18.1):

    * базы нет (CLI запускали без приложения) — ``{}``, momentum не учтён;
    * база битая/несовместимая/занята — строка в stderr и ``{}``: задача
      не должна умирать из-за сигнала, который лишь уточняет score;
    * история короче двух замеров — ``None`` для имени (окно 30 дней пустое:
      «Δ по одному замеру» было бы выдумкой, см. ``core.collectors.momentum``).

    ``names`` — имена, по которым лежат наблюдения (:func:`_registry_name`),
    а не id строк каталога: ``packages.check`` писал наблюдения под именем
    реестра.
    """
    wanted = [name for name in dict.fromkeys(names) if name]
    if not wanted:
        return {}
    store: Any = None
    try:
        db_path = resolve_data_dir(settings) / "gapaudit.db"
        if not db_path.is_file():
            return {}
        # Импорт локальный: ``app/`` — персистентность, и CLI без приложения
        # (чистая машина, скилл) должен работать и без него.
        from app.store import Store

        store = Store(db_path)
        store.__enter__()
        momentums: dict[str, float | None] = {}
        for name in wanted:
            history = store.get_observations_history(name, limit=100)
            momentums[name] = (
                compute_momentum(history).delta_30d if history else None
            )
        return momentums
    except Exception as error:  # noqa: BLE001 - сигнал не должен убивать задачу
        print(
            f"[gapaudit] momentum: история наблюдений недоступна ({error}) — "
            "сигнал не учтён",
            file=sys.stderr,
        )
        return {}
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001 - закрытие битой базы не наша беда
                pass


def _gap_rows(
    names: list[str],
    lookups: list[str],
    checks: list[dict[str, Any]],
    signals_by_name: dict[str, dict[str, Any]],
    sources: list[list[str]],
    momentums: dict[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    """Строки входа Gap Score + поля результата одним проходом.

    ``momentum_30d`` — из ``momentums`` (по имени реестра, :func:`_momentum_from_store`);
    ``None`` — истории нет или она короче двух замеров. Компонент с ``None``
    не входит ни в сумму, ни в знаменатель — score честно считается по
    доступным данным.

    ``names`` — id строк каталога (они же имена в результате), ``lookups`` —
    имена, по которым собирались наблюдения и сигналы (см. :func:`_registry_name`);
    ``registry_name`` в строке — это ``lookup``, чтобы пользователь видел, под
    каким именем пакет искали в реестрах (``owner/repo`` ≠ имя проекта).
    """
    deltas = momentums or {}
    rows: list[dict[str, Any]] = []
    for name, lookup, check, item_sources in zip(names, lookups, checks, sources):
        signals = signals_by_name.get(lookup, {})
        rows.append(
            {
                "name": name,
                "registry_name": lookup,
                "adoption_week": check.get("week"),
                "momentum_30d": deltas.get(lookup),
                "flag": check.get("flag"),
                "funding_total": _figure(signals, "funding", "total"),
                "patents_impulse_pct": _figure(signals, "patents", "impulse_pct"),
                "jobs_total": _figure(signals, "jobs", "total"),
                "search_interest_trend": _search_trend(signals),
                "verdict": check.get("verdict"),
                "sources": item_sources,
            }
        )
    return rows


def _ranked_items(
    rows: list[dict[str, Any]], scores: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    """Слить строки со score, отсортировать по gap_score DESC и усечь ``limit``.

    Пакеты без score (``None``: ни одного компонента) уходят в конец: «нет
    данных» не должно стоять выше измеренной ниши. Сортировка вторичная по
    имени — ответ детерминирован при равных score.
    """
    items: list[dict[str, Any]] = []
    for row in rows:
        score = scores.get(str(row.get("name") or ""))
        items.append(
            {
                **row,
                "gap_score": None if score is None else score.score,
                "components_available": (
                    None if score is None else score.components_available
                ),
            }
        )
    items.sort(
        key=lambda item: (
            item["gap_score"] is None,
            -(item["gap_score"] or 0.0),
            # При равном score выше идёт измеренная ниша: score на одной
            # компоненте (только «MCP нет») у пакета без adoption — это «100
            # из 100 по одному признаку», и ставить такие строки над
            # измеренными значило бы прятать данные внизу списка.
            item["adoption_week"] is None,
            str(item["name"]),
        )
    )
    return items[: max(1, int(limit))]


# --- команда ------------------------------------------------------------------


class _Config:
    """Всё, что нужно шагам конвейера: аргументы, настройки, HTTP-клиент."""

    def __init__(self, args: argparse.Namespace, settings: Settings, http: Any) -> None:
        self.query: str = str(args.query or "").strip()
        self.min_stars = int(args.min_stars)
        self.pages = int(args.pages)
        self.limit = int(args.limit)
        self.sources: list[str] = list(args.sources)
        self.signal_kinds: list[str] = list(args.signal_kinds or [])
        self.settings = settings
        self.http = http


def _cmd_run(args: argparse.Namespace, argv: list[str] | None = None) -> int:
    """Выполнить конвейер Niche Finder; код выхода 0/2.

    ``argv`` — аргументы командной строки: по ним решается, включён ли
    протокол (``--job-json``). Тесты зовут ``main([...])`` in-process, и
    ``sys.argv`` в этот момент — аргументы pytest, а не CLI.
    """
    settings, http = load_runtime(args)
    config = _Config(args, settings, http)

    bound_error = _check_bounds(args)
    if bound_error:
        print(f"[gapaudit] {bound_error}", file=sys.stderr)
        if job_json_requested(argv):
            emit_error(bound_error)
        return 2
    if not config.query:
        print("[gapaudit] query: не может быть пустой", file=sys.stderr)
        if job_json_requested(argv):
            emit_error("query: не может быть пустой")
        return 2

    on_progress = build_job_emitter() if job_json_requested(argv) else None

    # 1. каталог
    catalog = _catalog(config, on_progress)
    items = catalog.get("items") or []
    # ``found`` — весь каталог (сколько нашлось), ``checked`` — сколько реально
    # проверено (CHECK_LIMIT). Разницу показывает экран: «Проверено 50 из 139».
    found = len(items)
    # Проверяем не весь каталог, а первые CHECK_LIMIT строк по звёздам: каждая
    # строка — до трёх запросов к реестрам, а 130 строк упираются в их лимиты
    # (adoption не приходит вовсе, все score равны 100). Отбор — до проверки,
    # потому что дешевле не спрашивать, чем отбрасывать ответы.
    ranked_rows = sorted(
        (row for row in items if row.get("id")),
        key=lambda row: -(row.get("stars") or 0),
    )[:CHECK_LIMIT]
    names = [str(row["id"]) for row in ranked_rows]
    sources_by_name = {
        str(row["id"]): [str(source) for source in (row.get("sources") or [])]
        for row in ranked_rows
    }
    # Имена для реестров/поиска сигналов: у GitHub-строки id — owner/repo,
    # а PyPI/npm знают проект под именем репозитория (см. _registry_name).
    lookups = [_registry_name(name) for name in names]

    # 2. проверка пакетов
    checks = _checks(lookups, config, on_progress) if names else []

    # 2b. momentum из истории наблюдений (P5f-2): только чтение Store, при
    # отсутствии базы — пусто, и компонент честно не участвует в score.
    momentums = _momentum_from_store(lookups, settings) if names else {}

    # 3. сигналы (ключ — имя для поиска, чтобы jobs/patents искали по проекту)
    signals_by_name, kinds_used = (
        _collect_signals(lookups, config, on_progress) if names else ({}, [])
    )

    # 4. Gap Score
    if on_progress is not None:
        on_progress(0.75, "gap_score")
    weights = default_weights_from_settings(settings)
    rows = _gap_rows(
        names,
        lookups,
        checks,
        signals_by_name,
        [sources_by_name.get(name, []) for name in names],
        momentums,
    )
    scores = compute_gap_scores_batch(rows, weights=weights) if rows else {}
    ranked = _ranked_items(rows, scores, config.limit)

    result = {
        "query": config.query,
        "items": ranked,
        "stats": {
            "total": len(ranked),
            "found": found,
            "checked": len(names),
            "with_gap": sum(1 for item in ranked if item["gap_score"] is not None),
            "signals_used": kinds_used,
        },
    }
    write_json(args.json_out or DEFAULT_JSON, result)

    _print_summary(result)
    if on_progress is not None:
        on_progress(1.0, "done")
    return 0


def _print_summary(result: dict[str, Any]) -> None:
    """Человекочитаемый итог: top-N по gap_score (под ``--job-json`` — в stderr)."""
    items = result.get("items") or []
    stats = result.get("stats") or {}
    if not items:
        print(
            f"niche: по фразе «{result.get('query')}» ничего не найдено — "
            "проверьте фразу или увеличьте --pages"
        )
        return
    rows = [
        [
            str(item.get("name")),
            "—" if item.get("gap_score") is None else f"{item['gap_score']:.0f}",
            "есть" if item.get("flag") else "нет",
            "—" if item.get("adoption_week") is None else str(item["adoption_week"]),
            str(item.get("verdict") or "—"),
        ]
        for item in items[:TOP_ROWS]
    ]
    print_table(
        ["пакет", "gap", "MCP", "adoption/нед", "вердикт"],
        rows,
        aligns=["l", "r", "l", "r", "l"],
    )
    used = ", ".join(stats.get("signals_used") or []) or "нет (ключи не настроены)"
    checked = stats.get("checked")
    checked_note = (
        f", проверено {checked}" if checked is not None and checked != stats.get("found") else ""
    )
    print(
        f"niche: найдено {stats.get('found', len(items))}{checked_note}, "
        f"в результате {stats.get('total', len(items))}, "
        f"со score {stats.get('with_gap', 0)}; сигналы: {used}"
    )


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; под ``--job-json`` — протокол JSON-lines вокруг тела.

    ``hello`` → ``progress`` (шаги конвейера) → ``done``; ошибка тела —
    ``{"type": "error"}`` и код 2 (шаг 3d-a). ``argparse`` на неверном
    аргументе завершает процесс кодом 2 сам (usage уходит в stderr и попадает
    в лог задачи).
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(raw)

    if not job_json_requested(argv):
        return _cmd_run(args, argv)
    with job_json_mode():
        try:
            code = _cmd_run(args, argv)
        except Exception as error:  # noqa: BLE001 - задача обязана отчитаться ошибкой
            emit_error(str(error))
            return 2
        if code == 0:
            emit_done()
        return code


if __name__ == "__main__":
    sys.exit(main())
