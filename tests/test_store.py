"""Тесты SQLite-слоя: Store, миграции, API наблюдений/событий, SqliteCache (шаг 2).

Без файловой БД, где не нужна (in-memory ``Store(":memory:")``) и без сети:
HTTP-совместимость SqliteCache проверяется фейковым транспортом по паттерну
из ``tests/test_core_http.py``.
"""

from __future__ import annotations

import importlib
import re
import sqlite3
import time

import pytest

from app.store import SCHEMA_VERSION, Store, SqliteCache, utcnow_iso

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

ALL_TABLES = {
    "meta",
    "packages",
    "observations",
    "events",
    "cache",
    "jobs",
    "catalogs",
    "catalog_items",
    "schedules",
}


def observation(name: str = "requests", **overrides) -> dict:
    """Результат ``core.packages.check()`` — форма из контракта шага."""
    base = {
        "name": name,
        "src": "pypi",
        "ver": "2.32.3",
        "date": "2025-01-15",
        "age_days": 10,
        "summary": "HTTP for Humans",
        "home": "https://requests.readthedocs.io",
        "flag": True,
        "mcp": True,
        "month": 12000,
        "week": 3000,
        "verdict": "ЖИВОЙ",
    }
    base.update(overrides)
    return base


def conn_of(store: Store) -> sqlite3.Connection:
    """Соединение открытого Store: фикстура гарантирует, что оно открыто."""
    assert store.conn is not None, "Store не открыт"
    return store.conn


def table_names(store: Store) -> set[str]:
    rows = conn_of(store).execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def schema_version(store: Store) -> str | None:
    row = conn_of(store).execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return row[0] if row is not None else None


def count(store: Store, table: str) -> int:
    return int(conn_of(store).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.fixture
def store():
    with Store(":memory:") as opened:
        yield opened


# --- миграции -----------------------------------------------------------------


def test_migration_creates_all_tables_and_version_1(store: Store) -> None:
    assert store.conn is not None
    assert schema_version(store) == "1"
    assert SCHEMA_VERSION == 1
    # meta + 8 таблиц миграции 1; sqlite_sequence (AUTOINCREMENT) не наша.
    assert ALL_TABLES <= table_names(store)


def test_second_enter_on_file_db_is_idempotent(tmp_path) -> None:
    db = tmp_path / "twice.db"
    with Store(db) as first:
        first.record_observation(observation(), run_id="r1", measured_at="2026-10-19T10:00:00Z")
        assert schema_version(first) == "1"
    with Store(db) as second:
        assert schema_version(second) == "1"
        assert count(second, "observations") == 1


def test_migrate_from_version_zero_without_meta(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "fresh.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE scratch(x TEXT)")  # БД без meta — версия 0
    raw.commit()
    raw.close()
    with Store(db) as store:
        assert schema_version(store) == "1"
        assert "observations" in table_names(store)


def test_future_schema_version_raises(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "future.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    raw.execute("INSERT INTO meta VALUES('schema_version', '999')")
    raw.commit()
    raw.close()
    with pytest.raises(RuntimeError, match="более новой версией"):
        Store(db).__enter__()


def test_close_is_idempotent(store: Store) -> None:
    store.close()
    store.close()  # второй вызов не падает


def test_ping(store: Store) -> None:
    assert Store(":memory:").ping() is False  # не открыт
    assert store.ping() is True
    store.close()
    assert store.ping() is False


# --- record_observation --------------------------------------------------------


def test_record_observation_writes_observations_and_packages(store: Store) -> None:
    observed = observation()
    observed_id = store.record_observation(observed, run_id="run-1", measured_at="2026-10-19T10:00:00Z")

    assert observed_id > 0
    assert store.count_observations() == 1

    row = dict(conn_of(store).execute("SELECT * FROM observations").fetchone())
    assert row["id"] == observed_id
    assert row["run_id"] == "run-1"
    assert row["name"] == "requests"
    assert row["source"] == "pypi"
    assert row["version"] == "2.32.3"
    assert row["released_at"] == "2025-01-15"
    assert row["adoption_month"] == 12000
    assert row["adoption_week"] == 3000
    assert row["flag"] == "MCP"
    assert row["flag_value"] == 1
    assert row["verdict"] == "ЖИВОЙ"
    assert row["measured_at"] == "2026-10-19T10:00:00Z"

    package = store.get_package("requests")
    assert package is not None
    assert package["source"] == "pypi"
    assert package["url"] == "https://requests.readthedocs.io"
    assert package["summary"] == "HTTP for Humans"
    assert package["first_seen"] == "2026-10-19T10:00:00Z"
    assert package["last_seen"] == "2026-10-19T10:00:00Z"


def test_flag_maps_to_flag_value(store: Store) -> None:
    store.record_observation(observation("with-flag", flag=True), run_id="r", measured_at="2026-10-19T10:00:00Z")
    store.record_observation(
        observation("no-flag", flag=False), run_id="r", measured_at="2026-10-19T10:00:01Z"
    )
    flags = {
        dict(row)["name"]: (dict(row)["flag"], dict(row)["flag_value"])
        for row in conn_of(store).execute("SELECT * FROM observations")
    }
    assert flags["with-flag"] == ("MCP", 1)
    assert flags["no-flag"] == ("MCP", 0)


def test_keyword_label_overrides_flag(store: Store) -> None:
    store.record_observation(
        observation(), run_id="r", measured_at="2026-10-19T10:00:00Z", keyword_label="HELM"
    )
    row = dict(conn_of(store).execute("SELECT * FROM observations").fetchone())
    assert row["flag"] == "HELM"
    assert row["flag_value"] == 1


def test_default_measured_at_is_iso_utc(store: Store) -> None:
    store.record_observation(observation(), run_id="r")
    row = dict(conn_of(store).execute("SELECT * FROM observations").fetchone())
    assert ISO_RE.match(row["measured_at"]), row["measured_at"]
    assert len(row["measured_at"]) == 20


def test_explicit_measured_at_preserved(store: Store) -> None:
    store.record_observation(observation(), run_id="r", measured_at="2026-01-02T03:04:05Z")
    row = dict(conn_of(store).execute("SELECT * FROM observations").fetchone())
    assert row["measured_at"] == "2026-01-02T03:04:05Z"


def test_empty_name_raises(store: Store) -> None:
    with pytest.raises(ValueError):
        store.record_observation(observation(""), run_id="r")


def test_absent_package_src_dash_skips_packages(store: Store) -> None:
    store.record_observation(
        observation("ghost", src="-", ver=None, date=None), run_id="r", measured_at="2026-10-19T10:00:00Z"
    )
    assert count(store, "observations") == 1
    assert count(store, "packages") == 0
    row = dict(conn_of(store).execute("SELECT * FROM observations").fetchone())
    assert row["source"] == "-"


# --- record_observations (батч) -------------------------------------------------


def test_batch_writes_all_three(store: Store) -> None:
    ids = store.record_observations(
        [observation("alpha"), observation("beta"), observation("gamma")],
        run_id="batch",
        measured_at="2026-10-19T10:00:00Z",
    )
    assert len(set(ids)) == 3 and all(i > 0 for i in ids)
    assert store.count_observations() == 3
    assert count(store, "packages") == 3


def test_batch_uses_single_utcnow(store: Store, monkeypatch) -> None:
    calls: list[int] = []

    def counting_now() -> str:
        calls.append(1)
        return f"2026-10-19T10:00:{len(calls):02d}Z"

    monkeypatch.setattr("app.store.utcnow_iso", counting_now)
    store.record_observations(
        [observation("alpha"), observation("beta"), observation("gamma")], run_id="batch"
    )
    assert len(calls) == 1  # один utcnow_iso() на весь батч
    moments = {
        dict(row)["measured_at"]
        for row in conn_of(store).execute("SELECT measured_at FROM observations")
    }
    assert moments == {"2026-10-19T10:00:01Z"}


def test_batch_rolls_back_completely_on_error(store: Store) -> None:
    with pytest.raises(ValueError):
        store.record_observations(
            [observation("alpha"), observation(""), observation("gamma")], run_id="batch"
        )
    assert store.count_observations() == 0
    assert count(store, "packages") == 0


# --- record_event / record_events ------------------------------------------------


def test_record_event_inserts_row(store: Store) -> None:
    event_id = store.record_event(
        run_id="r", kind="release", name="requests", detail="1.0 → 2.0 (2026-10-01)"
    )
    assert event_id > 0
    row = dict(conn_of(store).execute("SELECT * FROM events").fetchone())
    assert row["id"] == event_id
    assert row["run_id"] == "r"
    assert row["name"] == "requests"
    assert row["detail"] == "1.0 → 2.0 (2026-10-01)"
    assert ISO_RE.match(row["at"])


def test_record_events_batch_shares_single_at(store: Store, monkeypatch) -> None:
    calls: list[int] = []

    def counting_now() -> str:
        calls.append(1)
        return f"2026-10-19T11:{len(calls):02d}:00Z"

    monkeypatch.setattr("app.store.utcnow_iso", counting_now)
    ids = store.record_events(
        [
            {"kind": "new", "name": "alpha"},
            {"kind": "missing", "name": "beta", "detail": "нет в реестре"},
            {"kind": "release", "name": "gamma"},
        ],
        run_id="r",
    )
    assert len(set(ids)) == 3
    assert len(calls) == 1
    assert count(store, "events") == 3
    moments = {dict(row)["at"] for row in conn_of(store).execute("SELECT at FROM events")}
    assert moments == {"2026-10-19T11:01:00Z"}


def test_event_kind_saved_as_is(store: Store) -> None:
    store.record_event(run_id="r", kind="flag_appeared", name="requests")
    row = dict(conn_of(store).execute("SELECT * FROM events").fetchone())
    assert row["kind"] == "flag_appeared"


# --- get_latest_observations ------------------------------------------------------


def test_latest_picks_newest_per_name(store: Store) -> None:
    store.record_observation(observation("a"), run_id="r", measured_at="2026-10-19T09:00:00Z")
    store.record_observation(observation("b"), run_id="r", measured_at="2026-10-19T10:00:00Z")
    store.record_observation(observation("a"), run_id="r", measured_at="2026-10-19T11:00:00Z")

    latest = store.get_latest_observations(["a", "b"])
    assert len(latest) == 2
    by_name = {row["name"]: row for row in latest}
    assert by_name["a"]["measured_at"] == "2026-10-19T11:00:00Z"
    assert by_name["b"]["measured_at"] == "2026-10-19T10:00:00Z"


def test_latest_without_names_returns_all_distinct(store: Store) -> None:
    store.record_observation(observation("a"), run_id="r", measured_at="2026-10-19T09:00:00Z")
    store.record_observation(observation("b"), run_id="r", measured_at="2026-10-19T10:00:00Z")
    store.record_observation(observation("a"), run_id="r", measured_at="2026-10-19T11:00:00Z")

    latest = store.get_latest_observations()
    assert sorted(row["name"] for row in latest) == ["a", "b"]


def test_latest_with_empty_names_is_empty(store: Store) -> None:
    store.record_observation(observation(), run_id="r")
    assert store.get_latest_observations([]) == []


def test_latest_result_has_check_keys(store: Store) -> None:
    store.record_observation(observation(), run_id="r", measured_at="2026-10-19T10:00:00Z")
    row = store.get_latest_observations(["requests"])[0]
    required = {
        "name",
        "source",
        "version",
        "released_at",
        "adoption_month",
        "adoption_week",
        "flag_value",
        "flag",
        "verdict",
        "measured_at",
        "run_id",
    }
    assert required <= set(row)


# --- get_observations_history ------------------------------------------------------


def test_history_newest_first(store: Store) -> None:
    for moment in ("2026-10-17T10:00:00Z", "2026-10-18T10:00:00Z", "2026-10-19T10:00:00Z"):
        store.record_observation(observation("a"), run_id="r", measured_at=moment)
    history = store.get_observations_history("a")
    assert len(history) == 3
    assert [row["measured_at"] for row in history] == [
        "2026-10-19T10:00:00Z",
        "2026-10-18T10:00:00Z",
        "2026-10-17T10:00:00Z",
    ]


def test_history_respects_limit(store: Store) -> None:
    for moment in ("2026-10-17T10:00:00Z", "2026-10-18T10:00:00Z", "2026-10-19T10:00:00Z"):
        store.record_observation(observation("a"), run_id="r", measured_at=moment)
    assert len(store.get_observations_history("a", limit=2)) == 2


def test_history_unknown_name_is_empty(store: Store) -> None:
    assert store.get_observations_history("nope") == []


# --- get_events / get_package / list_packages / count_* ----------------------------


def test_events_since_filter(store: Store) -> None:
    store.record_event(run_id="r", kind="release", name="old", at="2020-01-01T00:00:00Z")
    store.record_event(run_id="r", kind="release", name="new", at="2026-06-01T00:00:00Z")
    events = store.get_events(since="2026-01-01T00:00:00Z")
    assert [row["name"] for row in events] == ["new"]


def test_events_kind_filter(store: Store) -> None:
    store.record_event(run_id="r", kind="release", name="a")
    store.record_event(run_id="r", kind="flag_appeared", name="b")
    store.record_event(run_id="r", kind="release", name="c")
    releases = store.get_events(kind="release")
    assert sorted(row["name"] for row in releases) == ["a", "c"]


def test_get_package_returns_row_or_none(store: Store) -> None:
    assert store.get_package("requests") is None
    store.record_observation(observation(), run_id="r", measured_at="2026-10-19T10:00:00Z")
    package = store.get_package("requests")
    assert isinstance(package, dict)
    assert package["name"] == "requests"


def test_list_packages_order_limit_and_filter(store: Store) -> None:
    for name, moment in (
        ("p-old", "2026-10-01T10:00:00Z"),
        ("p-new", "2026-10-03T10:00:00Z"),
        ("p-mid", "2026-10-02T10:00:00Z"),
    ):
        store.record_observation(observation(name), run_id="r", measured_at=moment)
    # пакет без наблюдений — фильтр only_with_observations должен его убрать
    conn_of(store).execute(
        "INSERT INTO packages(name, source, url, summary, first_seen, last_seen) "
        "VALUES('lonely', 'pypi', NULL, NULL, '2026-10-04T10:00:00Z', '2026-10-04T10:00:00Z')"
    )

    names = [row["name"] for row in store.list_packages()]
    assert names == ["lonely", "p-new", "p-mid", "p-old"]  # last_seen DESC
    assert len(store.list_packages(limit=2)) == 2
    filtered = [row["name"] for row in store.list_packages(only_with_observations=True)]
    assert "lonely" not in filtered
    assert sorted(filtered) == ["p-mid", "p-new", "p-old"]


def test_counts(store: Store) -> None:
    store.record_observation(observation(), run_id="r", measured_at="2026-10-19T10:00:00Z")
    store.record_event(run_id="r", kind="new", name="requests", at="2026-10-19T10:00:00Z")
    store.record_event(run_id="r", kind="release", name="requests", at="2026-10-19T10:00:01Z")
    assert store.count_observations() == 1
    assert store.count_events() == 2


# --- SqliteCache --------------------------------------------------------------------


def test_cache_put_get_roundtrip(store: Store) -> None:
    cache = SqliteCache(store)
    cache.put("GET https://example.test/api", {"hello": "мир"})
    hit = cache.get("GET https://example.test/api")
    assert hit is not None
    fetched_at, value = hit
    assert isinstance(fetched_at, float)
    assert value == {"hello": "мир"}
    assert cache.get("GET https://missing") is None


def test_cache_expired_ttl_returns_none_keeps_row(store: Store) -> None:
    cache = SqliteCache(store, ttl_hours=12.0)
    stale = time.time() - 13 * 3600
    cache.put("GET https://example.test", {"a": 1}, fetched_at=stale)
    assert cache.get("GET https://example.test") is None
    assert count(store, "cache") == 1  # просроченная запись не удаляется


def test_cache_save_is_noop(store: Store) -> None:
    cache = SqliteCache(store)
    cache.put("k", "v")
    assert cache.save() is None
    assert cache.get("k") is not None  # save не нужен для видимости записи


def test_cache_clear_removes_everything(store: Store) -> None:
    cache = SqliteCache(store)
    cache.put("k1", "v1")
    cache.put("k2", "v2")
    cache.clear()
    assert cache.get("k1") is None
    assert cache.get("k2") is None
    assert count(store, "cache") == 0


class _FakeTransport:
    """Транспорт-заглушка: считает вызовы, всегда отдаёт один и тот же JSON."""

    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}') -> None:
        self.calls = 0
        self._status = status
        self._body = body

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls += 1
        return self._status, self._body


def test_cache_works_with_http_client(store: Store) -> None:
    from core.http import HttpClient, Throttle

    transport = _FakeTransport()
    client = HttpClient(
        cache=SqliteCache(store),
        throttle=Throttle(per_minute={"example.test": 60000}),
        transport=transport,
        sleep=lambda _seconds: None,
    )
    first = client.get("https://example.test/api/thing")
    second = client.get("https://example.test/api/thing")
    assert first == (200, {"ok": True})
    assert second == (200, {"ok": True})
    assert transport.calls == 1  # второй вызов взят из кэша
    assert count(store, "cache") == 1


def test_core_cache_does_not_export_sqlite_cache() -> None:
    """Gotcha 19: core не знает про sqlite3 — заглушки в core больше нет."""
    import core.cache

    assert not hasattr(core.cache, "SqliteCache")


# --- utcnow_iso ----------------------------------------------------------------------


def test_utcnow_iso_format() -> None:
    moment = utcnow_iso()
    assert ISO_RE.match(moment), moment
    assert len(moment) == 20


def test_utcnow_iso_sorts_as_string() -> None:
    first = utcnow_iso()
    second = utcnow_iso()
    assert second >= first


# --- идемпотентность --------------------------------------------------------------------


def test_same_run_id_keeps_history(store: Store) -> None:
    store.record_observation(observation(), run_id="run", measured_at="2026-10-19T09:00:00Z")
    store.record_observation(observation(), run_id="run", measured_at="2026-10-19T10:00:00Z")
    assert store.count_observations() == 2
    assert len(store.get_observations_history("requests")) == 2


def test_two_stores_on_same_file_see_data(tmp_path) -> None:
    db = tmp_path / "shared.db"
    with Store(db) as first:
        first.record_observation(observation(), run_id="r1", measured_at="2026-10-19T09:00:00Z")
    with Store(db) as second:
        assert second.count_observations() == 1
        assert second.get_package("requests") is not None
        second.record_observation(observation(), run_id="r2", measured_at="2026-10-19T10:00:00Z")
        assert second.count_observations() == 2


def test_import_store_creates_no_files(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    import app.store

    importlib.reload(app.store)
    assert list(tmp_path.iterdir()) == []
