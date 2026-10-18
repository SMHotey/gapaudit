"""Сравнение двух списков наблюдений — чистые функции (шаг 1c).

Модуль ничего не знает ни о сети, ни о БД, ни о файлах: на вход приходят два списка
наблюдений (их пишет шаг 2 в таблицу ``observations``), на выходе — список событий
для UI и CLI (шаг 1e) и для таблицы ``events`` (шаг 2).

Порт legacy ``verdict()``/``days_since()``/``diff_runs()`` из ``check_predecessors.py``
с двумя осознанными изменениями:

* контракт наблюдения берётся из схемы БД (``version``, ``released_at``,
  ``adoption_week``, ``flag``), а не из ``ver``/``date``/``week``/``mcp`` словаря CLI —
  ключи ``name``/``version`` совпадают с ``observations``, поэтому UI не пересобирает
  словари по дороге;
* событие — словарь с машиночитаемым ``kind`` (``release``, ``flag_appeared``,
  ``adoption_change``, ``verdict_change``, ``new``, ``missing``), а не кортеж с русской
  подписью: подпись живёт в ``detail``.

Пороги — в :class:`VerdictConfig`: они одинаковы для ``check()`` (модуль
:mod:`core.packages`) и для сравнения прогонов, поэтому «ЖИВО» в таблице и «ЖИВО»
в событии значат одно и то же.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "VERDICT_ALIVE",
    "VERDICT_ABSENT",
    "VERDICT_DEAD",
    "VERDICT_PRESENT",
    "VERDICT_STALE",
    "VERDICT_WEAK",
    "VerdictConfig",
    "apply_stale",
    "days_since",
    "diff_observations",
    "verdict",
]

VERDICT_ABSENT = "ОТСУТСТВУЕТ"
VERDICT_DEAD = "МЁРТВО"
VERDICT_WEAK = "СЛАБО"
VERDICT_ALIVE = "ЖИВО"
VERDICT_STALE = "ЗАСТОЙ"
VERDICT_PRESENT = "ЕСТЬ (adoption не измерен)"

#: Подписи событий — те же, что печатал legacy ``diff_runs()``.
DETAIL_NEW = "не было в прошлом прогоне"
DETAIL_MISSING = "нет в текущем прогоне"
DETAIL_FLAG_APPEARED = "вендор закрывает нишу — пересмотреть решение об интеграции"
DETAIL_FLAG_REMOVED = "проверить вручную"


@dataclass(frozen=True)
class VerdictConfig:
    """Пороги вердикта и чувствительности сравнения (значения legacy).

    ``alive_weekly``/``alive_monthly`` — «живо» и выше; ``dead_weekly``/``dead_monthly`` —
    «мертво» строго ниже; ``stale_days`` — сколько дней без релиза превращают «ЖИВО»
    в «ЗАСТОЙ»; ``adoption_delta_pct`` — минимальное изменение недельных скачиваний
    (по модулю), о котором сообщаем событием.
    """

    alive_weekly: int = 100
    alive_monthly: int = 400
    dead_weekly: int = 10
    dead_monthly: int = 40
    stale_days: int = 540
    adoption_delta_pct: float = 25.0


def days_since(date_str: str | None) -> int | None:
    """Сколько дней назад была дата ``YYYY-MM-DD``; ``None`` на пустое и мусор.

    Отличается от :func:`core.github.age_days` предметом: там ISO-8601 с таймзоной
    (``2024-05-01T10:00:00Z``), здесь — дата без таймзоны, как её отдают PyPI и npm.
    Расчёт по календарю машины, как в legacy, — иначе «ЗАСТОЙ» может разойтись на сутки.
    Будущая дата даёт ``0``: контракт обещает неотрицательное число, а «релиз из будущего»
    не должен выглядеть старее сегодняшнего.
    """
    if not date_str:
        return None
    try:
        parsed = time.strptime(str(date_str)[:10], "%Y-%m-%d")
        return max(0, int((time.time() - time.mktime(parsed)) / 86400))
    except Exception:
        return None


def verdict(
    has_pypi: bool,
    has_npm: bool,
    week: int | None,
    month: int | None,
    *,
    cfg: VerdictConfig,
) -> str:
    """Русский вердикт по факту публикации и скачиваниям — порт legacy ``verdict()``.

    Порядок проверок как в legacy: сначала «есть ли пакет вообще», потом недельные
    скачивания, потом месячные, и только в конце «опубликовано, adoption не измерен».
    «ЗАСТОЙ» здесь не выставляется: он требует ещё и даты релиза, поэтому накладывается
    отдельно — :func:`apply_stale`.
    """
    if not has_pypi and not has_npm:
        return VERDICT_ABSENT
    if week is not None:
        if week >= cfg.alive_weekly:
            return VERDICT_ALIVE
        return VERDICT_DEAD if week < cfg.dead_weekly else VERDICT_WEAK
    if month is not None:
        if month >= cfg.alive_monthly:
            return VERDICT_ALIVE
        return VERDICT_DEAD if month < cfg.dead_monthly else VERDICT_WEAK
    return VERDICT_PRESENT


def apply_stale(verdict_str: str, age_days: int | None, *, cfg: VerdictConfig) -> str:
    """«ЖИВО» без релизов дольше ``cfg.stale_days`` -> «ЗАСТОЙ» (иначе вердикт как есть).

    Скачивания есть, развития нет — зависимость-зомби. Поверх других вердиктов не
    накладывается: у «МЁРТВО» и «СЛАБО» своя причина, и подменять её нельзя.
    """
    if verdict_str == VERDICT_ALIVE and age_days is not None and age_days > cfg.stale_days:
        return VERDICT_STALE
    return verdict_str


def _index(rows: list[dict]) -> dict[str, dict]:
    """Наблюдения по имени; записи без имени и не-словари пропускаем."""
    indexed: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if isinstance(name, str) and name:
            indexed[name] = row
    return indexed


def _adoption_event(name: str, prev: dict, cur: dict, cfg: VerdictConfig) -> dict | None:
    """Событие о скачке недельных скачиваний; ``None``, если скачка нет."""
    old_week = prev.get("adoption_week")
    new_week = cur.get("adoption_week")
    if old_week is None or new_week is None or old_week <= 0:
        return None
    delta = (new_week - old_week) / old_week * 100
    if abs(delta) < cfg.adoption_delta_pct:
        return None
    return {
        "kind": "adoption_change",
        "name": name,
        "detail": f"{old_week} → {new_week} за нед ({delta:+.0f}%)",
        "old": old_week,
        "new": new_week,
    }


def _diff_one(name: str, prev: dict, cur: dict, cfg: VerdictConfig) -> list[dict]:
    """События по одному имени: релиз, признак ниши, adoption, вердикт — в этом порядке."""
    events: list[dict] = []

    old_ver, new_ver = prev.get("version"), cur.get("version")
    if old_ver != new_ver:
        released = cur.get("released_at") or "?"
        events.append(
            {
                "kind": "release",
                "name": name,
                "detail": f"{old_ver} → {new_ver} ({released})",
                "old": old_ver,
                "new": new_ver,
            }
        )

    old_flag, new_flag = bool(prev.get("flag")), bool(cur.get("flag"))
    if not old_flag and new_flag:
        events.append(
            {"kind": "flag_appeared", "name": name, "detail": DETAIL_FLAG_APPEARED,
             "old": False, "new": True}
        )
    elif old_flag and not new_flag:
        events.append(
            {"kind": "flag_removed", "name": name, "detail": DETAIL_FLAG_REMOVED,
             "old": True, "new": False}
        )

    adoption = _adoption_event(name, prev, cur, cfg)
    if adoption is not None:
        events.append(adoption)

    old_verdict, new_verdict = prev.get("verdict"), cur.get("verdict")
    if old_verdict != new_verdict:
        events.append(
            {
                "kind": "verdict_change",
                "name": name,
                "detail": f"{old_verdict} → {new_verdict}",
                "old": old_verdict,
                "new": new_verdict,
            }
        )

    return events


def diff_observations(prev: list[dict], cur: list[dict], *, cfg: VerdictConfig) -> list[dict]:
    """Что изменилось между двумя прогонами — список событий в стабильном порядке.

    Порядок: сначала появления (``new``, в порядке текущего прогона), затем пропажи
    (``missing``, в порядке прошлого — как legacy печатал «НОВОЕ» раньше «ПРОПАЛО»),
    затем по каждому общему имени его события в порядке ``release -> flag_* ->
    adoption_change -> verdict_change``. Порядок фиксирован, чтобы тесты и UI не
    зависели от порядка словаря и чтобы таймлайн читался сверху вниз.

    Все поля наблюдения опциональны: частичный словарь (например, без ``adoption_week``)
    не событий не порождает, но и не падает.

    Отличие от legacy ``diff_runs()``: там событие о скачке adoption пропадало, если
    новые скачивания равны нулю (``if ow and cw``), — здесь ноль считается изменением.
    """
    prev_index = _index(prev)
    cur_index = _index(cur)
    events: list[dict] = []

    for name, row in cur_index.items():
        if name not in prev_index:
            events.append(
                {"kind": "new", "name": name, "detail": DETAIL_NEW, "old": None, "new": row}
            )
    for name, row in prev_index.items():
        if name not in cur_index:
            events.append(
                {"kind": "missing", "name": name, "detail": DETAIL_MISSING, "old": row, "new": None}
            )

    for name, row in cur_index.items():
        previous = prev_index.get(name)
        if previous is not None:
            events.extend(_diff_one(name, previous, row, cfg))

    return events
