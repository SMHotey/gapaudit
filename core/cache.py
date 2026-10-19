"""Кэш с TTL для результатов HTTP (шаг 1a).

Один интерфейс — два потребителя:

* CLI — :class:`FileCache`, JSON-файл (как ``raw/.pcache.json`` в legacy, но с явной
  версией формата);
* приложение — SqliteCache поверх таблицы ``cache`` (шаг 2).

**SqliteCache живёт в app/store.py — core не знает про sqlite3.** Здесь остаётся
только контракт :class:`CacheBackend`: DictCache/FileCache (core, для CLI)
и SqliteCache (app, для UI/jobs) — см. AGENTS.md, gotcha 19.

Требования DEVPLAN §4: импорт модуля ничего не читает и не пишет, глобалов нет.
Конструктор тоже ничего не читает: файл загружается лениво при первом ``get``/``put``,
на диск пишет только :meth:`FileCache.save` (атомарно, tmp + ``os.replace``).

TTL задаётся один раз в конструкторе (по умолчанию 12 часов — как в legacy CLI) и
в методах не передаётся: приложению нужен один кэш на всю задачу, а не разные TTL
в разных вызовах.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Protocol

__all__ = [
    "DEFAULT_TTL_HOURS",
    "FORMAT_VERSION",
    "CacheBackend",
    "DictCache",
    "FileCache",
]

DEFAULT_TTL_HOURS = 12.0
FORMAT_VERSION = 1


class CacheBackend(Protocol):
    """Минимальный контракт кэша: его реализуют файл, память и SQLite."""

    def get(self, key: str) -> tuple[float, Any] | None:
        """(fetched_at_epoch, value) или None, если записи нет или она просрочена."""
        ...

    def put(self, key: str, value: Any, fetched_at: float | None = None) -> None:
        """Положить значение; ``fetched_at`` по умолчанию — текущее время."""
        ...

    def save(self) -> None:
        """Персист, если backend файловый. Для памяти — no-op."""
        ...

    def clear(self) -> None:
        """Удалить все записи."""
        ...


class _TtlCache:
    """Общая часть: хранение TTL и проверка свежести записи."""

    def __init__(self, ttl_hours: float = DEFAULT_TTL_HOURS) -> None:
        self.ttl_hours = float(ttl_hours)

    def _fresh(self, fetched_at: float, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return fetched_at + self.ttl_hours * 3600.0 >= moment


class DictCache(_TtlCache):
    """Кэш в памяти — для тестов и для сценариев «живём один прогон»."""

    def __init__(self, ttl_hours: float = DEFAULT_TTL_HOURS) -> None:
        super().__init__(ttl_hours)
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> tuple[float, Any] | None:
        hit = self._entries.get(key)
        if hit is None or not self._fresh(hit[0]):
            return None
        return hit

    def put(self, key: str, value: Any, fetched_at: float | None = None) -> None:
        self._entries[key] = (time.time() if fetched_at is None else float(fetched_at), value)

    def save(self) -> None:  # noqa: D401 — персиста нет, интерфейс общий
        return None

    def clear(self) -> None:
        self._entries.clear()

    def prune(self) -> int:
        """Удалить просроченные записи; вернуть, сколько удалено."""
        stale = [key for key, (fetched_at, _) in self._entries.items() if not self._fresh(fetched_at)]
        for key in stale:
            del self._entries[key]
        return len(stale)

    def __len__(self) -> int:
        return len(self._entries)


def _parse_payload(payload: Any) -> dict[str, dict[str, Any]]:
    """Разобрать содержимое файла: текущий формат, старый формат или мусор.

    Битый/неизвестный JSON — это пустой кэш, а не исключение: кэш не должен
    валить прогон, который может просто сходить в сеть заново.
    """
    if not isinstance(payload, dict):
        return {}

    entries: dict[str, dict[str, Any]] = {}
    current = payload.get("entries")
    if isinstance(current, dict):
        for key, record in current.items():
            if not isinstance(record, dict) or "ts" not in record:
                continue
            try:
                fetched_at = float(record["ts"])
            except (TypeError, ValueError):
                continue
            entries[str(key)] = {"ts": fetched_at, "value": record.get("value")}
        return entries

    # Старый формат raw/.pcache.json: {"GET <url>": [ts, [status, value]]}.
    for key, record in payload.items():
        if not isinstance(record, (list, tuple)) or len(record) != 2:
            continue
        try:
            fetched_at = float(record[0])
        except (TypeError, ValueError):
            continue
        entries[str(key)] = {"ts": fetched_at, "value": record[1]}
    return entries


class FileCache(_TtlCache):
    """JSON-файл с TTL: ``{"version": 1, "entries": {"<key>": {"ts": ..., "value": ...}}}``."""

    def __init__(self, path: str, ttl_hours: float = DEFAULT_TTL_HOURS) -> None:
        super().__init__(ttl_hours)
        self.path = str(path)
        self._entries: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._dirty = False

    # --- внутреннее ---------------------------------------------------------

    def _load(self) -> None:
        """Ленивая загрузка: первый get/put читает файл, конструктор — нет."""
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        self._entries = _parse_payload(payload)

    # --- контракт CacheBackend ---------------------------------------------

    def get(self, key: str) -> tuple[float, Any] | None:
        self._load()
        record = self._entries.get(key)
        if record is None:
            return None
        fetched_at = float(record["ts"])
        if not self._fresh(fetched_at):
            # Просроченную запись не удаляем: чистку делает put, prune или clear.
            return None
        return fetched_at, record["value"]

    def put(self, key: str, value: Any, fetched_at: float | None = None) -> None:
        self._load()
        self._entries[key] = {
            "ts": time.time() if fetched_at is None else float(fetched_at),
            "value": value,
        }
        self._dirty = True

    def save(self) -> None:
        """Записать файл атомарно: во временный файл рядом, затем ``os.replace``."""
        if not self._dirty:
            return
        payload = {"version": FORMAT_VERSION, "entries": self._entries}
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        handle_fd, tmp_path = tempfile.mkstemp(prefix=".pcache-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        self._dirty = False

    def clear(self) -> None:
        self._load()
        self._entries.clear()
        self._dirty = True

    # --- дополнительно к протоколу -----------------------------------------

    def prune(self) -> int:
        """Удалить просроченные записи; вернуть, сколько удалено."""
        self._load()
        stale = [
            key
            for key, record in self._entries.items()
            if not self._fresh(float(record["ts"]))
        ]
        for key in stale:
            del self._entries[key]
        if stale:
            self._dirty = True
        return len(stale)

    def __len__(self) -> int:
        self._load()
        return len(self._entries)
