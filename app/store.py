"""SQLite-хранилище GapAudit: схема, миграции, API наблюдений (шаг 2, DEVPLAN §5).

Слой: `app/` (см. §3) — только здесь импортируется `sqlite3`; `core/` про БД
ничего не знает (AGENTS.md, gotcha 19). В этом шаге Store — библиотечный:
ни `cli/`, ни `app/web.py` его не вызывают, первая запись из приложения —
в шаге 3 (`app/jobs.py`).

Использование::

    with Store("data/gapaudit.db") as store:
        store.record_observation(check_result, run_id="2026-10-19T14:23:05Z")
        latest = store.get_latest_observations()

Правила модуля:

* одно соединение на Store, никаких глобальных соединений;
* `isolation_level=None` (автокоммит) + явные транзакции ``with _transaction``:
  неявных транзакций нет, каждая многошаговая операция закрывает свою;
* ISO-строки времени — только через :func:`utcnow_iso` или переданные явно;
* ничего не печатает: ошибки — исключения наружу.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

__all__ = [
    "CATALOG_ALIVE_DAYS",
    "SCHEMA_VERSION",
    "Store",
    "SqliteCache",
    "migrate",
    "migrate_1",
    "migrate_2",
    "utcnow_iso",
]

#: Горизонт «живого» проекта в каталоге: push не старше стольких дней.
#: Тот же смысл, что у ``core.catalog.FRESH_DAYS`` (там — отчёт по свежести),
#: но граница живёт здесь: фильтр ``alive`` экрана «Каталоги» — это SQL.
CATALOG_ALIVE_DAYS = 90

#: Текущая версия схемы. Больше, чем в meta.schema_version, — БД новее кода.
#: 2 (шаг 3c): ``catalog_items.url`` — ссылка на проект из источника находки.
SCHEMA_VERSION = 2

_OBSERVATION_COLUMNS = (
    "id, run_id, name, source, version, released_at, adoption_month, "
    "adoption_week, flag, flag_value, verdict, measured_at"
)

_EVENT_COLUMNS = "id, run_id, kind, name, detail, at"

#: Поля, разрешённые для update_job(); всё остальное — ValueError.
_JOB_UPDATABLE = (
    "status",
    "progress",
    "message",
    "started_at",
    "finished_at",
    "error",
    "pid",
)


def _decode_params(raw: Any) -> Any:
    """params из TEXT-колонки обратно в dict/list.

    Писали JSON (:meth:`Store.create_job`), но читать чужую/битую строку
    обязаны без исключения: поле params не должно ронять чтение задачи.
    Непарсящийся текст остаётся строкой — как есть.
    """
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(decoded, (dict, list)):
            return decoded
    return raw


def utcnow_iso() -> str:
    """Текущий момент в ISO 8601 UTC: секунды точности, ``Z`` в конце.

    Пример: ``2026-10-19T14:23:05Z``. Фиксированный формат и длина (20
    символов) означают, что лексикографическая сортировка строк — это
    хронологическая сортировка: на ней построены выборки `measured_at DESC`.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _days_ago_date(days: int) -> str:
    """Дата ``YYYY-MM-DD`` ровно ``days`` дней назад (UTC).

    Граница фильтра ``alive`` каталога: ``catalog_items.pushed`` хранится
    датой без времени (так его отдаёт GitHub), поэтому сравнивать надо с такой
    же датой — ISO-метка времени в этой колонке дала бы ложные сравнения.
    """
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))


def _decode_json_field(raw: Any, default: Any) -> Any:
    """JSON-поле из TEXT-колонки; битая строка — ``default``.

    Чтение каталога не должно падать из-за чужой/испорченной строки: экран
    списка лучше покажет пустые фразы, чем «500» на всю страницу.
    """
    if raw is None:
        return default
    if isinstance(raw, type(default)):
        return raw
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return decoded if isinstance(decoded, type(default)) else default


def _like_escape(text: str) -> str:
    """Экранировать ``%``/``_``/``\\`` для ``LIKE … ESCAPE '\\'``.

    Поиск на экране «Пакеты» — по подстроке; без экранирования запрос «100%»
    вернул бы всё, а «a_b» совпало бы с «axb».
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Явная транзакция: ``BEGIN`` … ``COMMIT``, при исключении — ``ROLLBACK``.

    ``isolation_level=None`` отключает неявные транзакции sqlite3, поэтому
    атомарность многошаговых операций обеспечивается только явным ``BEGIN``;
    ``with conn`` в конце коммитит открытую транзакцию или откатывает её.
    """
    conn.execute("BEGIN")
    with conn:
        yield conn


def _last_id(cursor: sqlite3.Cursor) -> int:
    """id вставленной строки.

    ``lastrowid`` типизирован как ``int | None``: None после INSERT
    практически невозможен, но молчаливый 0 сломал бы перенаправление id —
    поэтому такое считаем ошибкой.
    """
    if cursor.lastrowid is None:
        raise RuntimeError("sqlite3 не вернул lastrowid после INSERT")
    return int(cursor.lastrowid)


def migrate_1(conn: sqlite3.Connection) -> None:
    """Миграция 0 → 1: все таблицы схемы DEVPLAN §5 в одной транзакции."""
    statements = (
        """CREATE TABLE IF NOT EXISTS packages(
               name TEXT PRIMARY KEY,
               source TEXT,
               url TEXT,
               summary TEXT,
               first_seen TEXT NOT NULL,
               last_seen TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS observations(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               run_id TEXT NOT NULL,
               name TEXT NOT NULL,
               source TEXT,
               version TEXT,
               released_at TEXT,
               adoption_month INTEGER,
               adoption_week INTEGER,
               flag TEXT,
               flag_value INTEGER,
               verdict TEXT,
               measured_at TEXT NOT NULL
           )""",
        """CREATE INDEX IF NOT EXISTS idx_observations_name_measured
               ON observations(name, measured_at DESC)""",
        "CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id)",
        """CREATE TABLE IF NOT EXISTS events(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               run_id TEXT NOT NULL,
               kind TEXT NOT NULL,
               name TEXT NOT NULL,
               detail TEXT,
               at TEXT NOT NULL
           )""",
        "CREATE INDEX IF NOT EXISTS idx_events_at ON events(at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_events_kind_at ON events(kind, at DESC)",
        """CREATE TABLE IF NOT EXISTS cache(
               key TEXT PRIMARY KEY,
               fetched_at REAL NOT NULL,
               value TEXT NOT NULL
           )""",
        """CREATE TABLE IF NOT EXISTS jobs(
               id TEXT PRIMARY KEY,
               kind TEXT NOT NULL,
               params TEXT NOT NULL,
               status TEXT NOT NULL,
               progress REAL DEFAULT 0,
               message TEXT,
               created_at TEXT NOT NULL,
               started_at TEXT,
               finished_at TEXT,
               log_path TEXT,
               error TEXT,
               pid INTEGER
           )""",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at DESC)",
        """CREATE TABLE IF NOT EXISTS catalogs(
               id TEXT PRIMARY KEY,
               queries TEXT NOT NULL,
               created_at TEXT NOT NULL,
               stats TEXT,
               path TEXT
           )""",
        """CREATE TABLE IF NOT EXISTS catalog_items(
               catalog_id TEXT NOT NULL,
               name TEXT NOT NULL,
               source TEXT,
               stars INTEGER,
               lang TEXT,
               pushed TEXT,
               desc TEXT,
               PRIMARY KEY(catalog_id, name)
           )""",
        """CREATE TABLE IF NOT EXISTS schedules(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               name TEXT NOT NULL UNIQUE,
               kind TEXT NOT NULL,
               params TEXT NOT NULL,
               cron TEXT NOT NULL,
               enabled INTEGER NOT NULL DEFAULT 1,
               last_run TEXT
           )""",
    )
    with _transaction(conn):
        for statement in statements:
            conn.execute(statement)
        # Своя версия, а не SCHEMA_VERSION: номер пишет та миграция, к которой
        # он относится (с 3c версия схемы 2, и migrate_1, записав «2», оставил
        # бы БД без колонки url, но с видом «всё применено»).
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )


def migrate_2(conn: sqlite3.Connection) -> None:
    """Миграция 1 → 2: колонка ``catalog_items.url`` (шаг 3c).

    ``ALTER TABLE … ADD COLUMN`` в SQLite добавляет колонку без перестройки
    таблицы, старые строки получают NULL. Миграция идемпотентна: колонка уже
    есть — выходим. Это не перестраховка, а условие повторного старта: если
    процесс упал между ``ALTER`` и записью ``meta.schema_version``, версия
    осталась 1, миграция запустится снова, и без проверки SQLite ответил бы
    «duplicate column name: url» — приложение не открылось бы вовсе.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(catalog_items)")}
    if "url" in columns:
        return
    with _transaction(conn):
        conn.execute("ALTER TABLE catalog_items ADD COLUMN url TEXT")


#: Реестр миграций: номер версии → функция. Новая миграция = новая запись.
_MIGRATIONS = {1: migrate_1, 2: migrate_2}


def migrate(conn: sqlite3.Connection) -> None:
    """Привести БД к :data:`SCHEMA_VERSION`, по порядку прогоняя ``migrate_N``.

    Ничего не печатает. ``meta.schema_version`` читается как строка (TEXT в
    схеме), отсутствие записи — версия 0 (свежая БД). Версия больше
    ``SCHEMA_VERSION`` — ``RuntimeError``: код старее БД, доращивать наоборот
    мы не умеем.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        version = 0
    else:
        try:
            version = int(row[0])
        except (TypeError, ValueError):
            raise RuntimeError(f"некорректная meta.schema_version: {row[0]!r}") from None
    if version > SCHEMA_VERSION:
        raise RuntimeError("БД создана более новой версией приложения")
    while version < SCHEMA_VERSION:
        step = version + 1
        migration = _MIGRATIONS.get(step)
        if migration is None:
            raise RuntimeError(f"нет миграции со степенью {step}")
        migration(conn)
        # Версия фиксируется после каждой ступени, а не только в migrate_1:
        # без этого БД, пришедшая с version=1, каждый старт заново прогоняла бы
        # migrate_2 (ALTER TABLE в SQLite не повторяем, но полагаться на это
        # нельзя — миграции пишутся по одной и не обязаны быть идемпотентными).
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(step),),
        )
        version = step


class Store:
    """SQLite-хранилище: одно соединение, явные транзакции, без глобалов.

    Открывается контекстным менеджером; :meth:`close` идемпотентен, поэтому
    закрытие дважды (явно и через ``__exit__``) безопасно.
    """

    def __init__(self, db_path: str | Path = ":memory:"):
        self.db_path = str(db_path)
        self.conn: sqlite3.Connection | None = None
        # Единый замок на тела публичных методов: JobQueue зовёт Store из
        # воркеров, а sqlite3.Connection не потокобезопасен (AGENTS gotcha 21).
        self._lock = threading.Lock()

    # --- жизненный цикл ----------------------------------------------------

    def __enter__(self) -> "Store":
        conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self.conn = conn
        try:
            if self.db_path != ":memory:":
                # WAL нужен файловой БД (чтение не блокирует писателя);
                # в :memory: он не применяется — пропускаем.
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            migrate(conn)
            conn.row_factory = sqlite3.Row
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.conn is not None:
            try:
                if exc_type is None:
                    if self.conn.in_transaction:
                        self.conn.commit()
                elif self.conn.in_transaction:
                    self.conn.rollback()
            finally:
                self.close()

    def close(self) -> None:
        """Закрыть соединение. Идемпотентно: повторный вызов — no-op."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def ping(self) -> bool:
        """True, если соединение открыто и ``SELECT 1`` проходит."""
        with self._lock:
            if self.conn is None:
                return False
            try:
                self.conn.execute("SELECT 1").fetchone()
            except sqlite3.Error:
                return False
            return True

    # --- внутреннее --------------------------------------------------------

    def _require_conn(self) -> sqlite3.Connection:
        """Соединение или понятная ошибка, если Store не открыт/закрыт."""
        if self.conn is None:
            raise RuntimeError("Store не открыт или закрыт: with Store(path) as store")
        return self.conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Транзакция на соединении Store (см. :func:`_transaction`)."""
        with _transaction(self._require_conn()) as conn:
            yield conn

    @staticmethod
    def _upsert_package(
        conn: sqlite3.Connection, observation: dict, *, name: str, moment: str
    ) -> None:
        """Вставить/обновить строку в packages.

        Пакет с ``src == '-'`` (не найден ни в PyPI, ни в npm) строку в
        packages не создаёт — в observations он попадает как есть.
        """
        source = observation.get("src")
        if not source or source == "-":
            return
        conn.execute(
            """INSERT INTO packages(name, source, url, summary, first_seen, last_seen)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   source = COALESCE(excluded.source, packages.source),
                   url = COALESCE(excluded.url, packages.url),
                   summary = COALESCE(excluded.summary, packages.summary),
                   last_seen = excluded.last_seen""",
            (
                name,
                source,
                observation.get("home"),
                observation.get("summary"),
                moment,
                moment,
            ),
        )

    @staticmethod
    def _insert_observation(
        conn: sqlite3.Connection,
        observation: dict,
        *,
        name: str,
        run_id: str,
        moment: str,
        keyword_label: str,
    ) -> int:
        """INSERT одной строки observations; вернуть её id."""
        cursor = conn.execute(
            """INSERT INTO observations(
                   run_id, name, source, version, released_at,
                   adoption_month, adoption_week,
                   flag, flag_value, verdict, measured_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                name,
                observation.get("src"),
                observation.get("ver"),
                observation.get("date"),
                observation.get("month"),
                observation.get("week"),
                keyword_label,
                1 if observation.get("flag") else 0,
                observation.get("verdict"),
                moment,
            ),
        )
        return _last_id(cursor)

    def _record_batch(
        self,
        observations: list[dict],
        *,
        run_id: str,
        measured_at: str | None,
        keyword_label: str,
    ) -> list[int]:
        """Общий путь record_observation/record_observations: одна транзакция.

        ``measured_at`` вычисляется один раз на весь вызов, поэтому весь
        батч получает одну метку времени замера.
        """
        moment = measured_at if measured_at is not None else utcnow_iso()
        ids: list[int] = []
        with self._tx() as conn:
            for observation in observations:
                name = observation.get("name")
                if not name:
                    raise ValueError("observation['name'] пуст — наблюдение не записано")
                self._upsert_package(conn, observation, name=name, moment=moment)
                ids.append(
                    self._insert_observation(
                        conn,
                        observation,
                        name=name,
                        run_id=run_id,
                        moment=moment,
                        keyword_label=keyword_label,
                    )
                )
        return ids

    # --- запись ------------------------------------------------------------

    def record_observation(
        self,
        observation: dict,
        *,
        run_id: str,
        measured_at: str | None = None,
        keyword_label: str = "MCP",
    ) -> int:
        """Записать один результат ``core.packages.check()`` в одной транзакции.

        Принимает словарь вида ``{name, src, ver, date, summary, home, flag,
        mcp, month, week, verdict}``: сначала upsert в ``packages`` (кроме
        ``src == '-'``), затем INSERT в ``observations``. Возвращает id
        наблюдения. Пустое ``name`` — ``ValueError``.
        """
        with self._lock:
            return self._record_batch(
                [observation],
                run_id=run_id,
                measured_at=measured_at,
                keyword_label=keyword_label,
            )[0]

    def record_observations(
        self,
        observations: list[dict],
        *,
        run_id: str,
        measured_at: str | None = None,
        keyword_label: str = "MCP",
    ) -> list[int]:
        """Батч наблюдений в одной транзакции: либо записались все, либо ни одного.

        Один ``utcnow_iso()`` на весь батч — UI показывает одно время замера.
        """
        with self._lock:
            return self._record_batch(
                observations,
                run_id=run_id,
                measured_at=measured_at,
                keyword_label=keyword_label,
            )

    def record_event(
        self,
        *,
        run_id: str,
        kind: str,
        name: str,
        detail: str | None = None,
        at: str | None = None,
    ) -> int:
        """Записать одно событие ``core.diffing``; вернуть id в events.

        ``kind``: new | missing | release | flag_appeared | flag_removed |
        adoption_change | verdict_change.
        """
        # Делегирует приватному _record_events, а не record_events: иначе
        # замок взялся бы дважды (Lock не реентерабельный — был бы deadlock).
        with self._lock:
            return self._record_events(
                [{"kind": kind, "name": name, "detail": detail}],
                run_id=run_id,
                at=at,
            )[0]

    def record_events(
        self,
        events: list[dict],
        *,
        run_id: str,
        at: str | None = None,
    ) -> list[int]:
        """Батч событий в одной транзакции: ``events[i] = {kind, name, detail}``.

        Одна метка ``at`` на весь батч (как и у наблюдений).
        """
        with self._lock:
            return self._record_events(events, run_id=run_id, at=at)

    def _record_events(
        self,
        events: list[dict],
        *,
        run_id: str,
        at: str | None,
    ) -> list[int]:
        """Тело record_events без замка (вызывается уже под ``self._lock``)."""
        moment = at if at is not None else utcnow_iso()
        ids: list[int] = []
        with self._tx() as conn:
            for event in events:
                cursor = conn.execute(
                    "INSERT INTO events(run_id, kind, name, detail, at) VALUES(?, ?, ?, ?, ?)",
                    (
                        run_id,
                        event.get("kind"),
                        event.get("name"),
                        event.get("detail"),
                        moment,
                    ),
                )
                ids.append(_last_id(cursor))
        return ids

    # --- чтение -----------------------------------------------------------

    def get_latest_observations(self, names: list[str] | None = None) -> list[dict]:
        """Последнее наблюдение по каждому имени (``measured_at DESC, id DESC``).

        ``names=None`` — все имена, у которых есть хотя бы одна observation.
        ``names=[]`` — пусто. Для переданных имён порядок списка сохраняется,
        результат — те же ключи, что в ``check()`` плюс ``measured_at``/``run_id``.
        """
        with self._lock:
            conn = self._require_conn()
            wanted: list[str] = [] if names is None else list(dict.fromkeys(names))
            if names is not None and not wanted:
                return []
            window = (
                f"SELECT {_OBSERVATION_COLUMNS}, "
                "ROW_NUMBER() OVER (PARTITION BY name "
                "ORDER BY measured_at DESC, id DESC) AS rn "
                "FROM observations"
            )
            params: Sequence[Any]
            if names is None:
                params = ()
                sql = f"SELECT * FROM ({window}) WHERE rn = 1 ORDER BY name"
            else:
                placeholders = ", ".join("?" for _ in wanted)
                params = tuple(wanted)
                sql = f"SELECT * FROM ({window} WHERE name IN ({placeholders})) WHERE rn = 1"
            latest: dict[str, dict] = {}
            for row in conn.execute(sql, params):
                record = dict(row)
                record.pop("rn", None)
                latest[record["name"]] = record
        if names is None:
            return list(latest.values())
        return [latest[name] for name in wanted if name in latest]

    def get_observations_history(self, name: str, *, limit: int = 50) -> list[dict]:
        """Все наблюдения имени, новые сверху — для мини-графика adoption."""
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                f"SELECT {_OBSERVATION_COLUMNS} FROM observations "
                "WHERE name = ? ORDER BY measured_at DESC, id DESC LIMIT ?",
                (name, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _events_where(
        since: str | None, kind: str | None, name: str | None
    ) -> tuple[str, list[Any]]:
        """WHERE для ``events`` по трём фильтрам — один текст на выборку и счёт.

        Общий построитель, а не две копии: ``GET /api/events`` отдаёт страницу и
        ``total``, и если фильтры разойдутся, счётчик начнёт врать («3 события»,
        а в списке 2). Замок не берёт: вызывается уже под ``self._lock``.
        """
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("at >= ?")
            params.append(since)
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if name is not None:
            where.append("name = ?")
            params.append(name)
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def get_events(
        self,
        *,
        since: str | None = None,
        kind: str | None = None,
        name: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        """События для экрана «Мониторинг»: по убыванию ``at``, с фильтрами.

        ``since`` — ISO-строка включается в выборку (``at >= since``);
        ``name`` — точное имя пакета (шаг 3a: раньше фильтр по имени жил в
        ``app/web.py`` и читал все события ради карточки одного пакета).
        ``offset`` — пагинация экрана «Мониторинг» (порядок ``at DESC, id DESC``
        стабилен между страницами).
        """
        with self._lock:
            conn = self._require_conn()
            clause, params = self._events_where(since, kind, name)
            sql = (
                f"SELECT {_EVENT_COLUMNS} FROM events{clause} "
                "ORDER BY at DESC, id DESC LIMIT ? OFFSET ?"
            )
            params.extend([limit, offset])
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_events_filtered(
        self,
        *,
        since: str | None = None,
        kind: str | None = None,
        name: str | None = None,
    ) -> int:
        """Сколько событий под теми же фильтрами — ``total`` для пагинации."""
        with self._lock:
            conn = self._require_conn()
            clause, params = self._events_where(since, kind, name)
            return int(
                conn.execute(f"SELECT COUNT(*) FROM events{clause}", params).fetchone()[0]
            )

    def get_package(self, name: str) -> dict | None:
        """Одна строка ``packages`` или None, если пакета нет."""
        with self._lock:
            conn = self._require_conn()
            row = conn.execute("SELECT * FROM packages WHERE name = ?", (name,)).fetchone()
        return dict(row) if row is not None else None

    def list_packages(
        self, *, limit: int = 500, only_with_observations: bool = False
    ) -> list[dict]:
        """Список пакетов (экран «Пакеты»): по ``last_seen DESC``, с limit.

        ``only_with_observations=True`` — только те, у кого есть observation.
        """
        with self._lock:
            conn = self._require_conn()
            sql = "SELECT * FROM packages"
            if only_with_observations:
                sql += (
                    " WHERE EXISTS "
                    "(SELECT 1 FROM observations o WHERE o.name = packages.name)"
                )
            sql += " ORDER BY last_seen DESC LIMIT ?"
            rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _packages_where(q: str) -> tuple[str, list[Any]]:
        """WHERE поиска по имени/описанию — один текст на страницу и счёт.

        Пустой ``q`` — пустое условие (все пакеты). Подстрока сравнивается
        через ``LOWER`` с обеих сторон: регистр должен игнорироваться и для
        имени, и для summary, а не только для ASCII-имён.
        """
        text = (q or "").strip()
        if not text:
            return "", []
        pattern = "%" + _like_escape(text.lower()) + "%"
        return (
            " WHERE (LOWER(name) LIKE ? ESCAPE '\\'"
            " OR LOWER(COALESCE(summary, '')) LIKE ? ESCAPE '\\')",
            [pattern, pattern],
        )

    def search_packages(
        self, q: str = "", *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """Поиск пакетов по имени и описанию (экран «Пакеты», шаг 3a).

        ``q`` — подстрока без учёта регистра (``LOWER`` с обеих сторон: в SQLite
        ``LIKE`` регистронезависим только для ASCII). Пустой ``q`` — все пакеты,
        поэтому пагинация работает и без поиска. Порядок — ``last_seen DESC,
        name``: он же у ``list_packages``, и ``offset`` даёт стабильные страницы.

        Символы ``%``/``_`` в запросе экранируются: без этого строка «100%»
        нашла бы всё подряд, а поиск по подстроке молча превратился бы в
        поиск по шаблону.
        """
        with self._lock:
            conn = self._require_conn()
            clause, params = self._packages_where(q)
            sql = (
                "SELECT name, source, summary, last_seen FROM packages"
                f"{clause} ORDER BY last_seen DESC, name LIMIT ? OFFSET ?"
            )
            params.extend([limit, offset])
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_packages_filtered(self, q: str = "") -> int:
        """Сколько пакетов под тем же поиском — ``total`` для пагинации."""
        with self._lock:
            conn = self._require_conn()
            clause, params = self._packages_where(q)
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM packages{clause}", params
                ).fetchone()[0]
            )

    def count_observations(self) -> int:
        """Сколько строк в observations."""
        with self._lock:
            conn = self._require_conn()
            return int(conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0])

    def count_events(self) -> int:
        """Сколько строк в events."""
        with self._lock:
            conn = self._require_conn()
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    # --- задачи (таблица jobs, шаг 3a) ------------------------------------

    def create_job(self, job: dict) -> None:
        """INSERT в ``jobs``; возвращает None.

        Принимает словарь с ключами ``id, kind, params, status, progress,
        message, created_at, log_path`` (``created_at`` и ``message`` при
        отсутствии берутся дефолтными). ``params`` — dict/list: кладётся в
        TEXT как JSON (``str`` — как есть). Поля ``started_at``,
        ``finished_at``, ``error``, ``pid`` остаются NULL — их заполняет
        :meth:`update_job` по мере жизни задачи.
        """
        params = job.get("params")
        if isinstance(params, (dict, list)):
            params_text = json.dumps(params, ensure_ascii=False)
        elif params is None:
            params_text = "{}"
        else:
            params_text = str(params)
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                """INSERT INTO jobs(
                       id, kind, params, status, progress, message,
                       created_at, log_path
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.get("id"),
                    job.get("kind"),
                    params_text,
                    job.get("status"),
                    job.get("progress", 0.0),
                    job.get("message", ""),
                    job.get("created_at") or utcnow_iso(),
                    job.get("log_path"),
                ),
            )

    def update_job(self, job_id: str, **fields: Any) -> None:
        """UPDATE ``jobs`` по разрешённым полям; возвращает None.

        Разрешено: ``status, progress, message, started_at, finished_at,
        error, pid`` — всё остальное ``ValueError`` (нельзя незаметно
        переписать ``id`` или ``params``). Несуществующий ``job_id`` —
        молча no-op (обновилось 0 строк): жизненный цикл задачи гоняется
        воркерами и отменой параллельно, и «опоздавший» апдейт не должен
        ронять ни того, ни другого.
        """
        unknown = [name for name in fields if name not in _JOB_UPDATABLE]
        if unknown:
            raise ValueError(
                "недопустимые поля jobs: " + ", ".join(sorted(unknown))
            )
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values())
        values.append(job_id)
        with self._lock:
            conn = self._require_conn()
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)

    def get_job(self, job_id: str) -> dict | None:
        """Строка ``jobs`` (params — обратно в dict/list) или None."""
        with self._lock:
            conn = self._require_conn()
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["params"] = _decode_params(record.get("params"))
        return record

    def list_jobs(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict]:
        """Задачи по ``created_at DESC`` (tie-breaker ``id``), с limit.

        ``status`` — точное значение статуса (queued/running/done/...);
        ``None`` — все.
        """
        with self._lock:
            conn = self._require_conn()
            sql = "SELECT * FROM jobs"
            params: list[Any] = []
            if status is not None:
                sql += " WHERE status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC, id LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        return [
            {**dict(row), "params": _decode_params(dict(row).get("params"))}
            for row in rows
        ]

    # --- каталоги (таблицы catalogs/catalog_items, шаг 3b) -----------------

    def record_catalog(
        self,
        *,
        catalog_id: str,
        queries: list[str],
        stats: dict,
        path: str | None = None,
    ) -> None:
        """UPSERT строки ``catalogs``: фразы и сводка — JSON-текстом.

        ``created_at`` — момент записи, а не момент создания задачи: каталог
        появляется в БД, когда задача завершилась (пишет хук lifespan, а не
        обработчик HTTP). Повторная запись того же ``id`` (перезапуск хука,
        повторное завершение задачи) обновляет строку: таблица описывает
        каталог, а не историю его сборок.

        ``path`` — файл результата задачи; ``None`` его не затирает, иначе
        повторная запись без аргумента стёрла бы путь к исходнику.
        """
        moment = utcnow_iso()
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                """INSERT INTO catalogs(id, queries, created_at, stats, path)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       queries = excluded.queries,
                       created_at = excluded.created_at,
                       stats = excluded.stats,
                       path = COALESCE(excluded.path, catalogs.path)""",
                (
                    catalog_id,
                    json.dumps(list(queries), ensure_ascii=False),
                    moment,
                    json.dumps(stats, ensure_ascii=False),
                    path,
                ),
            )

    def record_catalog_items(self, catalog_id: str, items: list[dict]) -> int:
        """Батч ``INSERT OR REPLACE`` в ``catalog_items``: вернуть число строк.

        Имя строки берётся из ``name``, а при его отсутствии — из ``id``:
        CLI каталога кладёт проект в ``id`` (``owner/repo`` у GitHub), и
        переименовывать поле ради БД значило бы расходиться с ``build_catalog``.
        Строки без имени пропускаются — PK таблицы ``(catalog_id, name)``,
        безымянная строка не стыкуется ни с фильтром, ни с экспортом.

        ``sources`` (список источников после склейки дублей в
        ``core.catalog.dedupe``) важнее одиночного ``source``: один проект
        живёт и на GitHub, и в npm, а колонка одна — она хранит их через
        запятую, и экран «Источники» показывает всё найденное.

        ``url`` — ссылка на проект от источника находки (миграция 2, шаг 3c):
        GitHub отдаёт ``html_url``, npm — ``links.npm``, реестр — ``url``
        сервера (у записей реестра он пустой). Пустая строка хранится как
        NULL: «ссылки нет» и «ссылка пустая» — одно состояние, и экран не
        должен строить ссылку на пустой адрес.
        """
        written = 0
        with self._lock, self._tx() as conn:
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("id")
                if not name:
                    continue
                sources = [str(source) for source in (item.get("sources") or []) if source]
                source = ",".join(sources) if sources else item.get("source")
                conn.execute(
                    """INSERT OR REPLACE INTO catalog_items(
                           catalog_id, name, source, stars, lang, pushed, desc, url
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        catalog_id,
                        str(name),
                        source,
                        item.get("stars"),
                        item.get("lang"),
                        item.get("pushed"),
                        item.get("desc"),
                        item.get("url") or None,
                    ),
                )
                written += 1
        return written

    @staticmethod
    def _catalog_record(row: sqlite3.Row) -> dict:
        """Строка ``catalogs`` с ``queries``/``stats``, развёрнутыми из JSON."""
        record = dict(row)
        record["queries"] = _decode_json_field(record.get("queries"), [])
        record["stats"] = _decode_json_field(record.get("stats"), {})
        return record

    def get_catalog(self, catalog_id: str) -> dict | None:
        """Один каталог (id, queries, created_at, stats, path) или ``None``."""
        with self._lock:
            conn = self._require_conn()
            row = conn.execute(
                "SELECT * FROM catalogs WHERE id = ?", (catalog_id,)
            ).fetchone()
        return self._catalog_record(row) if row is not None else None

    def list_catalogs(self, *, limit: int = 100) -> list[dict]:
        """Каталоги по ``created_at DESC`` (tie-breaker ``id``) + ``items_count``.

        Счётчик элементов считается подзапросом в том же SELECT: экран
        «Каталоги» рисует список целиком, и отдельный COUNT на каждый каталог
        превратил бы один запрос в сотню.
        """
        with self._lock:
            conn = self._require_conn()
            rows = conn.execute(
                """SELECT c.id, c.queries, c.created_at, c.stats, c.path,
                          (SELECT COUNT(*) FROM catalog_items i
                            WHERE i.catalog_id = c.id) AS items_count
                     FROM catalogs c
                    ORDER BY c.created_at DESC, c.id
                    LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._catalog_record(row) for row in rows]

    @staticmethod
    def _catalog_items_where(
        catalog_id: str,
        *,
        alive: bool | None = None,
        lang: str | None = None,
        min_stars: int | None = None,
        q: str | None = None,
    ) -> tuple[str, list[Any]]:
        """WHERE элементов каталога — один текст на страницу и на счёт.

        Общий построитель, а не две копии: ``GET /api/catalogs/{id}/items``
        отдаёт страницу и ``total``, и разошедшиеся фильтры дали бы «5
        элементов», из которых на экране три.

        ``alive=True`` — push не старше :data:`CATALOG_ALIVE_DAYS` дней,
        ``alive=False`` — старше. Строка без ``pushed`` (npm/реестр его не
        отдают) не попадает ни в одну из этих групп: сравнение с NULL в SQL
        ложно, и «живой» — это подтверждённая свежесть, а не её отсутствие.
        Видна такая строка под фильтром «Все».

        ``min_stars`` сравнивается с ``COALESCE(stars, 0)``: у строк без
        звёзд «звёзд меньше порога» неизвестно, и ``min_stars=0`` на экране
        означает «без ограничения», а не «только строки со звёздами»
        (``stars >= 0`` отбросил бы NULL-ы).
        """
        where = ["catalog_id = ?"]
        params: list[Any] = [catalog_id]
        if alive is True:
            where.append("pushed >= ?")
            params.append(_days_ago_date(CATALOG_ALIVE_DAYS))
        elif alive is False:
            where.append("pushed < ?")
            params.append(_days_ago_date(CATALOG_ALIVE_DAYS))
        if lang:
            where.append("lang = ?")
            params.append(lang)
        if min_stars is not None:
            where.append("COALESCE(stars, 0) >= ?")
            params.append(min_stars)
        text = (q or "").strip()
        if text:
            pattern = "%" + _like_escape(text.lower()) + "%"
            where.append(
                "(LOWER(name) LIKE ? ESCAPE '\\'"
                " OR LOWER(COALESCE(desc, '')) LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern])
        return " WHERE " + " AND ".join(where), params

    def get_catalog_items(
        self,
        catalog_id: str,
        *,
        alive: bool | None = None,
        lang: str | None = None,
        min_stars: int | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Элементы каталога с фильтрами: ``stars DESC`` (без звёзд — в конце).

        ``ORDER BY … NULLS LAST`` поддерживается SQLite 3.30+ (здесь 3.39.4);
        ``name`` вторым ключом делает страницы стабильными: без него две
        строки с одинаковыми звёздами могли меняться местами между запросами,
        и пагинация показывала бы дубль вместо второй страницы.
        """
        with self._lock:
            conn = self._require_conn()
            clause, params = self._catalog_items_where(
                catalog_id, alive=alive, lang=lang, min_stars=min_stars, q=q
            )
            sql = (
                "SELECT name, source, stars, lang, pushed, desc, url FROM catalog_items"
                f"{clause} ORDER BY stars DESC NULLS LAST, name LIMIT ? OFFSET ?"
            )
            params.extend([limit, offset])
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_catalog_items(
        self,
        catalog_id: str,
        *,
        alive: bool | None = None,
        lang: str | None = None,
        min_stars: int | None = None,
        q: str | None = None,
    ) -> int:
        """Сколько элементов под теми же фильтрами — ``total`` для пагинации.

        Ключевые аргументы явные (а не ``**filters``): опечатка в имени
        фильтра тогда падает ``TypeError`` в тесте, а не тихо считает всё
        подряд, пока страница отдаёт отфильтрованное.
        """
        with self._lock:
            conn = self._require_conn()
            clause, params = self._catalog_items_where(
                catalog_id, alive=alive, lang=lang, min_stars=min_stars, q=q
            )
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM catalog_items{clause}", params
                ).fetchone()[0]
            )

    def delete_catalog(self, catalog_id: str) -> None:
        """Удалить каталог и его элементы одной транзакцией.

        Элементы — первыми: без внешнего ключа (схема §5 его не задаёт)
        обратный порядок оставил бы их сиротами навсегда.
        """
        with self._lock, self._tx() as conn:
            conn.execute(
                "DELETE FROM catalog_items WHERE catalog_id = ?", (catalog_id,)
            )
            conn.execute("DELETE FROM catalogs WHERE id = ?", (catalog_id,))


class SqliteCache:
    """CacheBackend (protocol ``core.cache``) поверх таблицы ``cache``.

    Совместим с ``core.http.HttpClient(cache=SqliteCache(store))``: ключ —
    как есть (``"GET " + url``), значение — JSON с ``ensure_ascii=False``.
    Пишет в БД сразу (автокоммит), поэтому ``save()`` — no-op.
    """

    def __init__(self, store: Store, ttl_hours: float = 12.0):
        self.store = store
        self.ttl_hours = float(ttl_hours)

    def get(self, key: str) -> tuple[float, Any] | None:
        """``(fetched_at, value)`` или None: записи нет или TTL истёк.

        Просроченная запись НЕ удаляется — как в DictCache/FileCache:
        чистку делает put или clear.
        """
        conn = self.store._require_conn()
        row = conn.execute(
            "SELECT fetched_at, value FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        fetched_at = float(row[0])
        if fetched_at + self.ttl_hours * 3600.0 < time.time():
            return None
        return fetched_at, json.loads(row[1])

    def put(self, key: str, value: Any, fetched_at: float | None = None) -> None:
        """Записать значение атомарно (один UPSERT, без отдельного save)."""
        conn = self.store._require_conn()
        moment = time.time() if fetched_at is None else float(fetched_at)
        conn.execute(
            "INSERT INTO cache(key, fetched_at, value) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "fetched_at = excluded.fetched_at, value = excluded.value",
            (key, moment, json.dumps(value, ensure_ascii=False)),
        )

    def save(self) -> None:
        """No-op: пишем сразу в БД (``isolation_level=None`` — автокоммит)."""
        return None

    def clear(self) -> None:
        """Удалить все записи кэша."""
        conn = self.store._require_conn()
        conn.execute("DELETE FROM cache")
