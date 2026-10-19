"""Тесты SQLite-слоя: Store, миграции, API наблюдений/событий, SqliteCache (шаг 2).

Без файловой БД, где не нужна (in-memory ``Store(":memory:")``) и без сети:
HTTP-совместимость SqliteCache проверяется фейковым транспортом по паттерну
из ``tests/test_core_http.py``.
"""

from __future__ import annotations

import importlib
import re
import sqlite3
import threading
import time

import pytest

from app.store import SCHEMA_VERSION, Store, SqliteCache, migrate_1, utcnow_iso

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


def test_migration_creates_all_tables_and_version_2(store: Store) -> None:
    assert store.conn is not None
    assert schema_version(store) == "2"
    assert SCHEMA_VERSION == 2
    # meta + 8 таблиц миграции 1; sqlite_sequence (AUTOINCREMENT) не наша.
    assert ALL_TABLES <= table_names(store)


def test_second_enter_on_file_db_is_idempotent(tmp_path) -> None:
    db = tmp_path / "twice.db"
    with Store(db) as first:
        first.record_observation(observation(), run_id="r1", measured_at="2026-10-19T10:00:00Z")
        assert schema_version(first) == "2"
    with Store(db) as second:
        assert schema_version(second) == "2"
        assert count(second, "observations") == 1


def test_migrate_from_version_zero_without_meta(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "fresh.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE scratch(x TEXT)")  # БД без meta — версия 0
    raw.commit()
    raw.close()
    with Store(db) as store:
        assert schema_version(store) == "2"
        assert "observations" in table_names(store)


def test_migrate_1_to_2_adds_url_column_keeping_old_rows(tmp_path) -> None:
    """БД версии 1 поднимается до 2: колонка ``url`` появляется, строки целы.

    Версия 1 создаётся вручную (``migrate_1`` + строка в ``meta``) — так
    выглядит БД, сделанная приложением до шага 3c. Строка каталога, записанная
    до миграции, обязана остаться: ``ALTER TABLE ADD COLUMN`` не перестраивает
    таблицу, и у неё просто нет ссылки.
    """
    db = tmp_path / "v1.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    migrate_1(raw)
    assert raw.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0] == "1"
    assert "url" not in {row[1] for row in raw.execute("PRAGMA table_info(catalog_items)")}
    raw.execute("INSERT INTO catalog_items(catalog_id, name) VALUES('c1', 'old/repo')")
    raw.commit()
    raw.close()

    with Store(db) as store:
        assert schema_version(store) == "2"
        assert "url" in {
            row[1] for row in conn_of(store).execute("PRAGMA table_info(catalog_items)")
        }
        assert store.get_catalog_items("c1")[0]["url"] is None


def test_record_catalog_items_stores_url(store: Store) -> None:
    """``url`` из находки источника доезжает до ``get_catalog_items``."""
    store.record_catalog_items(
        "cat-1",
        [{"name": "owner/repo", "url": "https://github.com/owner/repo", "sources": ["github"]}],
    )

    item = store.get_catalog_items("cat-1")[0]
    assert item["url"] == "https://github.com/owner/repo"


def test_record_catalog_items_without_url_stores_none(store: Store) -> None:
    """Нет ``url`` (или он пустой) — NULL: экран отличает «ссылки нет» от адреса."""
    store.record_catalog_items(
        "cat-1",
        [
            {"name": "no-url", "sources": ["registry"]},
            {"name": "empty-url", "url": "", "sources": ["registry"]},
        ],
    )

    urls = {item["name"]: item["url"] for item in store.get_catalog_items("cat-1")}
    assert urls == {"no-url": None, "empty-url": None}


def test_reenter_version_2_db_does_not_rerun_migration(tmp_path) -> None:
    """Повторное открытие БД v2 — без «duplicate column name: url».

    Проверка на файле, а не на ``:memory:``: миграция видит уже применённую
    схему только если версия записана в ``meta`` — файл это и проверяет.
    """
    db = tmp_path / "twice-v2.db"
    with Store(db) as first:
        first.record_catalog_items("c1", [{"name": "owner/repo", "url": "https://x"}])

    with Store(db) as second:
        assert schema_version(second) == "2"
        assert second.get_catalog_items("c1")[0]["url"] == "https://x"


def test_future_schema_version_raises(tmp_path) -> None:

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


# --- фильтр по имени и пагинация событий (шаг 3a) ---------------------------


def _seed_events(store: Store) -> None:
    """Три события: два релиза requests, один новый flask (общая база для тестов)."""
    store.record_events(
        [
            {"kind": "release", "name": "requests", "detail": "2.32.0"},
            {"kind": "release", "name": "requests", "detail": "2.32.1"},
            {"kind": "new", "name": "flask", "detail": None},
        ],
        run_id="r",
        at="2026-10-19T10:00:00Z",
    )


def test_events_name_filter(store: Store) -> None:
    """``name`` отбирает события одного пакета (фильтр живёт в SQL, шаг 3a)."""
    _seed_events(store)

    names = [row["name"] for row in store.get_events(name="requests")]

    assert names == ["requests", "requests"]
    assert store.get_events(name="нет-такого") == []


def test_events_kind_and_name_filters_combine(store: Store) -> None:
    """Два фильтра складываются через AND, а не перекрывают друг друга."""
    _seed_events(store)

    both = store.get_events(kind="release", name="requests")
    mismatch = store.get_events(kind="new", name="requests")

    assert [row["detail"] for row in both] == ["2.32.1", "2.32.0"]
    assert mismatch == []


def test_events_offset_paginates_without_repeats(store: Store) -> None:
    """``offset`` листает по ``at DESC, id DESC``: страницы не пересекаются."""
    for index in range(12):
        store.record_event(
            run_id="r",
            kind="release",
            name=f"p{index:02d}",
            at=f"2026-10-19T10:{index:02d}:00Z",
        )

    first = [row["name"] for row in store.get_events(limit=5, offset=0)]
    second = [row["name"] for row in store.get_events(limit=5, offset=5)]
    last = [row["name"] for row in store.get_events(limit=5, offset=10)]

    assert first == ["p11", "p10", "p09", "p08", "p07"]
    assert len(second) == 5 and not set(first) & set(second)
    assert len(last) == 2
    assert store.get_events() == store.get_events(limit=200, offset=0)


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


def test_search_packages_substring_and_escaping(store: Store) -> None:
    """Поиск по подстроке в name и summary; ``%`` — символ, а не шаблон."""
    for name in ("requests", "urllib3", "flask"):
        store.record_observation(
            observation(name, summary=f"description of {name}"),
            run_id="r",
            measured_at="2026-10-19T10:00:00Z",
        )
    store.record_observation(
        observation("percent", summary="shares 100% of the market"),
        run_id="r",
        measured_at="2026-10-19T10:00:00Z",
    )

    assert len(store.search_packages("")) == 4
    assert store.count_packages_filtered("") == 4
    assert [row["name"] for row in store.search_packages("REQ")] == ["requests"]
    # "lib" есть только в summary у urllib3
    assert [row["name"] for row in store.search_packages("lib")] == ["urllib3"]
    # «%» — буква: без экранирования LIKE вернул бы все четыре пакета
    assert [row["name"] for row in store.search_packages("%")] == ["percent"]
    assert store.count_packages_filtered("%") == 1
    assert store.search_packages("zzz") == []


def test_counts(store: Store) -> None:
    store.record_observation(observation(), run_id="r", measured_at="2026-10-19T10:00:00Z")
    store.record_event(run_id="r", kind="new", name="requests", at="2026-10-19T10:00:00Z")
    store.record_event(run_id="r", kind="release", name="requests", at="2026-10-19T10:00:01Z")
    assert store.count_observations() == 1
    assert store.count_events() == 2


# --- jobs (таблица jobs, шаг 3a) -------------------------------------------


def job_record(
    job_id: str = "job-1",
    *,
    kind: str = "packages.check",
    status: str = "queued",
    created_at: str = "2026-10-19T10:00:00Z",
    params: dict | None = None,
) -> dict:
    """Словарь для Store.create_job() в форме контракта шага 3a."""
    return {
        "id": job_id,
        "kind": kind,
        "params": {"names": ["requests"]} if params is None else params,
        "status": status,
        "progress": 0.0,
        "message": "",
        "created_at": created_at,
        "log_path": f"data/jobs/{job_id}.log",
    }


def test_create_job_round_trip(store: Store) -> None:
    store.create_job(job_record())
    job = store.get_job("job-1")
    assert job is not None
    assert job["id"] == "job-1"
    assert job["kind"] == "packages.check"
    assert job["status"] == "queued"
    assert job["progress"] == 0.0
    assert job["message"] == ""
    assert job["created_at"] == "2026-10-19T10:00:00Z"
    assert job["log_path"] == "data/jobs/job-1.log"
    assert job["params"] == {"names": ["requests"]}  # JSON обратно в dict
    # заполняются позже через update_job
    assert job["started_at"] is None
    assert job["finished_at"] is None
    assert job["error"] is None
    assert job["pid"] is None


def test_get_job_unknown_id_returns_none(store: Store) -> None:
    assert store.get_job("nope") is None


def test_update_job_status_started_at(store: Store) -> None:
    store.create_job(job_record())
    store.update_job(
        "job-1",
        status="running",
        started_at="2026-10-19T10:00:05Z",
        progress=0.25,
        message="стр. 1/4",
        pid=4242,
    )
    job = store.get_job("job-1")
    assert job is not None
    assert job["status"] == "running"
    assert job["started_at"] == "2026-10-19T10:00:05Z"
    assert job["progress"] == 0.25
    assert job["message"] == "стр. 1/4"
    assert job["pid"] == 4242


def test_update_job_unknown_field_raises(store: Store) -> None:
    store.create_job(job_record())
    with pytest.raises(ValueError, match="недопустимые поля"):
        store.update_job("job-1", kind="registry.scan")
    with pytest.raises(ValueError, match="params"):
        store.update_job("job-1", params={"x": 1})


def test_update_job_unknown_id_is_noop(store: Store) -> None:
    store.create_job(job_record())
    assert store.update_job("ghost", status="done") is None  # 0 строк, без исключения
    assert store.get_job("ghost") is None


def test_list_jobs_created_at_desc(store: Store) -> None:
    store.create_job(job_record("old", created_at="2026-10-19T10:00:00Z"))
    store.create_job(job_record("new", created_at="2026-10-19T12:00:00Z"))
    store.create_job(job_record("mid", created_at="2026-10-19T11:00:00Z"))
    assert [row["id"] for row in store.list_jobs()] == ["new", "mid", "old"]


def test_list_jobs_status_filter(store: Store) -> None:
    store.create_job(job_record("a", status="queued"))
    store.create_job(job_record("b", status="done", created_at="2026-10-19T11:00:00Z"))
    store.create_job(job_record("c", status="queued", created_at="2026-10-19T12:00:00Z"))
    queued = store.list_jobs(status="queued")
    assert sorted(row["id"] for row in queued) == ["a", "c"]
    assert [row["id"] for row in store.list_jobs(status="done")] == ["b"]


def test_list_jobs_limit(store: Store) -> None:
    for index in range(3):
        store.create_job(
            job_record(f"j{index}", created_at=f"2026-10-19T10:00:0{index}Z")
        )
    rows = store.list_jobs(limit=2)
    assert len(rows) == 2
    assert [row["id"] for row in rows] == ["j2", "j1"]  # по created_at DESC


def test_list_jobs_parallel_create(store: Store) -> None:
    """2 потока × 50 create_job: ровно 100 строк, без исключений."""
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def writer(prefix: str) -> None:
        try:
            start.wait(timeout=10)
            for index in range(50):
                store.create_job(
                    job_record(
                        f"{prefix}-{index}",
                        created_at="2026-10-19T10:00:00Z",
                    )
                )
        except BaseException as error:  # noqa: BLE001 - провал потока должен упасть в main
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert len(store.list_jobs(limit=200)) == 100


# --- потокобезопасность Store (шаг 3a, follow-up 0.1) ------------------------


def test_record_observation_is_thread_safe(tmp_path) -> None:
    """2 потока × 100 записей на файловой БД: ровно 200 наблюдений."""
    errors: list[BaseException] = []
    with Store(tmp_path / "concurrent.db") as store:
        start = threading.Barrier(2)

        def writer(run_id: str) -> None:
            try:
                start.wait(timeout=10)
                for _ in range(100):
                    store.record_observation(observation(), run_id=run_id)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=writer, args=(r,)) for r in ("run-a", "run-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors, errors
        assert store.count_observations() == 200


def test_reads_during_writes_do_not_raise(tmp_path) -> None:
    """Чтения параллельно с записью не роняются и видят монотонный счётчик."""
    errors: list[BaseException] = []
    observed: list[int] = []
    with Store(tmp_path / "readers.db") as store:
        stop = threading.Event()

        def writer() -> None:
            try:
                for _ in range(50):
                    store.record_observation(observation(), run_id="run")
            except BaseException as error:  # noqa: BLE001
                errors.append(error)
            finally:
                stop.set()

        def reader() -> None:
            try:
                while not stop.is_set():
                    store.count_observations()
                    store.get_latest_observations()
                    store.get_events(limit=5)
                observed.append(store.count_observations())
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors, errors
        assert observed and observed[0] == 50


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


# --- каталоги (шаг 3b) ---------------------------------------------------------------


def catalog_item(name: str = "owner/repo", **overrides) -> dict:
    """Строка каталога в форме ``core.catalog.build_catalog`` (проект — в ``id``)."""
    base = {
        "source": "github",
        "id": name,
        "url": f"https://github.com/{name}",
        "desc": "test impact analysis",
        "stars": 100,
        "lang": "Python",
        "pushed": "2026-10-01",
        "topics": [],
        "query": "test impact analysis",
        "sources": ["github"],
    }
    base.update(overrides)
    return base


def date_ago(days: int) -> str:
    """``YYYY-MM-DD`` N дней назад — так GitHub отдаёт ``pushed_at[:10]``."""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))


def seed_catalog(
    store: Store, catalog_id: str = "cat-1", items: list[dict] | None = None
) -> None:
    """Каталог + элементы: то, что в проде делает хук lifespan по result.json."""
    store.record_catalog(
        catalog_id=catalog_id,
        queries=["test impact analysis"],
        stats={"total": len(items or [])},
    )
    store.record_catalog_items(
        catalog_id, items if items is not None else [catalog_item()]
    )


def test_record_catalog_round_trip(store: Store) -> None:
    """Записанный каталог читается с развёрнутыми JSON-полями и меткой времени."""
    store.record_catalog(
        catalog_id="c1",
        queries=["flaky test detection", "test impact analysis"],
        stats={"total": 7, "languages": {"Python": 5}},
        path="raw/c1.json",
    )

    catalog = store.get_catalog("c1")

    assert catalog is not None
    assert catalog["id"] == "c1"
    assert catalog["queries"] == ["flaky test detection", "test impact analysis"]
    assert catalog["stats"] == {"total": 7, "languages": {"Python": 5}}
    assert catalog["path"] == "raw/c1.json"
    assert ISO_RE.match(catalog["created_at"])


def test_record_catalog_same_id_replaces_row(store: Store) -> None:
    """Повторная запись того же id — UPSERT, а не вторая строка."""
    store.record_catalog(
        catalog_id="c1", queries=["old"], stats={"total": 1}, path="raw/old.json"
    )
    store.record_catalog(catalog_id="c1", queries=["new"], stats={"total": 2})

    assert count(store, "catalogs") == 1
    catalog = store.get_catalog("c1")
    assert catalog is not None
    assert catalog["queries"] == ["new"]
    assert catalog["stats"] == {"total": 2}
    # путь от повторной записи без аргумента не затирается
    assert catalog["path"] == "raw/old.json"


def test_record_catalog_items_writes_every_row(store: Store) -> None:
    seed_catalog(
        store,
        items=[
            catalog_item("a/x"),
            catalog_item("b/y", stars=5),
            catalog_item("c/z", stars=None),
        ],
    )

    assert count(store, "catalog_items") == 3
    assert {item["name"] for item in store.get_catalog_items("cat-1")} == {
        "a/x",
        "b/y",
        "c/z",
    }


def test_record_catalog_items_is_idempotent(store: Store) -> None:
    """Тот же каталог повторно: PK ``(catalog_id, name)`` держит 3 строки.

    Заодно проверяется, что повторная запись обновляет данные, а не молча
    оставляет старые: ``INSERT OR REPLACE``.
    """
    items = [catalog_item("a/x"), catalog_item("b/y"), catalog_item("c/z")]
    seed_catalog(store, items=items)

    written = store.record_catalog_items(
        "cat-1", [dict(item, stars=(item["stars"] or 0) + 1) for item in items]
    )

    assert written == 3
    assert count(store, "catalog_items") == 3
    assert {item["stars"] for item in store.get_catalog_items("cat-1")} == {101}


def test_record_catalog_items_uses_id_and_joined_sources(store: Store) -> None:
    """Имя — из ``id``, если нет ``name``; источники склеены через запятую."""
    seed_catalog(
        store, items=[catalog_item("owner/repo", sources=["github", "npm"])]
    )

    item = store.get_catalog_items("cat-1")[0]

    assert item["name"] == "owner/repo"
    assert item["source"] == "github,npm"


def test_record_catalog_items_skips_rows_without_name(store: Store) -> None:
    """Строка без имени не пишется: PK каталога — имя, пустое не стыкуется."""
    seed_catalog(store, items=[catalog_item("a/x"), {"stars": 3}, "мусор"])  # type: ignore[list-item]

    assert count(store, "catalog_items") == 1


def test_get_catalog_items_sorted_by_stars_desc(store: Store) -> None:
    """Порядок — по звёздам вниз; строки без звёзд в конце (NULLS LAST)."""
    seed_catalog(
        store,
        items=[
            catalog_item("low/repo", stars=5),
            catalog_item("high/repo", stars=900),
            catalog_item("none/repo", stars=None),
            catalog_item("mid/repo", stars=50),
        ],
    )

    names = [item["name"] for item in store.get_catalog_items("cat-1")]

    assert names == ["high/repo", "mid/repo", "low/repo", "none/repo"]


def test_get_catalog_items_alive_true_keeps_fresh_push(store: Store) -> None:
    """``alive=True`` — только push за последние 90 дней."""
    seed_catalog(
        store,
        items=[
            catalog_item("fresh/repo", pushed=date_ago(5)),
            catalog_item("stale/repo", pushed=date_ago(400)),
            catalog_item("unknown/repo", pushed=None),
        ],
    )

    assert [item["name"] for item in store.get_catalog_items("cat-1", alive=True)] == [
        "fresh/repo"
    ]
    assert store.count_catalog_items("cat-1", alive=True) == 1


def test_get_catalog_items_alive_false_keeps_stale_push(store: Store) -> None:
    """``alive=False`` — push старше 90 дней; строка без даты — ни в одной группе."""
    seed_catalog(
        store,
        items=[
            catalog_item("fresh/repo", pushed=date_ago(5)),
            catalog_item("stale/repo", pushed=date_ago(400)),
            catalog_item("unknown/repo", pushed=None),
        ],
    )

    assert [item["name"] for item in store.get_catalog_items("cat-1", alive=False)] == [
        "stale/repo"
    ]
    assert store.count_catalog_items("cat-1", alive=False) == 1


def test_get_catalog_items_lang_filter(store: Store) -> None:
    seed_catalog(
        store,
        items=[
            catalog_item("py/repo", lang="Python"),
            catalog_item("go/repo", lang="Go"),
        ],
    )

    assert [item["name"] for item in store.get_catalog_items("cat-1", lang="Go")] == [
        "go/repo"
    ]


def test_get_catalog_items_min_stars_filter(store: Store) -> None:
    """Порог звёзд; ``min_stars=0`` — «без ограничения», включая строки без звёзд."""
    seed_catalog(
        store,
        items=[
            catalog_item("big/repo", stars=100),
            catalog_item("small/repo", stars=3),
            catalog_item("none/repo", stars=None),
        ],
    )

    assert [item["name"] for item in store.get_catalog_items("cat-1", min_stars=10)] == [
        "big/repo"
    ]
    assert store.count_catalog_items("cat-1", min_stars=0) == 3


def test_get_catalog_items_q_matches_name_and_desc(store: Store) -> None:
    """Поиск — по имени и описанию, без учёта регистра."""
    seed_catalog(
        store,
        items=[
            catalog_item("flaky/tool", desc="Flaky test detection"),
            catalog_item("another/repo", desc="flaky tests are painful"),
            catalog_item("unrelated/repo", desc="vector database"),
        ],
    )

    hits = store.get_catalog_items("cat-1", q="FLAKY")

    assert {item["name"] for item in hits} == {"flaky/tool", "another/repo"}


def test_get_catalog_items_q_escapes_like_wildcards(store: Store) -> None:
    """``%`` и ``_`` в запросе — символы, а не шаблон (gotcha 44)."""
    seed_catalog(
        store,
        items=[
            catalog_item("percent/repo", desc="ускоряет сборку на 100%"),
            catalog_item("plain/repo", desc="совсем другое описание"),
            catalog_item("under/repo", desc="имя a_b и всё"),
        ],
    )

    assert [item["name"] for item in store.get_catalog_items("cat-1", q="100%")] == [
        "percent/repo"
    ]
    assert [item["name"] for item in store.get_catalog_items("cat-1", q="a_b")] == [
        "under/repo"
    ]


def test_get_catalog_items_limit_and_offset(store: Store) -> None:
    seed_catalog(
        store, items=[catalog_item(f"repo/{n}", stars=n * 10) for n in range(1, 6)]
    )

    first = store.get_catalog_items("cat-1", limit=2)
    second = store.get_catalog_items("cat-1", limit=2, offset=2)

    assert [item["name"] for item in first] == ["repo/5", "repo/4"]
    assert [item["name"] for item in second] == ["repo/3", "repo/2"]
    assert store.count_catalog_items("cat-1") == 5


def test_list_catalogs_newest_first_with_items_count(store: Store) -> None:
    """Список каталогов: ``created_at DESC``, элементы посчитаны в том же SELECT."""
    seed_catalog(store, catalog_id="cat-1", items=[catalog_item("a/x")])
    seed_catalog(
        store, catalog_id="cat-2", items=[catalog_item("b/y"), catalog_item("c/z")]
    )
    # created_at у обеих строк — одна секунда: время задаём явно, иначе
    # порядок держал бы tie-breaker id, а тест проверял бы не то
    conn_of(store).execute(
        "UPDATE catalogs SET created_at = ? WHERE id = ?",
        ("2026-10-19T10:00:00Z", "cat-1"),
    )
    conn_of(store).execute(
        "UPDATE catalogs SET created_at = ? WHERE id = ?",
        ("2026-10-19T11:00:00Z", "cat-2"),
    )

    catalogs = store.list_catalogs()

    assert [catalog["id"] for catalog in catalogs] == ["cat-2", "cat-1"]
    assert [catalog["items_count"] for catalog in catalogs] == [2, 1]
    assert catalogs[0]["queries"] == ["test impact analysis"]


def test_delete_catalog_removes_catalog_and_items(store: Store) -> None:
    seed_catalog(store, catalog_id="cat-1")
    seed_catalog(store, catalog_id="cat-2")

    store.delete_catalog("cat-1")

    assert store.get_catalog("cat-1") is None
    assert store.count_catalog_items("cat-1") == 0
    assert store.get_catalog("cat-2") is not None
    assert count(store, "catalogs") == 1
    assert count(store, "catalog_items") == 1
