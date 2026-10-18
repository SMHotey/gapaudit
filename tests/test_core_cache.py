"""Тесты кэша: TTL, файловый персист, устойчивость к битому JSON (шаг 1a).

Файловые тесты работают только в ``tmp_path`` — рабочая папка проекта не трогается.
"""

from __future__ import annotations

import json
import time

import pytest

from core.cache import DEFAULT_TTL_HOURS, DictCache, FileCache, SqliteCache


def test_default_ttl_is_12_hours() -> None:
    assert DEFAULT_TTL_HOURS == 12.0
    assert DictCache().ttl_hours == 12.0


def test_put_then_get_roundtrip() -> None:
    cache = DictCache()
    cache.put("GET https://example.test/a", (200, {"ok": True}))

    hit = cache.get("GET https://example.test/a")

    assert hit is not None
    fetched_at, value = hit
    assert value == (200, {"ok": True})
    assert abs(fetched_at - time.time()) < 5


def test_missing_key_returns_none() -> None:
    assert DictCache().get("GET https://example.test/none") is None


def test_expired_entry_returns_none_and_stays() -> None:
    cache = DictCache(ttl_hours=12)
    cache.put("k", "v", fetched_at=time.time() - 13 * 3600)

    assert cache.get("k") is None
    # Просроченная запись не удаляется на чтении: чистку делает prune/put/clear.
    assert len(cache) == 1
    assert cache.prune() == 1
    assert len(cache) == 0


def test_custom_ttl_is_honoured() -> None:
    cache = DictCache(ttl_hours=1)
    cache.put("k", "v", fetched_at=time.time() - 3601)
    assert cache.get("k") is None

    cache.put("fresh", "v", fetched_at=time.time() - 300)
    assert cache.get("fresh") is not None


def test_save_and_load_survive_rewrite(tmp_path) -> None:
    path = tmp_path / "raw" / ".pcache.json"
    cache = FileCache(str(path))
    cache.put("GET https://example.test/a", (200, {"n": 1}))
    cache.save()

    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["entries"]["GET https://example.test/a"]["value"] == [200, {"n": 1}]

    restored = FileCache(str(path))
    hit = restored.get("GET https://example.test/a")
    assert hit is not None
    assert hit[1] == [200, {"n": 1}]


def test_broken_json_behaves_like_empty_cache(tmp_path) -> None:
    path = tmp_path / ".pcache.json"
    path.write_text("{это не json", encoding="utf-8")

    cache = FileCache(str(path))
    assert cache.get("GET https://example.test/a") is None

    cache.put("GET https://example.test/a", (200, {"ok": 1}))
    assert cache.get("GET https://example.test/a") is not None


def test_legacy_flat_format_is_readable(tmp_path) -> None:
    """Старый raw/.pcache.json: {"GET url": [ts, [status, value]]}."""
    path = tmp_path / ".pcache.json"
    path.write_text(
        json.dumps({"GET https://example.test/a": [time.time(), [200, {"n": 2}]]}),
        encoding="utf-8",
    )

    hit = FileCache(str(path)).get("GET https://example.test/a")

    assert hit is not None
    assert hit[1] == [200, {"n": 2}]


def test_file_cache_is_lazy_and_creates_nothing(tmp_path) -> None:
    """Конструктор не читает и не пишет: файл появляется только после save()."""
    path = tmp_path / "nested" / ".pcache.json"
    cache = FileCache(str(path))
    cache.put("k", "v")

    assert not path.exists()
    assert list(tmp_path.iterdir()) == []

    cache.save()
    assert path.exists()


def test_dict_cache_needs_no_filesystem(tmp_path) -> None:
    cache = DictCache()
    cache.put("k", "v")

    assert cache.get("k") is not None
    assert cache.save() is None  # персиста нет, интерфейс общий
    assert list(tmp_path.iterdir()) == []


def test_clear_empties_cache(tmp_path) -> None:
    cache = DictCache()
    cache.put("k", "v")
    cache.clear()
    assert cache.get("k") is None

    path = tmp_path / ".pcache.json"
    file_cache = FileCache(str(path))
    file_cache.put("k", "v")
    file_cache.save()
    file_cache.clear()
    file_cache.save()
    assert FileCache(str(path)).get("k") is None


def test_sqlite_cache_is_a_step_2_placeholder() -> None:
    with pytest.raises(NotImplementedError, match="шаг 2"):
        SqliteCache(conn=None)
