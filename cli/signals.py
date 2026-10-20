"""CLI-обёртка для сигналов (шаг P5a, расширение P5b/P5c; DEVPLAN §7/§18.3).

Запуск::

    python -m cli.signals collect --package requests --kind jobs
                                  --job-json --json-out <file>

Подкоманда ``collect`` собирает сигналы по одному пакету. Ключи читаются из
``<data_dir>/api_keys.json`` (``core.api_keys.ApiKeysStore``) — не из
параметров: CLI автономен (Железное правило 2), каталог данных
определяется общефлагом ``--data-dir`` как у шести других скриптов.

``--kind`` повторяется (``action="append"``); с P5c допустимы ``jobs``,
``patents`` и ``funding`` (``choices``): будущий ``search_interest`` (P5d)
добавляется одной строкой в :data:`SIGNAL_KINDS` (и одной строкой в
:data:`KIND_TO_PROVIDER`). Повтор одного вида — дедупликация, не ошибка:
задача «jobs дважды» означает «собери jobs», а не «умри». Без ``--kind``
собираются все виды :data:`SIGNAL_KINDS`.

У провайдера ``edgar`` (funding) нет ключа — SEC требует **User-Agent с
email**, и он лежит в ``meta``, а не в ``encrypted_value`` (P5c). Поэтому
для funding читается :meth:`ApiKeysStore.public_meta`, а не
:meth:`ApiKeysStore.get`: последний вернул бы ``{}`` (у ``edgar``
``encrypted_value`` пуст), и сигнал навсегда остался бы «не настроен».

Протокол задачи — как у остальных CLI: под ``--job-json`` hello →
progress (по одной строке на страну/год) → done; таблицы и итоги в stderr
(их читает лог задачи), результат — в ``--json-out``. Ошибка тела —
``{"type": "error"}`` и код 2.

Без ключа провайдера это НЕ ошибка: ``configured=False`` в результате,
код 0 — graceful degradation (§18.1): сигнал «не настроен», приложение
работает.
"""

from __future__ import annotations

import argparse
import sys

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

__all__ = ["SIGNAL_KINDS", "main"]

#: Виды сигналов, которые умеет собирать ``collect`` (P5a: jobs; P5b:
#: +patents; P5c: +funding). Тот же набор проверяет ``POST
#: /api/signals/collect`` (app/web.py) — два слоя не должны разойтись
#: (gotcha 54, тест «согласие слоёв»).
SIGNAL_KINDS: tuple[str, ...] = ("jobs", "patents", "funding")

#: Вид сигнала -> провайдер ключей (core.api_keys.PROVIDERS).
KIND_TO_PROVIDER: dict[str, str] = {
    "jobs": "adzuna",
    "patents": "patentsview",
    "funding": "edgar",
}

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
            "funding — SEC EDGAR)"
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
