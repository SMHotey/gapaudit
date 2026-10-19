"""Потоковый обход каталога реестра MCP — CLI-обёртка (шаг 1f).

Порт ``legacy/catalog_scan.py``: тот же обход по курсору одним проходом
(``core.scan``), те же коды выхода (0 — каталог пройден, 1 — обрыв, 130 — Ctrl+C),
но чекпоинт и итоговый JSON пишутся в каталог данных, а прогресс идёт в stderr.

Запуск (обход занимает десятки минут — с ``PYTHONUNBUFFERED=1`` и в лог):
  python -m cli.catalog_scan --limit 100
  python -m cli.catalog_scan --checkpoint ck.json --json-out out.json --no-sleep
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from cli._common import (
    build_argparser,
    emit_done,
    emit_error,
    job_json_mode,
    job_json_requested,
    load_runtime,
    print_table,
    write_json,
)
from core.scan import DEFAULT_BASE_URL, DEFAULT_CLASSES, ScanConfig, ScanState, scan
from core.mcp_registry import DEFAULT_LIMIT
from core.settings import resolve_data_dir

__all__ = ["main"]

#: Имена файлов по умолчанию — в каталоге данных (не в cwd, не в ../data).
CHECKPOINT_NAME = "registry_scan_ckpt.json"
JSON_NAME = "registry_scan.json"


def _main(argv: list[str] | None = None) -> int:
    parser = build_argparser(
        prog="catalog_scan.py",
        description="Потоковый обход каталога реестра MCP: размер, классы задач, тип поставки",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="шаблон URL каталога с пагинацией ({limit}; фиксируется в чекпоинте)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="серверов на страницу")
    parser.add_argument("--max-pages", type=int, default=400, help="предохранитель от бесконечного обхода")
    parser.add_argument("--checkpoint-every", type=int, default=25, help="как часто писать чекпоинт (страниц)")
    parser.add_argument("--checkpoint", metavar="FILE",
                        help="файл чекпоинта (по умолчанию <data_dir>/registry_scan_ckpt.json)")
    parser.add_argument("--hide-zero", action="store_true", help="не печатать классы с count=0")
    parser.add_argument("--no-sleep", action="store_true", help="спать 0 с между страницами (для тестов)")
    args = parser.parse_args(argv)

    try:
        settings, http = load_runtime(args)
        data_dir = resolve_data_dir(settings)
    except ValueError as error:
        print(f"[gapaudit] {error}", file=sys.stderr)
        return 2

    checkpoint_path = args.checkpoint or str(Path(data_dir) / CHECKPOINT_NAME)
    json_path = args.json_out or str(Path(data_dir) / JSON_NAME)

    config = ScanConfig(
        base_url=args.base_url,
        classes=DEFAULT_CLASSES,
        limit=args.limit,
        checkpoint_every=args.checkpoint_every,
        max_pages=args.max_pages,
        sleep_between_pages=0.0 if args.no_sleep else 0.05,
    )
    state = _load_checkpoint(checkpoint_path)
    if state is None:
        state = ScanState(base_url=config.base_url)

    started = time.monotonic()
    try:
        state = scan(
            http,
            config=config,
            state=state,
            on_progress=lambda current: _print_progress(current, started),
            on_checkpoint=lambda current: write_json(checkpoint_path, current.as_checkpoint()),
            cancel=None,
        )
    except KeyboardInterrupt:
        # Ctrl+C: чекпоинт сохранён — следующий запуск продолжит с той же страницы.
        write_json(checkpoint_path, state.as_checkpoint())
        print("\nпрервано пользователем — чекпоинт сохранён", file=sys.stderr)
        return 130

    # След для продолжения остаётся и после успешного обхода — как в legacy.
    write_json(checkpoint_path, state.as_checkpoint())
    write_json(json_path, state.summary())

    _print_classes(state, hide_zero=args.hide_zero)
    if state.finished:
        print("КУРСОР КОНЧИЛСЯ — каталог пройден полностью")
    else:
        print(f"СТОП: обрыв на стр.{state.pages} — курсор сохранён в {checkpoint_path}")
    print(f"ИТОГ: записей={state.entries}, уникальных имён={len(state.names)}, "
          f"неймспейсов={len(state.namespaces)}")
    print(f"СОХРАНЕНО: {json_path}")
    if http.cache is not None:
        http.cache.save()
    return 0 if state.finished else 1


def _load_checkpoint(path: str) -> ScanState | None:
    """Чекпоинт с прошлого запуска; отсутствие или мусор — свежий обход."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    state = ScanState.from_checkpoint(payload if isinstance(payload, dict) else None)
    print(f"резюме с чекпоинта: стр.{state.pages}, записей {state.entries}")
    return state


def _print_progress(state: ScanState, started: float) -> None:
    """Строка прогресса каждые 25 страниц — как в legacy."""
    if not state.pages or state.pages % 25:
        return
    rate = state.entries / max(time.monotonic() - started, 1e-6)
    print(f"[gapaudit] стр.{state.pages} записей={state.entries} "
          f"имён={len(state.names)} скорость={rate:.0f}зап/с", flush=True)


def _print_classes(state: ScanState, *, hide_zero: bool) -> None:
    """Плотность классов задач: печать всех ярлыков, как в итоговом JSON legacy."""
    rows = [
        [label, str(count)]
        for label, count in sorted(state.counts.items(), key=lambda item: -item[1])
        if not (hide_zero and count == 0)
    ]
    if not rows:
        return
    print("\n=== КЛАССЫ ЗАДАЧ (по описаниям серверов) ===")
    print_table(["класс", "записей"], rows, aligns=["l", "r"])


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; под ``--job-json`` — протокол JSON-lines вокруг тела.

    Таблицы при этом уходят в stderr (лог задачи), stdout остаётся чистым
    протоколом; ошибка тела — ``{"type": "error"}`` и код 2 (шаг 3d-a).
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
