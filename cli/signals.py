"""CLI-обёртка для сигналов (шаг P5a, расширение P5b/P5c/P5d; DEVPLAN §7/§18.3).

Запуск::

    python -m cli.signals collect --package requests --kind jobs
                                  --job-json --json-out <file>

Подкоманда ``collect`` собирает сигналы по одному пакету. Ключи читаются из
``<data_dir>/api_keys.json`` (``core.api_keys.ApiKeysStore``) — не из
параметров: CLI автономен (Железное правило 2), каталог данных
определяется общефлагом ``--data-dir`` как у шести других скриптов.

``--kind`` повторяется (``action="append"``); с P5d допустимы ``jobs``,
``patents``, ``funding`` и ``search_interest`` (``choices``). Повтор одного
вида — дедупликация, не ошибка: задача «jobs дважды» означает «собери
jobs», а не «умри». Без ``--kind`` собираются все виды :data:`SIGNAL_KINDS`.

Два вида читают не тот канал, что остальные:

* ``funding`` — провайдер ``edgar`` без ключа: SEC требует **User-Agent с
  email**, и он лежит в ``meta`` (P5c). Читается
  :meth:`ApiKeysStore.public_meta` — точный канал только для ``meta``;
  ``get`` тут тоже вернул бы значение (он домешивает ``meta``), но
  публичный канал не должен расшифровывать ``encrypted_value`` зря;
* ``search_interest`` — **два источника** (P5d): Wordstat
  (``get("yandex_wordstat")``) и Google Trends через Apify
  (``get("apify")``). Официальный Google Trends (``google_trends``,
  alpha по заявке) в P5d не реализован: если ключ есть, а Apify нет —
  источник получает ``configured=False`` и честное сообщение в ``error``
  (см. :data:`GOOGLE_TRENDS_OFFICIAL_NOT_IMPLEMENTED`), чтобы карточка
  показала причину, а не «не настроен» на введённый ключ.

Протокол задачи — как у остальных CLI: под ``--job-json`` hello →
progress (по одной строке на регион/источник) → done; таблицы и итоги в
stderr (их читает лог задачи), результат — в ``--json-out``. Ошибка тела —
``{"type": "error"}`` и код 2.

Без ключа провайдера это НЕ ошибка: ``configured=False`` в результате,
код 0 — graceful degradation (§18.1): сигнал «не настроен», приложение
работает.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

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
from core.collectors.funding import compute_funding, default_transport as funding_transport
from core.collectors.jobs import compute_jobs, default_transport
from core.collectors.patents import compute_patents, default_post_transport
from core.collectors.search_interest import (
    SearchInterestSource,
    compute_search_interest,
    default_post_transport as search_interest_transport,
)

__all__ = ["GOOGLE_TRENDS_OFFICIAL_NOT_IMPLEMENTED", "SIGNAL_KINDS", "main"]

#: Виды сигналов, которые умеет собирать ``collect`` (P5a: jobs; P5b:
#: +patents; P5c: +funding; P5d: +search_interest). Тот же набор проверяет
#: ``POST /api/signals/collect`` (app/web.py) — два слоя не должны разойтись
#: (gotcha 54, тест «согласие слоёв»).
SIGNAL_KINDS: tuple[str, ...] = ("jobs", "patents", "funding", "search_interest")

#: Вид сигнала -> провайдер ключей (core.api_keys.PROVIDERS). У
#: ``search_interest`` источников два, и здесь стоит ОСНОВНЫЙ (Wordstat —
#: бесплатный, по регионам РФ); второй (``apify``) и задел под
#: ``google_trends`` читаются в :func:`_collect_search_interest`. Таблица
#: обязана совпадать с ``app.web.SIGNAL_KIND_PROVIDERS`` (тот же тест).
KIND_TO_PROVIDER: dict[str, str] = {
    "jobs": "adzuna",
    "patents": "patentsview",
    "funding": "edgar",
    "search_interest": "yandex_wordstat",
}

#: Сообщение источника Google Trends, когда настроен официальный alpha-ключ,
#: а Apify нет: коллектора для официального API в P5d нет (§14, вариант A —
#: по заявке; реализован вариант B). Текст уезжает в карточку как ``error``
#: источника, поэтому он человекочитаемый, а не код.
GOOGLE_TRENDS_OFFICIAL_NOT_IMPLEMENTED = (
    "google_trends official alpha not implemented yet (только Apify)"
)

#: Куда писать результат без ``--json-out`` (как у legacy-скриптов: raw/).
DEFAULT_JSON = "raw/signals.json"


#: Общие флаги build_argparser, которым argparse ждёт значение отдельным
#: токеном: следующий за ними токен — значение, а не позиционный аргумент.
_VALUE_FLAGS = frozenset(
    {"--data-dir", "--settings", "--cache-path", "--keyword", "--json-out", "--package"}
)


def _extract_command(argv: list[str]) -> tuple[str, list[str]]:
    """Вытащить подкоманду из argv; вернуть ``(команда, остальное для argparse)``.

    Подкоманда — первый ПОЗИЦИОННЫЙ токен (не флаг и не значение флага),
    где бы он ни стоял: и ``cli.signals collect --package X --job-json``,
    и порядок ``_default_runner`` ``--job-json --json-out=X collect
    --package X`` обязаны работать. Значение флага ``--package collect``
    подкомандой не считается — иначе пакет с именем «collect» съедал бы
    диспетчеризацию.
    """
    rest: list[str] = []
    command = "collect"
    expect_value = False
    found = False
    for token in argv:
        if expect_value:
            expect_value = False
            rest.append(token)
            continue
        if token in _VALUE_FLAGS:
            expect_value = True
            rest.append(token)
            continue
        if token.startswith("-"):
            rest.append(token)
            continue
        if not found:
            command = token
            found = True
            continue
        rest.append(token)
    return command, rest


def _main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Подкоманда диспетчеризуется вручную: argparse-сабпарсеры не наследуют
    # общие флаги build_argparser (--job-json/--json-out после "collect" были
    # бы "unrecognized arguments"), а править cli/_common.py шаг не разрешает.
    command, rest = _extract_command(raw)
    if command != "collect":
        print(
            f"[gapaudit] неизвестная команда: {command!r} (доступна: collect)",
            file=sys.stderr,
        )
        return 2

    parser = build_argparser(
        prog="signals.py",
        description=(
            "Сбор сигналов ниш по пакету (jobs — Adzuna, patents — PatentsView, "
            "funding — SEC EDGAR, search_interest — Yandex Wordstat + Apify)"
        ),
    )
    parser.add_argument(
        "--package",
        required=True,
        metavar="ИМЯ",
        help="пакет (для jobs — ключевое слово поиска вакансий)",
    )
    parser.add_argument(
        "--kind",
        action="append",
        choices=SIGNAL_KINDS,
        default=None,
        help=f"вид сигнала; можно повторять; по умолчанию: {', '.join(SIGNAL_KINDS)}",
    )
    args = parser.parse_args(rest)

    try:
        settings, _client = load_runtime(args)
    except ValueError as error:
        print(f"[gapaudit] {error}", file=sys.stderr)
        return 2

    package = str(args.package or "").strip()
    if not package:
        print("[gapaudit] package: не может быть пустым", file=sys.stderr)
        return 2

    # Дедупликация с сохранением порядка: --kind jobs --kind jobs = один jobs.
    kinds = list(dict.fromkeys(args.kind or list(SIGNAL_KINDS)))

    # Прогресс по одной строке на страну — в протокол задачи (SSE до UI);
    # в автономном режиме on_progress=None, чтобы не мусорить в stdout.
    on_progress = build_job_emitter() if job_json_requested(argv) else None

    try:
        keys = ApiKeysStore(settings.data_dir)
        signals: dict[str, dict] = {}
        # Какой вид собирается — решает choices argparse (--kind), здесь
        # ветка на вид (новый вид = одна строка в SIGNAL_KINDS + эта).
        for kind in kinds:
            if kind == "jobs":
                signals["jobs"] = _collect_jobs(keys, package, on_progress)
            elif kind == "patents":
                signals["patents"] = _collect_patents(keys, package, on_progress)
            elif kind == "funding":
                signals["funding"] = _collect_funding(keys, package, on_progress)
            elif kind == "search_interest":
                signals["search_interest"] = _collect_search_interest(
                    keys, package, on_progress
                )
    except Exception as error:  # noqa: BLE001 - тело CLI обязано отчитаться ошибкой
        print(f"[gapaudit] {error}", file=sys.stderr)
        if job_json_requested(argv):
            emit_error(str(error))
        return 2

    payload = {"package": package, "signals": signals}
    write_json(args.json_out or DEFAULT_JSON, payload)

    _print_summary(signals)
    return 0


def _collect_jobs(
    keys: ApiKeysStore, package: str, on_progress: object
) -> dict:
    """Собрать ``jobs`` через ``core.collectors.jobs``; ключа нет — ``configured=False``.

    ``ApiKeysStore.get`` возвращает ``None``, когда провайдер не настроен
    или поля неполные — оба случая коллектор получает как ``app_id=None``:
    «половина учётных данных хуже их отсутствия» (core/api_keys.py).
    """
    credentials = keys.get(KIND_TO_PROVIDER["jobs"]) or {}
    signal = compute_jobs(
        package,
        app_id=credentials.get("app_id") or None,
        app_key=credentials.get("app_key") or None,
        transport=default_transport,
        on_progress=on_progress,  # type: ignore[arg-type]
    )
    return signal.as_dict()


def _collect_patents(
    keys: ApiKeysStore, package: str, on_progress: object
) -> dict:
    """Собрать ``patents`` через ``core.collectors.patents``; ключа нет — ``configured=False``.

    ``ApiKeysStore.get`` вернёт ``None`` и для неполного ключа — коллектору
    уходит ``api_key=None`` («не настроен», запросов нет, код 0; P5b).
    """
    credentials = keys.get(KIND_TO_PROVIDER["patents"]) or {}
    signal = compute_patents(
        package,
        api_key=credentials.get("api_key") or None,
        transport=default_post_transport,
        on_progress=on_progress,  # type: ignore[arg-type]
    )
    return signal.as_dict()


def _collect_funding(
    keys: ApiKeysStore, package: str, on_progress: object
) -> dict:
    """Собрать ``funding`` через ``core.collectors.funding``; нет User-Agent — ``configured=False``.

    User-Agent EDGAR НЕ секрет и живёт в ``meta`` провайдера, поэтому
    читается :meth:`ApiKeysStore.public_meta`, а не :meth:`ApiKeysStore.get`:
    у ``edgar`` ``encrypted_value`` пуст, и ``get("edgar")`` вернул бы ``{}``
    — сигнал остался бы «не настроен» даже с введённым User-Agent (P5c, §0.7).
    """
    meta = keys.public_meta(KIND_TO_PROVIDER["funding"])
    signal = compute_funding(
        package,
        user_agent=meta.get("user_agent") or None,
        transport=funding_transport,
        on_progress=on_progress,  # type: ignore[arg-type]
    )
    return signal.as_dict()


def _collect_search_interest(
    keys: ApiKeysStore, package: str, on_progress: object
) -> dict:
    """Собрать ``search_interest``: Wordstat и Google Trends (Apify) сразу.

    Два источника — две пары учётных данных (P5d)::

        yandex_wordstat -> {"token", "client_id"}   (секрет + meta)
        apify           -> {"api_token", "actor_id"} (секрет + meta)

    ``ApiKeysStore.get`` домешивает несекретные поля из ``meta`` (P5c,
    §0.4), поэтому оба набора приходят целиком: ``client_id`` и
    ``actor_id`` не нужно читать отдельно через ``public_meta``. Неполный
    набор ``get`` возвращает как ``None`` — источник получает пустые
    значения и остаётся ``configured=False`` («половина учётных данных
    хуже их отсутствия»).

    Официальный Google Trends (``google_trends``, alpha по заявке)
    коллектора в P5d не имеет: если его ключ введён, а Apify нет —
    источник Google Trends получает ``configured=False`` и
    ``error`` с объяснением (см. :data:`GOOGLE_TRENDS_OFFICIAL_NOT_IMPLEMENTED`):
    «не настроен» на введённый ключ был бы ложью, а тихий пустой блок —
    потерей причины. Из этого сообщения ``app/web.py`` выводит статус
    ``ok`` (есть что показать), поэтому причина видна в карточке.
    """
    wordstat = keys.get("yandex_wordstat") or {}
    apify = keys.get("apify") or {}
    official = keys.get("google_trends") or {}
    signal = compute_search_interest(
        package,
        wordstat_token=wordstat.get("token") or None,
        wordstat_client_id=wordstat.get("client_id") or None,
        apify_token=apify.get("api_token") or None,
        apify_actor_id=apify.get("actor_id") or None,
        transport=search_interest_transport,
        on_progress=on_progress,  # type: ignore[arg-type]
    )
    if official and not apify:
        # Ключ официального alpha есть, а Apify не настроен: не выдаём
        # «не настроен» — говорим, что именно не реализовано.
        signal = replace(
            signal,
            google_trends=SearchInterestSource(
                source="google_trends",
                points=(),
                total=None,
                trend_pct=None,
                configured=False,
                error=GOOGLE_TRENDS_OFFICIAL_NOT_IMPLEMENTED,
            ),
        )
    return signal.as_dict()


def _print_summary(signals: dict[str, dict]) -> None:
    """Человекочитаемый итог в stdout (под --job-json он уходит в stderr).

    Секции независимы: ``--kind patents`` без jobs печатает только patents,
    и наоборот (P5b: видов сигнала несколько, ранний ``return`` одного вида
    не должен съедать итог другого).
    """
    jobs = signals.get("jobs")
    if jobs is not None:
        if not jobs.get("configured"):
            print("jobs: ключ Adzuna не настроен — см. Настройки → API-ключи")
        elif jobs.get("error"):
            print(f"jobs: ошибка — {jobs['error']}")
        else:
            rows = [
                [row["label"], str(row["count"])]
                for row in (jobs.get("by_country") or [])
            ]
            if rows:
                print_table(["страна", "вакансии"], rows, aligns=["l", "r"])
            print(f"jobs: собрано, всего вакансий: {jobs['total']}")

    patents = signals.get("patents")
    if patents is not None:
        if not patents.get("configured"):
            print("patents: ключ PatentsView не настроен — см. Настройки → API-ключи")
        elif patents.get("error"):
            print(f"patents: ошибка — {patents['error']}")
        else:
            rows = [
                [str(row["year"]), str(row["count"])]
                for row in (patents.get("by_year") or [])
            ]
            if rows:
                print_table(["год", "патентов"], rows, aligns=["l", "r"])
            print(f"patents: собрано, всего патентов: {patents['total']}")
            impulse = patents.get("impulse_pct")
            if impulse is not None:
                sign = "+" if impulse > 0 else ""
                print(f"patents: импульс {sign}{impulse:.1f}% за 3 года")

    funding = signals.get("funding")
    if funding is not None:
        if not funding.get("configured"):
            print("funding: User-Agent SEC EDGAR не настроен — см. Настройки → API-ключи")
        elif funding.get("error"):
            print(f"funding: ошибка — {funding['error']}")
        else:
            rows = [
                [f"Q{row['quarter']} {row['year']}", str(row["count"])]
                for row in (funding.get("by_quarter") or [])
            ]
            if rows:
                print_table(["квартал", "раундов"], rows, aligns=["l", "r"])
            print(f"funding: собрано, всего filing'ов формы D: {funding['total']}")
            trend = funding.get("trend_pct")
            if trend is not None:
                sign = "+" if trend > 0 else ""
                print(f"funding: тренд {sign}{trend:.1f}% за 4 квартала")

    search_interest = signals.get("search_interest")
    if search_interest is not None:
        # Два источника в одной секции: каждый со своим состоянием, чтобы
        # «не настроен» одного не прятал данные другого (P5d).
        _print_search_interest_source("wordstat", search_interest.get("wordstat"))
        _print_search_interest_source(
            "google_trends", search_interest.get("google_trends")
        )


def _print_search_interest_source(name: str, source: object) -> None:
    """Итог одного источника search_interest (Wordstat или Google Trends).

    ``None`` — источник не запрашивали (ручное конструирование сигнала);
    ``configured=False`` — «не настроен», но с ``error`` — причина;
    ``configured=True`` — таблица регионов (top-5, по убыванию интереса)
    и тренд, если источник его даёт (у Wordstat тренда нет: только срез
    за 30 дней).
    """
    if not isinstance(source, dict):
        return
    label = "Yandex Wordstat" if name == "wordstat" else "Google Trends"
    if not source.get("configured"):
        if source.get("error"):
            print(f"{name}: не реализовано — {source['error']}")
        else:
            print(f"{name}: ключ {label} не настроен — см. Настройки → API-ключи")
        return
    if source.get("error"):
        print(f"{name}: ошибка — {source['error']}")
        return
    rows = [
        [point["region"], str(point["value"]) if point["value"] is not None else "—"]
        for point in (source.get("points") or [])[:5]
    ]
    if rows:
        print_table([f"{label}: регион", "интерес"], rows, aligns=["l", "r"])
    total = source.get("total")
    if total is not None:
        print(f"{name}: собрано, суммарный интерес: {total}")
    trend = source.get("trend_pct")
    if trend is not None:
        sign = "+" if trend > 0 else ""
        print(f"{name}: тренд {sign}{trend:.1f}% за 12 мес")


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; под ``--job-json`` — протокол JSON-lines вокруг тела.

    Таблицы при этом уходят в stderr (лог задачи), stdout остаётся чистым
    протоколом; ошибка тела — ``{"type": "error"}`` и код 2 (шаг 3d-a).
    ``argparse`` при неверном ``--kind`` завершает процесс кодом 2 через
    ``SystemExit`` — тот же код, что у задачи-ошибки, только без строки
    ``error`` (её добавит лог: usage уходит в stderr).
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
