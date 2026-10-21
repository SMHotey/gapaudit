# GapAudit — план разработки приложения поверх скилла ecosystem-gap-audit

Статус: черновик к согласованию. Дата: 18.10.2026.
Клон: `C:\Users\malik\gapaudit`. Оригинальный скилл: `...\hermes\skills\research\ecosystem-gap-audit` —
**остаётся рабочим и не трогается без нужды**.

## 1. Что делаем и что не делаем

Делаем: локальное приложение (pywebview + FastAPI + SQLite), которое превращает набор
CLI-скриптов в пульт для долгих задач: очередь с прогрессом, накопленная история замеров,
сравнение ниш, каталоги, мониторинг изменений.

Не делаем:

- не переписываем методологию (её источник — SKILL.md, он не меняется);
- не удаляем и не ломаем CLI: все шесть скриптов продолжают работать теми же командами;
- не делаем хостинг, многопользовательский режим, авторизацию, облако — один пользователь, одна машина;
- не делаем SPA со сборкой: серверный HTML + немного JS;
- не храним секреты и не ходим в чужие API иначе, чем сейчас (только публичные эндпоинты).

## 2. Ключевые решения и почему именно так

| Решение | Почему |
|---|---|
| pywebview как оболочка окна | WebView2 на машине есть (155.0.4283.62) — Chromium-движок без Electron и без Node; окно нативное, Python остаётся единственным языком проекта |
| FastAPI + uvicorn в фоновом потоке, окно смотрит на `http://127.0.0.1:<порт>` | Один и тот же бэкенд обслуживает и webview, и обычный браузер (режим отладки). Открыть UI в браузере — бесплатный инструмент разработки; JS-API pywebview для этого не годится |
| Данные — по HTTP, `js_api` — только для системных действий | `js_api` хорош для «открыть папку», «выбрать файл», «версия»; тянуть через него таблицы и прогресс значит изобретать RPC заново |
| SQLite (stdlib) + WAL | Один пользователь, один процесс. Даёт то, чего CLI не может: история замеров и сравнение во времени |
| Каждая задача — отдельный подпроцесс | Падение или отмена не роняет сервер; на Windows процесс убивается чисто; тяжёлые обходы (40 мин) переживают перезапуск окна через чекпоинты |
| Прогресс через SSE, не WebSocket | Односторонний поток, автоматические переподключения, никакой возни с бинарным протоколом |
| Источник истины кода — проект, скилл — синхронизируемая копия | Скилл обязан оставаться автономным (один каталог, никаких внешних импортов), поэтому синхронизация идёт проектом в скилл, а не наоборот |

## 3. Карта проекта

Слои: `core/` — чистые функции (БД, подпроцессов, FastAPI и Jinja2 не знает),
`app/` — приложение (импортирует `core` и `sqlite3`), `web/` — только шаблоны и статика
(Python-кода нет). `cli/` — обёртки над `core`, без БД и без UI.

```
C:\Users\malik\gapaudit\
  core/                 логика метода: чистые функции, никакого print и никаких путей от cwd
    http.py             GET с повторами, троттлингом по хосту и кэшем
    cache.py            кэш с TTL (по умолчанию 12 ч): DictCache/FileCache, с шага 2 — SqliteCache
    registry.py         pypi(), npm(), adoption(), downloads(), npm_search_full()
    github.py           gh-поиск, репозитории организации, verify_target(), age_days()
                        (для Search API вызывающий ставит throttle.set_rate('api.github.com', GITHUB_SEARCH_PER_MINUTE))
    packages.py         check(), check_many(), verdict(), similar_names(), names_from_file()
    catalog.py          build_catalog(), dedupe(), stats(), to_markdown()
    mcp_registry.py     реестр MCP: search() по фразе, servers_page()/list_servers_raw()
                        (сырая страница и её разновидности), list_servers() по курсору
    scan.py             потоковый обход каталога с чекпоинтом и отменой
    diffing.py          diff_observations() -> события (релиз, скачок adoption, появился MCP)
    collectors/         НОВЫЙ слой сигналов (P4/P5): внешние API-коллекторы, каждый —
                        отдельный модуль с инжектируемым транспортом
      __init__.py
      momentum.py        Δ adoption 7/30/90 дней (чистая функция над списком замеров)
      cross_ecosystem.py сравнение PyPI/npm/GitHub по одному «концепту»
      jobs.py            спрос на труд: Adzuna API
      patents.py         патентный импульс: PatentsView API
      funding.py         раунды финансирования: SEC EDGAR (full-text search, Form D)
      search_trends.py   поисковый интерес: Yandex Wordstat и/или Google Trends
    signals.py          агрегация сигналов: gap_score, signals_for_package() — чистые
                        функции (данные передаются параметром, про БД core не знает)
    api_keys.py         API Keys Store: чтение/запись <data_dir>/api_keys.json, права
                        0600, base64-«шифрование», проверка ключа (без sqlite3)
    settings.py         конфиг: пороги, ключевые слова, каталог данных, расписание
  app/                  приложение: импортирует core и sqlite3
    store.py            Store: SQLite-схема, миграции, запись/чтение observations/events/
                        packages/cache. SqliteCache живёт здесь (не в core/).
    jobs.py             очередь, статусы, запуск подпроцессов, отмена, логи
    web.py              FastAPI: create_app, lifespan, миддлварь (token из header/cookie/query),
                        _merge_saved_settings (settings.json при старте), все /api/* эндпоинты;
                        settings_path — в app.state
  web/                  только представление, Python-кода здесь нет
    views/              Jinja2-шаблоны
    static/             CSS, минимальный JS, локальные Chart.js/htmx (без CDN)
  desktop.py            точка входа приложения: поднять uvicorn в потоке, открыть pywebview
  cli/                  те же шесть скриптов; флаги и коды выхода сохраняются
  tools/sync_skill.py   собрать автономную копию для скилла (core + обёртки) и проверить её
  tests/                unit по core, фикстуры «demo-режима», UI-тесты, smoke CLI
  data/                 база, кэш, логи задач, собранные каталоги, api_keys.json
                        (dev-режим, в .gitignore; в сборке — %USERPROFILE%\GapAudit-data\)
  docs/
    API-KEYS.md         инструкции: как получить ключи Adzuna/PatentsView/EDGAR/
                        Yandex Wordstat/Google Trends (официальный alpha и Apify)
    screenshots/        скриншоты проверок шагов (коммитятся)
  DEVPLAN.md            этот файл
```

### Будущий MCP-сервер (P6, опционально)

Тонкий фасад над Store:

- Никакого состояния между вызовами: каждый tool — `store.get_*` или `store.record_*`.
- Транспорт: stdio для локального Cursor/Claude Desktop (сессия = время жизни процесса).
  Streamable HTTP — не наш случай (см. §1: без хостинга).
- `run_id` уже первоклассное понятие в observations/events — MCP-инструментам
  `recent_events(run_id=...)` и `observations_for_run(run_id)` ничего не надо менять в схеме.
- Мутирующие tools (`check_and_record`, `submit_job`) принимают `run_id` от клиента;
  read-only tools (`check_package`, `recent_events`) без побочных эффектов.
- Сессионность MCP — свойство транспорта (решает библиотека `mcp`), не Store.
  Никаких `session_id` в таблицах.

## 4. Шаг 0 — рефакторинг в `core` (обязателен, без него дальше нельзя)

Скрипты сейчас печатают в stdout и пишут в `raw/` относительно текущей папки. Веб-слой не должен
запускать их подпроцессом и разбирать вывод. Что делаем:

1. Переносим логику в `core` без изменения поведения: функции возвращают `dict`/`list`, ничего не печатают,
   пути передаются параметром, сеть ходит через `core/http.get()`.
2. Прогресс и отмена передаются колбэками, а не печатью:
   `on_progress: Callable[[float, str], None]`, `cancel: threading.Event`.
3. В CLI-обёртках остаются только разбор аргументов, форматирование таблиц и коды выхода.
4. Добавляем всем скриптам `--json-out FILE` (частично уже есть), чтобы результат можно было
   сравнивать в тестах и потреблять из UI.
5. Кэш переезжает из `raw/.pcache.json` в SQLite-таблицу `cache` (TTL и метка времени — те же).

Известные отклонения от legacy:
- `org_candidates(exclude_full_name=...)` — исправление мёртвой проверки legacy (`startswith(target+'/')`
  не срабатывала никогда), зафиксировано паритетным тестом;
- `pypi_raw` при пустом `releases[ver]` возвращает `date='?'` вместо `IndexError`, намеренная устойчивость;
- `names_from_file(fix_toml=False)` — по умолчанию сохраняет legacy-причуду `.toml` (лишние
  `dependencies`/`name` в списке имён); CLI вызывает с `fix_toml=True` (реализуется в 1f);
- поле `mcp` в результате `check()` — legacy-алиас для `flag`; будет удалено из CLI JSON в P4,
  `app/` использует `flag`;
- `catalog.report()`: свежесть считается без legacy-трюка `(age or 9999)` — push сегодняшнего дня
  (возраст 0) теперь «свежий», а не «данных нет»; в legacy такой проект сразу считался заброшенным.
  Паритет остальных полей и всех чистых функций `catalog` держит `tests/test_core_catalog_legacy_contract.py`;
- `scan`: чекпоинт шире legacy (пишутся `base_url`, `finished`, `started_at`, `elapsed`, а `ns`
  называется `namespaces`) — обход должен продолжаться после перезапуска окна, а не только после
  Ctrl+C; старый файл чекпоинта читается (`from_checkpoint` понимает `ns`);
- `scan`: не полученная страница реестра больше не значит «каталог кончился» — legacy на сбое сети
  молча завершал обход; здесь сбой оставляет `finished=False` и курсор для продолжения, а пустая
  страница закрывает обход. Различие даёт `mcp_registry.servers_page()` (`([], None)` против
  `(None, None)`), потому что в legacy-форме ответа `([], None)` эти два исхода неразличимы;
- `mcp_registry.servers_page` возвращает `(None, None)` при сбое сети («продолжить позже») и
  `([], cursor|None)` при валидной странице; `list_servers` сохраняет legacy-контракт
  `([], None)` для совместимости с `catalog.py`;
- `collect` (1g): элементы `catalog.json` — развёрнутые объекты серверов (`list_servers_raw`),
  а не записи формы `{"server": {...}}`, как писал legacy; потребители (`coverage.py`, `scan.py`)
  принимают обе формы, сравнение в `compat_check` идёт по именам серверов;
- `catalog.json` в `collect.py` хранит нормализованные элементы, не `{"server":{...}}`
  (следствие `list_servers_raw`). Флаг `--legacy-catalog-format` в P4 вернёт legacy-форму
  для внешних скриптов, читающих старый формат;
- `monitor.diff` принимает только `diff_from` (не `days`): `days` — это UI-фильтр отображения,
  а не параметр задачи. `registry.scan` не принимает `classes` (это настройка метода в
  `core.scan.DEFAULT_CLASSES`). `KIND_PARAM_TYPES` — третья грань того же контракта в
  `app/jobs.py` (ключи, обязательность, типы), единственный источник правды для `app/web.py`
  и `_default_runner` (3d-b).

Статус P1: закрыт 2026-10-19. 576 тестов. Сквозной контур
POST -> cli.check_predecessors --job-json -> Store.record_observations
-> GET /api/packages/{name} доказан (unit + hooks + e2e +
shutdown-order). Resume / recover_interrupted / отмена / SSE /
токен-миддлварь работают. Следующий — P2 (экраны).

Статус P2: закрыт 2026-10-19. 626 тестов. Экраны «Задачи» (таблица,
фильтр отмены/продолжения, SSE), «Сейчас решать» (матрица adoption x flag,
карточка пакета, SSE-refresh) и «Настройки» (пороги, keywords, readonly)
работают в WebView2. GET/PUT /api/settings, F5 = Обновить, токен через
sessionStorage (в 3a — HttpOnly cookie, gotcha 40). Следующий — P3
(Пакеты, Мониторинг, Каталоги, Сравнение, экспорт CSV/MD).

Статус P3 (наполовину): закрыт «Пакеты» (поиск, пагинация, карточка),
«Мониторинг» (таймлайн, фильтры), «Каталоги» (сборка по фразам, список,
детальный экран с фильтрами, пагинацией, экспорт CSV/MD). HttpOnly cookie
для токена (навигация без ?token=). monitor.diff как задача (с фиксом
fallback имён). Следующий — «Сравнение» + экспорт событий/matrix + полировка
(3c).

Статус P3: закрыт 2026-10-19. 739 тестов. Все 7 экранов работают в WebView2:
Задачи, Сейчас решать, Пакеты, Мониторинг, Каталоги, Сравнение, Настройки.
Экспорт: каталог (CSV/MD), события (CSV/JSON), матрица (CSV), сравнение
(CSV). monitor.diff как задача работает (fallback имён из prev). Миграция 2:
`catalog_items.url`. Следующий — P4 (упаковка, sync_skill, demo, PyInstaller,
README установки).

Статус P3-3c (закрыт 20.10.2026, отдельным коммитом): «Сравнение» (2–3 пакета
бок о бок), экспорт событий (CSV/JSON), экспорт матрицы (CSV), экспорт сравнения
(CSV). Правки 3c закоммичены — в рабочем дереве их больше нет.

Статус P4 (план): упаковка (PyInstaller onedir), sync_skill, demo-режим,
README «Установка на чистой машине», API Keys Store (инфраструктура для будущих
коллекторов), momentum, cross_ecosystem. P4 не добавляет новых экранов, кроме
блока «Сигналы» в карточке пакета.

Статус P4b (закрыт 2026-10-20): momentum, 839 тестов.

Статус P4c (закрыт 2026-10-20): cross-ecosystem momentum, миграция 3
(concept_packages, signal_observations), 884 теста.

Статус P5a (закрыт 2026-10-20): jobs (Adzuna), первый сигнал
с ключом, 925 тестов. Graceful degradation: без ключа
configured=False, приложение работает.

Статус P5b (закрыт 2026-10-20): patents (PatentsView), второй сигнал с
ключом, 959 тестов. Окно импульса — завершённые календарные годы
[current−3, current−1] (неполный текущий год дал бы ложный спад).
Формат PatentsView — `size`, не `per_page` (проверено по исходникам PVAPIViews.py).

P5c (закрыт 2026-10-20): funding (SEC EDGAR), третий сигнал с ключом
(User-Agent, не api_key), 996 тестов. Окно — завершённые кварталы
[current_q−4, current_q−1]. Формат EDGAR — hits.total.value (проверено
живым запросом).

P5d (закрыт 2026-10-20): search_interest (Wordstat + Apify Google Trends),
1068 тестов. Составной статус: "ok" при configured=True, даже если error без
points; "not_configured" если ни один источник не настроен; "not_collected"
если не собирали. Wordstat: regions — список чисел, client_id не в запросе,
trend_pct = None (динамика в /v1/dynamics, follow-up).

P5e (закрыт 2026-10-20): Gap Score — `core/signals.py` (GapScore,
GapWeights, Normalizer, compute_gap_score/batch), веса `gap_weight_*` в
Settings и форме настроек, `gap_score`/`gap_breakdown` в точках
`/api/matrix` и карточке, новый `GET /api/gap-score?names=a,b`, размер точки
на матрице = score. 1118 тестов (было 1068). Нормировка — линейный min-max.
Усечение 0..100 при отрицательных весах. Вырожденный диапазон (min == max) -> 0.5.

P5f-1 (закрыт 2026-10-20): Niche Finder, первая итерация — сквозной workflow
«фраза → ранжированный список подсфер». Новый `cli/niche.py` (конвейер
`catalog.build` → `packages.check` → `signals.collect` → `gap_score`, единый
`result.json`), kind `niche.find` в `app/jobs.py`, `POST /api/niche/find` +
`GET /api/niche/result/{job_id}`, экран `/niche` (форма, прогресс, таблица),
11-я колонка `gap_score` в экспорте матрицы. 1163 теста (было 1118).

P4d-1 (закрыт 2026-10-21): упаковка (PyInstaller onedir, `.exe` 37 МБ),
demo-режим (временный каталог), `tools/sync_skill.py`, CI (pytest + mypy +
compat + build на windows-latest), 1189 тестов. **ПРОМЕЖУТОЧНЫЙ:** `.exe`
собран из кода без P5f-2; пересборка — P4d-2, после P5f-2.

Что именно сделано: `gapaudit.spec` (PyInstaller onedir, `dist/gapaudit/`),
`tools/sync_skill.py` (зеркало
`core/` + `cli/` в скилл `ecosystem-gap-audit` + генерируемая точка входа
`scripts/run.py`), `tools/demo_mode.py` + `--demo`/`GAPAUDIT_DEMO=1` (данные из
`tests/fixtures`, сеть выключена, временный каталог данных), `.github/workflows/ci.yml`
(pytest + mypy + compat_check), README («Установка из .exe», «Demo-режим», «CI»,
«Статус»), extra `packaging` в `pyproject.toml` (pyinstaller), маркер
`pythonnet; sys_platform == 'win32'`. 1174 теста (было 1163; +9 sync_skill,
+16 demo_mode). Скилл синхронизирован: 32 файла, копия запускается
(`python -m scripts.run check_predecessors --help` — код 0).

P5f-2 (закрыт 2026-10-21): Niche Finder закрыт — momentum в CLI (Store
read-only), checked/found в UI, фильтры, экспорт CSV/MD, кнопка «Собрать» в
карточке, 503 в demo для niche.find. 1204 теста.

Статус P5: закрыт 2026-10-21. Все 6 сигналов (momentum, cross_eco, jobs,
patents, funding, search_interest) + Gap Score + Niche Finder. 1204 теста.
P6 (MCP, расписание, уведомления) — отложен в долгосрочный план.

Статус P4 (на момент закрытия P5f-2): P4d-1 закрыт, P4d-2 (пересборка `.exe`
после P5f-2) — следующий шаг.

P4d-2 (закрыт 2026-10-21): пересборка `.exe` после P5f-2, проверка на чистой
машине (health, matrix, niche, signals.collect, niche.export), README
обновлён. P4 закрыт. Код не менялся — только упаковка из HEAD `24d7e69`.

Статус P5 (план): сигналы ниш. Уровень 1 (без ключей): momentum,
cross_ecosystem — работают на существующих данных. Уровень 2 (ключи): jobs
(Adzuna), patents (PatentsView), funding (SEC EDGAR), search_interest
(Yandex Wordstat / Google Trends). Уровень 3 (интеграция): Gap Score, экран
«Поиск ниши» (Niche Finder), размер точки в матрице.

Статус P6 (отложен в долгосрочный план, 2026-10-21): MCP-сервер (stdio,
тонкий фасад над Store), расписание внутри приложения, уведомления о событии
«появился MCP», тёмная тема (уже есть), батчи с паузами для больших каталогов.
Причина отсрочки: сначала прожить месяц с упакованным приложением (P4d),
проверить гипотезу «нужен ли MCP-сервер и расписание». Возврат к P6 — по
результатам месяца использования, не раньше.

Отклонено: гео (PyPI by country / BigQuery). Причина: узкая применимость
(только PyPI), требует GCP-аккаунт, ценность ниже, чем momentum + jobs +
search_interest. В §16 п. 6 — закрыто как «отклонено».

Критерий готовности шага 0: `python cli/check_predecessors.py gerenuk` выдаёт тот же текст, что
старый скрипт; `core` импортируется без побочных эффектов (никаких сетевых вызовов на импорте);
все шесть CLI проходят smoke-тест на фикстурах.

## 5. Хранилище: схема SQLite

```sql
meta(key TEXT PRIMARY KEY, value TEXT)                    -- schema_version и пр.
cache(key TEXT PRIMARY KEY, fetched_at REAL, value TEXT)  -- тот же TTL 12 ч, но видимый в UI
jobs(id TEXT PRIMARY KEY, kind TEXT, params TEXT, status TEXT, progress REAL,
     message TEXT, created_at TEXT, started_at TEXT, finished_at TEXT,
     log_path TEXT, error TEXT, pid INTEGER)
packages(name TEXT PRIMARY KEY, source TEXT, url TEXT, summary TEXT,
     first_seen TEXT, last_seen TEXT)
observations(id INTEGER PRIMARY KEY, run_id TEXT, name TEXT, source TEXT, version TEXT,
     released_at TEXT, adoption_month INTEGER, adoption_week INTEGER,
     flag TEXT, flag_value INTEGER, verdict TEXT, measured_at TEXT)
catalogs(id TEXT PRIMARY KEY, queries TEXT, created_at TEXT, stats TEXT, path TEXT)
catalog_items(catalog_id TEXT, name TEXT, source TEXT, stars INTEGER, lang TEXT,
     pushed TEXT, desc TEXT, url TEXT, PRIMARY KEY(catalog_id, name))
events(id INTEGER PRIMARY KEY, run_id TEXT, kind TEXT, name TEXT, detail TEXT, at TEXT)
schedules(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, params TEXT, cron TEXT,
     enabled INTEGER, last_run TEXT)
signal_observations(id INTEGER PRIMARY KEY AUTOINCREMENT,
     package TEXT NOT NULL,
     signal_kind TEXT NOT NULL,   -- 'momentum', 'cross_eco', 'jobs', 'patents',
                                  -- 'funding', 'search_interest'
     value REAL,
     detail TEXT,                  -- JSON: по странам, по раундам, по регионам
     measured_at TEXT NOT NULL)
concept_packages(concept TEXT NOT NULL,
     ecosystem TEXT NOT NULL,      -- 'pypi', 'npm', 'github'
     package TEXT NOT NULL,
     PRIMARY KEY(concept, ecosystem, package))
```

Индекс к новой таблице:

```sql
CREATE INDEX idx_signal_pkg_kind_at
  ON signal_observations(package, signal_kind, measured_at DESC);
```

Таблиц: 11 именованных (после миграции 3) + sqlite_sequence (AUTOINCREMENT).
Считать по именам из
`sqlite_master`, не по общему числу.

Важно (follow-up 2c): `observations.flag` — TEXT-метка (напр. `"MCP"`),
`observations.flag_value` — INTEGER 0/1. API отдаёт `flag: bool` (производное от
`flag_value`) и `flag_label: str` (из `flag`). Не путать при чтении из Store:
`bool("MCP") == True` всегда.

Важно (follow-up 3b): `_upsert_package` пишет только пакеты с `source != '-'`
(пакеты, отсутствующие в реестрах, попадают только в `observations`, без записи
в `packages`). Это осознанное решение: `packages` — справочник известных
пакетов, `observations` — история замеров (включая отрицательные).

Уточнение 3b по `catalog_items`: `source` хранит источники склейки через
запятую (`github,npm`) — колонка одна, а после `core.catalog.dedupe` у находки
их несколько. `url` добавлен миграцией 2 (3c) и берётся из `item["url"]`:
gh_search отдаёт `html_url`, `npm_search_full` — `pkg.links.npm`, реестр —
`server.repository.url` (у записей реестра он пустой, хранится как NULL).
Экран строит ссылку из `url`, а при его отсутствии — из `name` (`owner/repo` →
страница GitHub, одиночное имя → поиск GitHub).

Что это даёт сразу: «покажи, у кого за 30 дней вырос adoption и при этом не появился MCP» —
один запрос; «когда впервые появился сервер у инструмента из моего эшелона» — из `events`;
«сколько всего уникальных пакетов в поле зрения» — из `packages`.

Правило: схема версионируется в `meta.schema_version`, миграции — маленькие функции `migrate_N()`.
Версию пишет сам `migrate()` после каждой ступени (а не `migrate_1`), и каждая миграция
идемпотентна — иначе повторный старт на той же БД падает на «duplicate column» (AGENTS gotcha 49).
Текущая версия схемы — 3 (концепты и сигналы: `concept_packages`,
`signal_observations`, миграция 3; до этого — 2, колонка
`catalog_items.url`).

Миграция 3 (P4): `signal_observations` и `concept_packages` — состояние схемы
становится 3. Правило прежнее: версию пишет `migrate()` после каждой ступени,
каждая миграция идемпотентна (AGENTS gotcha 49).

Таблицы `api_keys` в SQLite НЕТ — сознательно. Ключи лежат в отдельном файле
`<data_dir>/api_keys.json` (права 0600, в .gitignore), а не в БД: при экспорте
или копировании БД ключи не утекают, и `core/` не получает зависимости от
`sqlite3` (gotcha 19). Статус последней проверки ключа — тоже в этом файле.
Решение зафиксировано в §16 п. 7.

Соглашение о кэше (шаг 1a): `FileCache.put` пишет только в память; `save()` — атомарная запись на диск
(tmp + `os.replace`); CLI вызывает `save()` в конце прогона, приложение — на закрытии задачи.
`SqliteCache` (шаг 2) пишет в таблицу `cache` сразу, поэтому `save()` у него — no-op.

## 6. Задачи: контракты и поведение

Статусы: `queued → running → done | failed | cancelled`. Отдельно `interrupted` для случая,
когда приложение закрыли во время работы (задача поднимается из чекпоинта при следующем старте).

Уточнение (3a): статус job — `running`, но событие для подписчиков называется `started`
(не `running`). SSE-клиент не должен выводить `started` как статус — статус он берёт из
поля `status` события или из опроса задач.

```python
KIND_PARAM_KEYS (app/jobs.py — единственный источник правды; им же валидирует app/web.py):
  "packages.check": {"names": [str] (обязателен), "keyword": [str]}
  "catalog.build":  {"phrases": [str] (обязателен), "min_stars": int, "pages": int, "sources": [str]}
  "registry.scan":  {"base_url": str, "limit": int, "max_pages": int}
  "github.verify":  {"targets": [str] (обязателен), "keyword": str}
  "monitor.diff":   {"diff_from": str} (обязателен)
  "signals.collect": {"package": str (обязателен), "kinds": [str]}
  "niche.find":      {"q": str (обязателен), "filters": dict, "limit": int}

KIND_REQUIRED_KEYS — подмножество обязательных ключей на kind; KIND_PARAM_TYPES — типы
значений для 400. POST /api/jobs отвечает 400 (fail fast), если kind неизвестен, ключ не
из whitelist, тип не тот или обязательный ключ отсутствует/пуст — задача НЕ создаётся.
`classes` (registry.scan) и `days` (monitor.diff) параметрами задачи не являются: классы —
настройка метода (core.scan.DEFAULT_CLASSES), «за N дней» — фильтр отображения в UI (3d-b).
```

Виды задач регистрируются ТОЛЬКО в `app/jobs.py`
(`KIND_PARAM_KEYS`/`KIND_REQUIRED_KEYS`/`KIND_PARAM_TYPES`) — локального
`JOB_KINDS` в `app/web.py` нет (шаг 3d-b). Новый вид задачи требует согласованной
правки в четырёх местах (AGENTS gotcha 54): KIND_PARAM_KEYS, `_params_to_argv`
(или `_KIND_TO_MODULE`), хук результата в lifespan, форма в UI.
`signals.collect` и `niche.find` — НЕ CLI-скрипты: они работают через
`core/collectors/` внутри подпроцесса-обёртки, argv собирается по whitelist.

Обязательные свойства:

- ограничение параллелизма очереди (по умолчанию 2 задачи) и троттлинг по хосту: GitHub Search —
  30 запросов/мин, PyPI-статистика — склонна к 429; при ожидании в статус пишем «ждём лимит N с»;
- чекпоинт каждые 25 страниц (у `scan.py` уже есть — переносим как есть) и возможность продолжить
  с сохранённого курсора;
- отмена: сначала флаг, через 5 секунд — завершение процесса-ребёнка;
- лог каждой задачи — отдельный файл `data/logs/<job_id>.log`, в UI доступен по клику;
- прогресс: `progress` (0..1) + `message`; публикуется в SSE-канал сразу при изменении, без опроса;
- идемпотентность: повторный запуск с теми же параметрами не ломает историю, а добавляет наблюдения
  с новой меткой `measured_at` (именно на этом строится мониторинг).

## 7. Бэкенд: эндпоинты

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/` | главный экран «Сейчас решать» |
| GET | `/jobs` | список задач и статусы |
| POST | `/jobs` | создать задачу (`kind`, `params`) |
| POST | `/jobs/{id}/cancel` | отмена |
| POST | `/jobs/{id}/resume` | продолжение `interrupted`-задачи (3c: 501; 3d-b: реальный вызов `queue.resume_interrupted`, 404/409) |
| GET | `/jobs/{id}/log` | хвост лога задачи |
| GET | `/api/jobs?status=` | используется в P3 (мониторинг) и будущей пагинации; на экране «Задачи» фильтр клиентский (2a) — чтобы `confirmClose` видел running-задачи при активном фильтре «Готово». При >10k задач — переключиться на серверную пагинацию + отдельный endpoint `active_count` для `confirmClose` |
| GET | `/api/stream` | SSE: прогресс задач и события |
| GET | `/api/matrix` | агрегат экрана «Сейчас решать» (шаг 2b): точки adoption × признак из `get_latest_observations`, 4 квадранта по `alive_weekly`, пороги, счётчики. С шага P4b каждая точка несёт `momentum_30d` (Δ adoption за 30 дней, `null` — мало данных), `momentum_color` (`green`/`red`/`gray` по порогу ±5%) и `momentum_samples`. С шага P5e — `gap_score` (0..100 или `null`) и `gap_breakdown` (семь нормированных компонентов + `weights_used` + `components_available`) |
| GET | `/api/gap-score?names=a,b` | Gap Score 2–3 пакетов (шаг P5e): ответ `{scores: [{name, score, components, weights_used, components_available, components_total}], missing: [...]}`; min-max нормируется по ВСЕМ пакетам (как у матрицы), поэтому число совпадает с точкой экрана. 400 — имён не 2–3, 404 — ни одного в Store |
| POST | `/api/packages/check` | прогон имён (adoption × признак) |
| GET | `/api/packages/search?q=` | поиск по имени/описанию с пагинацией (шаг 3a): `q`, `limit` 1..200 (50), `offset`; ответ `{packages, total, limit, offset, q}`. Роут объявлен до `/api/packages/{name}` — иначе «search» стало бы именем пакета |
| GET | `/api/packages/{name}` | карточка пакета с историей (события — через `Store.get_events(name=...)`) |
| GET/POST | `/api/catalogs` | список и сборка каталога по фразам (шаг 3b) |
| GET | `/api/catalogs/{id}` | метаданные каталога + `items_count` (шаг 3b) |
| GET | `/api/catalogs/{id}/items?alive=&lang=&min_stars=&q=&limit=&offset=` | элементы с фильтрами и пагинацией; ответ `{items, total, limit, offset}` (шаг 3b) |
| POST | `/api/catalogs/{id}/export?format=csv\|md` | файл в ответе (`Content-Disposition: attachment`), CSV — `utf-8-sig` (шаг 3b) |
| GET | `/api/events` | таймлайн событий (шаг 3a; в плане назывался `/api/monitor`): `since`, `kind`, `name`, `limit` 1..500 (100), `offset`; ответ `{events, total, limit, offset}`, `total` — по тем же фильтрам |
| GET | `/api/compare` | сравнение 2–3 пакетов (шаг 3c): `names=a,b,c`; ответ `{packages: [{name, source, version, released_at, adoption_month, adoption_week, flag, flag_label, verdict, measured_at}], missing: [имена без наблюдений]}`. 400 — имён не 2–3 после дедупликации, 404 — ни одного имени в Store |
| POST | `/api/compare/export?format=csv&names=a,b` | выгрузка сравнения (шаг 3c): строка на пакет, колонки как у матрицы; 400/404 те же, что у `GET /api/compare` |
| POST | `/api/events/export?format=csv\|json&kind=&name=&since=&limit=` | выгрузка событий (шаг 3c): ВСЕ события под фильтрами, без пагинации; CSV `id,kind,name,detail,at,run_id` (`utf-8-sig`), JSON `{"events": [...]}`; `limit` 1..50000 (10000), `limit` больше потолка — 400 |
| POST | `/api/matrix/export?format=csv` | выгрузка точек матрицы (шаг 3c): те же точки, что у `GET /api/matrix` (общий `_matrix_points`), CSV `name,source,version,released_at,adoption_week,adoption_month,flag,verdict,measured_at,momentum_30d`, `flag` — 0/1; `momentum_30d` — десятая колонка с P4c (пусто при нехватке истории). `POST /api/compare/export` остаётся с прежними девятью колонками |
| GET/PUT | `/api/settings` | пороги, ключевые слова, TTL, лимиты (шаг 2c). GET отдаёт `settings` + `readonly` (`data_dir`, `db_path`, `version`, `api_token_required`) + `editable_keys` (10 ключей); PUT валидирует тип/диапазон/инвариант `alive_weekly >= dead_weekly`, пишет `<data_dir>/settings.json` и возвращает `warnings`. `data_dir` и `language` через API не меняются — это readonly |
| GET | `/api/signals/{package}` | все сигналы по пакету (P4/P5): `{momentum, cross_ecosystem, jobs, patents, signals_status, signals_available, signals_unavailable}` — полный контракт ниже, в «Коллекторах сигналов» |
| POST | `/api/signals/collect` | собрать сигналы: kind `signals.collect` (P4/P5). Тело `{package, kinds[]}` (`kinds ⊆ SIGNAL_COLLECT_KINDS`); 202 `{job_id, status}` |
| GET | `/api/concepts` | список концептов (P4c): `{concepts: [{concept, n_packages, ecosystems}]}`. Чтение `concept_packages`, без расчёта |
| POST | `/api/concepts` | добавить привязку (P4c): тело `{concept, ecosystem, package}`; 202 `{ok: true}`; 400 при пустых полях или `ecosystem` вне `{pypi,npm,github}`. `INSERT OR IGNORE` — повтор не создаёт дублей |
| DELETE | `/api/concepts/{concept}` | удалить концепт целиком (P4c): `{deleted: N}`; 404 — концепта не было |
| GET | `/api/gap-score?names=a,b,c` | gap_score по списку пакетов (P5): `{names, scores: [{name, score, breakdown}]}` |
| POST | `/api/niche/find` | Niche Finder (P5): тело `{q, filters, limit}`; 202 `{job_id}` — работа идёт задачей |
| GET | `/api/niche/result/{job_id}` | результат Niche Finder: `{q, total, results: [{name, gap_score, momentum_30d, flag, adoption_week, jobs_total, patents_percent, funding_count}]}` |
| GET | `/api/settings/api-keys` | список провайдеров + статус (P4). НЕ значения ключей: только `{provider, configured, last_check, last_check_ok, meta_public, meta_fields}` + `load_error` всего ответа |
| PUT | `/api/settings/api-keys` | сохранить ключ (P4): `{provider, value, meta?}` |
| POST | `/api/settings/api-keys/{provider}/check` | тестовый запрос к провайдеру (P4): `{ok, detail}` |
| DELETE | `/api/settings/api-keys/{provider}` | удалить ключ (P4) |
| GET | `/api/health` | готовность бэкенда (для окна) |
| GET | `/docs/{file}` | документация для окна: белый список из одного файла — `API-KEYS.md` (шаг P4b, §18.6). Остальные файлы `docs/` — 404: каталог целиком наружу не отдаётся, там внутренние заметки. Токен не требуется (это не `/api/*`), значений ключей в файле нет |

Решение (P5a): `/api/compare/export` оставлен без `momentum_30d`: это
сравнение пакетов по базовым полям (source/version/released/adoption/
flag/verdict/measured), не матрица. Если пользователь захочет momentum в
сравнении — это P5f (Niche Finder), когда появится gap_score и станет
ясно, какие метрики важны. Контракт 9 колонок зафиксирован.

Коллекторы сигналов (P4/P5):

- `GET /api/settings/api-keys` возвращает поле `meta_fields` (список
  несекретных полей, которые UI рендерит как `text`, а не `password`) и
  `meta_public` (значения этих полей). Секреты в `meta_public` не попадают:
  `meta` — публичный канал, `save` пускает туда только объявленные поля.
  `load_error` — причина, по которой файл ключей не прочитался (`null`, если
  всё в порядке); UI показывает её баннером, а не «тихо пусто» (шаг P4b).
- `GET /docs/{file}` — whitelist: только `API-KEYS.md`. Остальные файлы
  `docs/` — 404. Это защита от случайной отдачи внутренних документов.
- `GET /api/signals/{package}` — только чтение из SQLite/кэша, сети нет.
  Ответ (§0.3): `momentum` — объект (всегда, из истории); `cross_ecosystem` —
  объект | `null`; `jobs` — объект | `null`; `patents` — объект | `null`;
  `funding` — объект | `null` (P5c); `search_interest` — объект | `null`
  (P5d: `{query, wordstat, google_trends, total, configured, error}`, два
  подблока источников); плюс списки
  `signals_available`/`signals_unavailable` и карта `signals_status`
  `{"jobs": "ok"|"not_configured"|"not_collected", "patents": ..., "funding":
  ..., "search_interest": ...}`.
  `signals_status` — различимое состояние для UI: `ok` = есть данные,
  `not_configured` = ключ не настроен (у `funding` — не введён User-Agent
  EDGAR, провайдер `edgar`), `not_collected` = ключ есть, но не
  собирали (у `momentum` отдельного статуса нет — он всегда посчитан из
  истории, статуса «не настроен» не имеет; статусы относятся к видам,
  которые собираются внешним API). У `search_interest` статус **составной**
  (P5d): `ok` — настроен хотя бы один из двух источников (Wordstat/Apify)
  или источник несёт `error`; `not_configured` — не настроен ни один.
- `POST /api/signals/collect` — body `{"package": str, "kinds": [str]}`.
  Submit job `kind="signals.collect"`, ответ `202 {job_id, status}`.
  `kinds ⊆ SIGNAL_COLLECT_KINDS = ["jobs", "patents", "funding",
  "search_interest"]` (P5d; набор совпадает с `cli.signals.SIGNAL_KINDS`).
  Без `kinds` — все виды набора.
  Работа идёт задачей (подпроцесс), прогресс — через SSE, запись — в
  `signal_observations`.
- `POST /api/niche/find` — kind `niche.find`, конвейер из нескольких шагов
  (`catalog.build` → `packages.check` → `signals.collect` → gap_score).
  Результат читается `GET /api/niche/result/{job_id}`; SSE-подписчик не должен
  перезаписывать статус после `done` (gotcha 38/24).
- Graceful degradation: если ключа нет или внешний API вернул ошибку — коллектор
  возвращает пустой результат, сигнал получает статус `not_configured`/`error`,
  остальное приложение работает. Исключение из коллектора в UI/API не выходит
  никогда (gotcha 50).
- API-ключи: наружу отдаётся только статус (`configured`/`last_check_ok`),
  значение ключа — никогда. Хранится в `<data_dir>/api_keys.json` (0600),
  в SQLite — нет (§5, миграция 3).

Каталоги (шаг 3b, детали):

- `POST /api/catalogs` — submit job `catalog.build`; тело
  `{"phrases": [..], "min_stars": 20, "pages": 2, "sources": ["github","npm","registry"]}`,
  валидация 1..10 непустых фраз, `min_stars >= 0`, `pages` 1..10, источники ⊆
  трёх известных, лишние ключи — 400. Ответ `202 {"job_id", "catalog_id"}`;
  `catalog_id = job_id` — каталог пишется хуком lifespan под id задачи.
- `GET /api/catalogs` — список (id, queries, created_at, stats, items_count),
  `created_at DESC`; `items_count` считается подзапросом в том же SELECT.
- `GET /api/catalogs/{id}` — метаданные + `items_count`; неизвестный id — 404.
- `GET /api/catalogs/{id}/items` — фильтры `alive` (push за 90 дней),
  `lang`, `min_stars`, `q` (по имени/описанию с экранированием `%`/`_`),
  `limit` 1..500 (100), `offset`; `ORDER BY stars DESC NULLS LAST, name`.
- `POST /api/catalogs/{id}/export?format=csv|md` — весь каталог файлом
  (`Content-Disposition: attachment; filename=catalog-{id}.{ext}`), CSV
  `utf-8-sig`, Markdown — таблица `core.catalog.to_markdown`; `format` вне
  пары — 400.

Хук lifespan пишет результаты задач по kind: `packages.check` →
`record_observations`, `monitor.diff` → `record_events` (формат файла —
`{"events": [{"kind","name","detail"}, ...]}`, так его пишет
`cli/check_predecessors --diff` под `--job-json`), `catalog.build` →
`record_catalog` + `record_catalog_items`.

`monitor.diff` (фикс 3c): argv задачи содержит только `--diff=<файл>` — имён в
нём нет, и CLI падал на «нечего проверять» до чтения prev, поэтому событий не
появлялось никогда. Теперь при `--diff` без позиционных имён имена берутся из
самого prev-файла (`observations` или `items`); пустой/нечитаемый baseline под
`--job-json` — `emit_error` + код 2, а не тихий `done` с нулём событий
(AGENTS gotcha 45).

Правила: никакой сетевой работы в обработчиках, кроме чтения из SQLite/кэша; всё долгое — задачей.
Ответы — JSON со стабильными ключами, пригодные и для CLI, и для UI.
`params` возвращается как есть. Валидация `POST /api/jobs` строится по таблицам
`app/jobs.py` — `KIND_PARAM_KEYS` / `KIND_REQUIRED_KEYS` / `KIND_PARAM_TYPES` — это
единственный источник правды; локального `JOB_KINDS` в `app/web.py` нет (шаг 3d-b):
два описания «валидной задачи» расходились, и API отвечал 202 задаче, которая падала
с `missing param`. `app/web.py` импортирует таблицы и отвечает 400 при неизвестном
`kind`, неизвестном ключе `params`, неверном типе и отсутствующем/пустом обязательном
ключе (задача не создаётся — fail fast). `_default_runner` использует те же
`KIND_PARAM_KEYS`/`KIND_REQUIRED_KEYS` для проверки обязательных до `subprocess`.
Добавление нового `kind` — одна правка в `app/jobs.py` (плюс строка в
`_KIND_TO_MODULE`); `web.py` подхватывает её автоматически.
`_default_runner` маппит `kind` в argv по whitelist-таблице `_KIND_TO_MODULE`
(`packages.check`→`cli.check_predecessors`, `catalog.build`→`cli.catalog_build`,
`registry.scan`→`cli.catalog_scan`, `github.verify`→`cli.verify`,
`monitor.diff`→`cli.check_predecessors`); `kind` никогда не подставляется в имя модуля
(раньше `monitor.diff` давал `python -m cli.monitor.diff`, а чужой `kind` с точками/пробелами —
RCE). Параметры — `_params_to_argv` по whitelist на каждый kind: неизвестный `kind` или
неизвестный ключ в `params` — `ValueError` до `subprocess` (шаг 3d-a).

### Как клиенты находят GapAudit

Три способа, выбор — по ситуации, автоматического discovery нет:

| Клиент | Способ | Когда |
|---|---|---|
| Hermes (разработчик) | CLI + pytest + git | всегда |
| Человек (разово) | CLI (`python -m cli.*`) | один прогон без истории |
| Внешний агент (Cursor/Claude) | HTTP `/api/*` с `X-GapAudit-Token` | очередь + история; токен из `<data_dir>/session.token` (3b) |
| MCP-клиент (P6) | stdio MCP-сервер | нативный tool в Cursor/Claude Desktop |

Hermes не подключается к живому приложению — он его строит через CLI.
Внешний клиент выбирает способ по конфигурации, а не по discovery.

## 8. Слой pywebview: как именно поднимается окно

Порядок в `desktop.py` (обязательно на главном потоке, иначе pywebview падает):

1. свободный порт берём сами (`socket.bind(("127.0.0.1", 0))`), порт не хардкодим;
2. генерируем токен сессии `secrets.token_urlsafe(32)`;
3. поднимаем uvicorn в демон-потоке с middleware: все `/api/*` требуют заголовок `X-GapAudit-Token`
   или cookie; проверяем `Host`/`Origin` (защита от DNS-rebinding и от захода чужой страницы в браузере);
4. ждём `/api/health` (до 10 секунд), только потом открываем окно — иначе будет белый экран;
4a. сразу после успешного health-чека токен пишется в `<data_dir>/session.token`
   (права `0600`, на Windows дополнительно `icacls /inheritance:r /grant:r <user>:F`),
   а при закрытии окна файл удаляется — это единственный способ передать токен
   внешнему клиенту (§7, «Как клиенты находят GapAudit»). Реализация —
   `app/session.py`; каталог данных один на весь шаг: `desktop.py` выставляет
   `GAPAUDIT_DATA_DIR` до запуска uvicorn, и lifespan приложения читает тот же путь.
4a (продолжение, шаг 3c). Подтверждение закрытия — отдельным потоком: обработчик
   `closing` возвращает `False` и запускает поток с
   `window.evaluate_js("window.confirmClose()")`; при `True` выставляет флаг
   `approved` и зовёт `window.destroy()` (переход в `closing` снова, флаг виден).
   Причина: `evaluate_js` внутри `closing` вешает окно (`Control.Invoke` в UI-потоке
   WinForms ждёт семафор, который освобождает тот же поток). См. AGENTS gotcha 27 и
   скилл `desktop-webview-app-shells/references/windows-webview.md`.
5. `webview.create_window("GapAudit", url, width=1280, height=860, min_size=(1024, 720),
   confirm_close=False, text_select=True, js_api=Api())` — `confirm_close=False`, потому что
   подтверждение теперь своё (JS `confirm` с числом активных задач, см. 4a): нативный OK/Cancel
   из pywebview давал бы второй диалог подряд.
6. `webview.start(debug=args.dev)` — в dev-режиме открываются devtools WebView2;
7. на закрытии: если есть активные задачи — диалог «остановить и выйти / остаться»; затем
   корректная остановка uvicorn и завершение процессов-детей.

`js_api` (Api) — только системные вызовы, никаких данных:
`open_path(path)`, `reveal_in_explorer(path)`, `pick_file()`, `pick_folder()`, `open_external(url)`,
`app_info()` (версия, каталог данных, режим).

Режимы запуска:

- `python desktop.py` — обычное приложение;
- `python desktop.py --dev` — devtools включены, автоперезагрузка шаблонов;
- `python desktop.py --browser` — бэкенд без окна, открывается системный браузер (основной режим разработки UI);
- `python desktop.py --port 8790` — фиксированный порт для отладки.

Требования среды (проверено на этой машине): WebView2 Runtime **есть** (155.0.4283.62),
`pywebview` и `pythonnet` **нужно установить** — в отдельном окружении через `uv venv`, не в venv Hermes.
Проверка версии: `python -c "import webview, importlib.metadata as m; print(m.version('pywebview'))"`
— модуля `pywebview` не существует, пакет импортируется как `webview`, и атрибута
`webview.__version__` тоже нет (версия только через `importlib.metadata`).

## 9. Экраны

1. **Сейчас решать** (главный). Матрица adoption × признак (по умолчанию MCP) с квадрантами,
   рядом таблица «живые и без признака» — это и есть ответ «что делать». Клик по точке — карточка.
   Обязательные подписи: дата замера, «данные из кэша от HH:MM · обновить», текущие пороги.

   Уточнение шага 2b (реализовано): X — adoption (log scale, недельные скачивания),
   Y — наличие признака (2 строки: «с MCP» / «без MCP»), 4 квадранта с подписями
   («не лезть» / «не взлетело» / «ВОТ СЮДА» / «рынка нет»). Точки — только для
   наблюдений с непустым `adoption_week` (на log-оси `log10(0)` нет); они же
   считаются в `counts.with_week`, а `counts.total` — все последние наблюдения.
   Клик по точке — карточка пакета: информация, SVG mini-graph adoption по
   `/api/packages/{name}/history`, timeline `events`, кнопки «Проверить по GitHub»
   (`POST /api/jobs` kind=`github.verify`) и «Закрыть». Порог берётся из
   `Settings.alive_weekly` на каждом запросе (2c сможет менять без перезапуска).
   Живое обновление — SSE (`web/static/sse.js`), общий для «Задач» и матрицы.

   Поле признака в ответе `/api/matrix` — булево `flag` (из `flag_value`); метка
   ключевого слова (`flag` TEXT в схеме §5) отдаётся отдельно как `flag_label`.

   Уточнение шага 2c (реализовано): Y-координата точки не несёт смысла (строки —
   только признак flag). Точки с одинаковым adoption_week в одной строке разводятся
   по высоте на ±44 (5 полос), чтобы не накрывать друг друга и чтобы клик открывал
   выбранную точку. Это визуальная развязка, не данные. Под матрицей — строка
   «Последний замер: <дата> · Обновить» (самый свежий `measured_at` из точек,
   не дата загрузки экрана; дата загрузки живёт в панели как «обновлено в HH:MM»).
   F5 на всех экранах = «Обновить» (см. `web/static/app.js` и gotcha 39).
   Шаг 3c добавил в шапку кнопку CSV — выгрузку точек (`POST /api/matrix/export`,
   те же точки, что на экране: построитель `_matrix_points` общий).

   Шаг P5 добавляет в матрицу: размер точки = gap_score (0–100), цвет = momentum
   (зелёный — растёт, серый — стабилен, красный — падает), фильтр «только с
   momentum > 0». Точки без gap_score рисуются как раньше (базовый размер).

   Блок «Сигналы» в карточке пакета (P4/P5):

   - Momentum: Δ adoption 7/30/90 дней (числа + стрелки), при нехватке истории —
     «N замеров»;
   - Cross-ecosystem: сравнение adoption в PyPI/npm/GitHub для концепта;
   - Jobs: top-5 стран по вакансиям (если ключ Adzuna);
   - Patents: патентный импульс за 3 года (если ключ PatentsView);
   - Funding: последние раунды (если задан User-Agent EDGAR);
   - Search interest: топ-регионы и тренд 12 мес в двух подблоках — Yandex
     Wordstat (РФ) и Google Trends (глобально) — если настроен любой из них
     (P5d); каждый подблок показывает своё состояние («не настроен», «ошибка»,
     «данных нет»);
   - Gap Score: интегральное число 0–100 + breakdown по компонентам.

   Каждый сигнал помечен статусом: «данные», «не настроен» (ссылка на
   docs/API-KEYS.md) или «ошибка». Пустой сигнал не ломает карточку.

2. **Задачи**. Очередь, прогресс-бар, текущий шаг («страница 34 из 152»), отмена, лог по клику,
   история запусков с длительностью и статусом.
3. **Пакеты**. Поиск и карточка: версия, дата релиза, adoption за неделю и месяц, вердикт,
   признак, ссылки, история замеров (мини-график), кнопки «проверить по GitHub», «в эшелон».
4. **Каталоги**. Сборка по фразам (источники: GitHub, npm, реестр), список каталогов, элементы
   с фильтрами (живые/заброшенные, язык, звёзды), экспорт CSV/Markdown.

   Уточнение шага 3b (реализовано): два уровня — список каталогов (id, фразы,
   дата, N элементов) и детальный экран каталога `/catalogs/{id}`. На детальном —
   фильтры (живые/заброшенные по `pushed`, язык, `min_stars`, поиск),
   пагинация (100 на страницу), экспорт CSV/MD. Элементы — строки таблицы с
   именем, звёздами, языком, датой push, описанием и источниками; имя — внешняя
   ссылка на проект в новой вкладке (`url` из источника находки, миграция 2;
   при его отсутствии — `owner/repo` в GitHub, одиночное имя — в поиск GitHub).
   Пока каталог не записан (задача ещё идёт), экран показывает «каталог
   собирается» по статусу задачи с тем же id и обновляется сам: запись
   асинхронна и отстаёт от события `done` (gotcha 38). «← К списку» возвращает
   к `/catalogs`.

   Уточнение шага 3c: экспорт каталога (CSV/MD) выгружает весь каталог, не
   текущую страницу/фильтр. Экспорт отфильтрованного — отдельная опция в P4,
   если попросят. Детальный экран каталога, пока каталог собирается
   (`GET /api/catalogs/{id}` → 404, но `GET /api/jobs/{id}` → running/queued),
   показывает «Каталог собирается… N%» и опрашивает каждые 3 с (плюс
   SSE-событие done с задержкой 400 мс, gotcha 38).

5. **Мониторинг**. Таймлайн событий из `events`: релизы, скачки adoption, «появился MCP»
   (выделено отдельно), пропавшие пакеты; фильтр по периоду; выгрузка.

   Уточнение шага 3c: выгрузка таймлайна — кнопки CSV и JSON в шапке экрана,
   `POST /api/events/export` с текущими фильтрами (вид/имя). В файл уходит весь
   таймлайн под фильтром, а не текущая страница: экран листает по 50, выгрузка
   — «забрать всё».
6. **Сравнение**. 2–3 ниши/пакета бок о бок: adoption, даты, вердикты, признаки, размеры каталогов.

   Реализовано в 3c: экран `/compare` — форма «имена через запятую» (2–3,
   проверка на сервере: 400 при 1 или 4) и таблица «параметр × пакет» по
   последнему замеру каждого имени (`GET /api/compare`). Колонки: источник,
   версия, дата релиза, adoption за неделю и месяц, метка признака MCP, вердикт,
   дата замера. Имя без наблюдений не ошибка — колонки нет, а под таблицей
   строка «Нет данных: …»; 404 — только если не найдено ни одного. Экспорт —
   кнопка CSV (`POST /api/compare/export`, строка на пакет). Размеры каталогов
   в сравнение не входят: каталог — отдельная сущность с собственным экраном.
7. **Настройки**. Пороги (`ALIVE_*`, `DEAD_*`, `STALE_DAYS`), ключевые слова (несколько),
   каталог данных, расписание, тёмная тема.

   Реализовано в 2c (частично): пороги, keywords, TTL кэша, параллелизм задач и
   лимит реестра через `GET/PUT /api/settings`; каталог данных и версия — readonly
   (`data_dir` меняется только `--data-dir`/`GAPAUDIT_DATA_DIR`, требует перезапуска);
   расписание и тёмная тема — P6. Сохранённое пишется в `<data_dir>/settings.json`
   и накладывается в lifespan `app/web.py` (desktop.py файл не читает).

   Вкладка «API-ключи» (P4): провайдеры Adzuna, PatentsView, SEC EDGAR,
   Yandex Wordstat, Google Trends (официальный alpha), Apify (для Google Trends).
   Для каждого — поле ввода, статус («не настроен» / «настроен» / «проверка
   пройдена» / «ошибка»), кнопки «Сохранить», «Проверить», «Удалить» и ссылка на
   docs/API-KEYS.md. Предупреждение вверху: «Без ключа сигнал не работает,
   остальное приложение работает». Значения ключей не показываются в списке и не
   отдаются через API — только статус.

8. **Поиск ниши** (Niche Finder, P5). Сквозной workflow: фраза → сборка
   каталога → проверка пакетов → сбор сигналов → ранжированный список подсфер
   с фильтрами. Схема и контракт — §19.

Требования к UI: русский язык; никаких внешних CDN (Chart.js/htmx — локально в `static/`);
все таблицы с пагинацией (каталог реестра — до сотен тысяч строк, в DOM их не заливаем);
никаких модальных «подождите» — всё через статус задачи; горячая клавиша F5 обновляет данные из БД.

## 10. Совместимость CLI и синхронизация со скиллом

Обязательства перед CLI (проверяются тестом, а не обещанием):

- шесть скриптов запускаются теми же командами и флагами, включая `--keyword`, `--diff`,
  `--check-name`, `--from`, `--similar`, `--csv`, `--strict`, `--top`;
- коды выхода сохраняются (`--strict` → 2);
- формат таблиц не ломается без причины; новые поля — только в конец строки;
- работа без БД и без UI: CLI остаётся автономным (кладёт JSON рядом, в `raw/`, как сейчас).

Два места правды — это риск, поэтому он закрывается явно:

- истина кода — проект `C:\Users\malik\gapaudit`;
- `tools/sync_skill.py` собирает автономную копию для скилла: копирует `core/` и CLI-обёртки в
  `scripts/`, где обёртки подкладывают свой каталог в `sys.path` и импортируют `core`;

  Сделано в P4d (фактический контракт скрипта): обёртки копируются **как есть**, а каталог
  `scripts/` в `sys.path` подкладывает сгенерированный `scripts/run.py` — запуск копии:
  `python -m scripts.run <имя>` (`catalog_build`, `catalog_scan`, `check_predecessors`,
  `collect`, `coverage`, `niche`, `signals`, `verify`). Плоские `scripts/*.py` скилла
  (legacy-версии) не трогаются: зеркало — это `scripts/core/`, `scripts/cli/`,
  `scripts/run.py`, `scripts/__init__.py`. Флаги: `--dry-run` (план), `--check` (сравнение
  по sha256; 2 при расхождениях; после успешной проверки запускает
  `python -m scripts.run check_predecessors --help`), `--no-smoke`. Коды: 0 — ок,
  1 — ошибка (нет `SKILL.md`, копия не запускается), 2 — расхождения. `api_keys.json`,
  `tests/`, `data/`, `__pycache__` не копируются никогда;
- скилл остаётся самодостаточным (всё внутри его каталога), путь `AppData` в коде не фигурирует;
- `tools/compat_check.py` запускает старую версию скрипта и новую на фикстурах и сравнивает JSON —
  гейт «метод не изменился незаметно»;
- compat_check имеет два уровня: L1 (pytest-паритетные тесты, ~2 с) и L2 (legacy против new на
  одинаковых данных: legacy — in-process с подменённым транспортом, new — subprocess, сравнение
  ключевых полей JSON с allowlist известных отклонений, ~10 с). Гейт: exit 0 — все зелёные;
  1 — расхождение; 2 — L1 красный; 3 — изменён `legacy/`;
- правило правок: изменение метода сначала делается в проекте, тестируется, потом уезжает в скилл
  синхронизацией одним коммитом;
- compat_check состоит из: `tools/compat_check.py` (оркестратор), `tools/compat_runner.py`
  (фейки для new-подпроцесса), `tools/compat_allowlist.py` (известные отклонения). L2 для
  `catalog_scan` использует legacy-экстракт из `test_core_scan_legacy_contract.py` — legacy-модуль
  исполняется на импорте, subprocess-изоляция каталогов записи — P4, если понадобится.

## 11. Тестирование и demo-режим

- `core` — unit-тесты без сети: HTTP подменяется фикстурой (`core/http.get` инжектируется).
- **Demo-режим** (`--demo`, переменная `GAPAUDIT_DEMO=1`): все ответы берутся из
  `tests/fixtures/`, сеть выключена. Без него нельзя ни тестировать, ни показывать приложение.
  Сделан в P4d: `tools/demo_mode.py` (транспорт-словарь, `DictCache`, `gh`-снимок,
  сид Store), запуск `python desktop.py --demo` — данные приложения во временном каталоге,
  удаляемом при выходе; `settings.json` и `api_keys.json` не читаются. Тесты:
  `tests/test_demo_mode.py` (17, ни одного подпроцесса). До P4d compat_check не зависел от
  сети: фейковый транспорт инжектируется в процесс legacy и new одинаково, через ту же подмену.
- UI-тесты: `httpx`/`TestClient` по эндпоинтам + один тест «задача проходит путь
  queued → running → done» на фикстурах.
- **Интеграционные тесты через реальные подпроцессы** (`@pytest.mark.slow`). Закрывают дыры
  «две подсистемы по отдельности, но не вместе»:
  - `test_web_stream_worker` — POST /api/jobs → реальный JobQueue → реальный subprocess
    (fake_job.py) → SSE-поток → event: done;
  - `test_desktop_browser_smoke` — desktop.py --browser --port <free>, health OK, kill, exit 0.
  В быстром цикле (`-m "not slow"`) не участвуют. Цель: 1–2 таких теста на P1, не больше.
- CLI smoke: шесть команд, фиксированные входные данные, проверка кода выхода и ключевых полей JSON.
- Один живой тест по расписанию (GitHub + PyPI на 3–5 заранее известных именах), чтобы ловить
  изменения чужих API — но он не блокирует сборку.
- Проверка окна: запуск `desktop.py` и `/api/health`; отдельным тестом — что middleware отдаёт 403
  без токена и при чужом `Origin`.
- Бюджет тестов — абсолютные потолки по фазам (**P-phase caps**), а не «+50% к базовому»:
  «базовое» размывается от шага к шагу, поэтому такой формулы больше нет. Превышение —
  стоп-сигнал: сначала оптимизация, потом новые тесты.

  | Фаза | Полный (`pytest -q`) | Быстрый (`pytest -q -m "not slow"`) |
  |---|---:|---:|
  | P0 (закрыт) | ≤ 8 с | ≤ 5 с — факт 5.5 / 2.4 |
  | P1 (закрыт) | ≤ 30 с | ≤ 12 с (на чистой машине) — факт 22.9–26.2 / 8.1–9.7 |
  | P2 | ≤ 40 с | ≤ 20 с |
  | P3 | ≤ 60 с | ≤ 30 с |
  | P4 | ≤ 90 с | ≤ 45 с |
  | P5 | ≤ 120 с | ≤ 60 с |

  Все цифры — на «чистой машине» (без параллельных сборок). Под нагрузкой (сборка пакета,
  параллельный pytest) допустимо +25%. Сигнал регрессии — тренд fast-цикла на чистой машине,
  а не абсолют: гейт на абсолютное время без оговорки «clean machine» ложно-красный
  (AGENTS gotcha 29). Превышение cap на чистой машине — стоп-сигнал к оптимизации.

  Веб-тесты с lifespan не оптимизируем в ущерб изоляции (шаг 3b: каждый такой тест
  несёт `create_app` + lifespan с чистым SQLite — это и есть изоляция, флаки дороже секунд).

  Замер шага 3d-b (19.10.2026): на свободной машине полный 26.7 с (cap 30),
  быстрый 9.1 с (cap 12) — оба в норме. Под нагрузкой (параллельные прогоны, живой
  dev-сервер) те же тесты дают 25–35 с и 17–19 с: абсолютный гейт без оговорки
  «clean machine» ложно-красный (AGENTS gotcha 29). Вклад 3d-b — около 0.5 с.

  Замер шага 2a (19.10.2026): полный 26.5 с (cap P2 — 40), быстрый 10.1 с
  (cap P2 — 20); оба с запасом. Вклад 2a — 8 тестов, ~0.6 с: TestClient +
  шаблоны, ни одного подпроцесса. Под нагрузкой (живое окно, параллельный
  прогон) быстрый цикл доходил до 14.6 с — та же оговорка про «clean machine».

  Факт P2 (2b, 19.10.2026): полный 27.0–27.8 с (cap 40), быстрый 9.9–10.8 с
  (cap 20). База перед 2b, тот же прогон на этой машине: 27.5 / 9.2; добавлено
  17 тестов (+17: matrix 6, templates 4, sse_smoke 3, ui_contract 2,
  env_isolation 2), вклад ~0.7–1.6 с быстрого цикла. Под нагрузкой (живое окно
  WebView2 + Chromium) те же тесты давали 43.8 с полного — абсолютный гейт без
  оговорки «clean machine» ложно-красный (AGENTS gotcha 29).

  P2: полный ≤ 40 с, быстрый ≤ 20 с. Факт (2b): 27.84 / 10.79. Мутационная
  проверка гейтов (ui_contract, env_isolation, no_501) — все три краснеют при
  поломке. Под нагрузкой (WebView2 + Chromium) — до 44 с, это gotcha 29.

  Факт 2c (19.10.2026, чистая машина после закрытия окна): полный 27.58 с
  (cap 40), быстрый 11.09 с (cap 20). База шага на той же машине в начале —
  27.78 / 9.78: 25 новых тестов дали +1.3 с быстрого цикла
  (`test_web_settings.py` — 1.2 с на 20 тестов) и ноль к полному (в пределах
  шума). Под нагрузкой (PyCharm, Kaspersky, Docker Desktop, живое окно WebView2)
  те же прогоны давали 37.6 / 21.3–22.1 — это gotcha 29, а не регрессия.
  Все тесты шага in-memory, ни одного подпроцесса.

  P2 (закрыт): полный ≤ 40 с, быстрый ≤ 20 с. Факт (2c): 27.58 / 11.09.
  Мутационная проверка гейтов (alive>=dead, navigation) — оба краснеют при
  поломке. Под нагрузкой — 37.6 / 21.3, gotcha 29.

  P3: полный ≤ 60 с, быстрый ≤ 30 с. Факт (3b): 32.53 / 18.11. Мутационная
  проверка фильтра alive — 2 теста краснеют. Под нагрузкой — gotcha 29.

  Факт 3c (19.10.2026, та же машина): полный 36.01 с (cap 60), быстрый 18.41 с
  (cap 30). База перед шагом — 31.63 / 15.10: 30 новых тестов дали +4.4 с
  полного и +3.3 с быстрого цикла. Вклад — веб-тесты с lifespan: compare 12,
  export 10, store-миграции 4, CLI diff 2, шаблоны 4; каждый такой тест несёт
  `create_app` + чистый SQLite, и это цена изоляции (см. выше). Мутационная
  проверка: убран fallback имён из `--diff` — `test_diff_fills_names_from_prev_file`
  краснеет (восстановлено). Все тесты шага in-memory, ни одного подпроцесса.
  Под нагрузкой (живое окно WebView2, Chromium, PyCharm, Kaspersky, Docker)
  те же прогоны давали 69–75 с полного при тех же 18–19 с быстрого: медленные
  тесты шага — не они, а `test_jobs_recover` и compat-L2 (gotcha 29).

  P4 (в работе): полный ≤ 90 с, быстрый ≤ 45 с. Факт (P4b): 49.15 /
  29.07–34.67 (P4a: 36.26 / 19.90).
  P4b добавил momentum (3 часа + тесты): core/collectors/momentum.py,
  /api/signals/{package}, поля точки матрицы, UI карточки и матрицы.
  Все тесты шага in-memory, ни одного подпроцесса.

  Факт P4b (20.10.2026, машина под нагрузкой — IDE, антивирус, живой сервер):
  полный 49.15 с (cap 90), быстрый 29.07–34.67 с (cap 45). База перед шагом,
  тот же прогон в worktree коммита P4a: 29.85 / 32.75 — то есть 47 новых
  тестов не сдвинули тренд (вклад ~+1.5 с быстрого цикла: matrix 10, signals 4,
  core momentum 28, шаблоны 5, ключи 6), а разница с 19.96 с в начале сессии —
  загрузка машины в этот час, а не регрессия (AGENTS gotcha 29).

  P4 (закрыт): full ≤90, fast ≤45. Факт (P4c): 42.01 / 23.87–23.99.
  P5 (в работе): full ≤120, fast ≤60. Факт (P5a): 76.79 / 38.96.
  Факт (P5b): 53.63 / 26.59 (53.63 — финальный прогон P5b; 44.00 из раннего
  замера под нагрузкой оставлено в истории отчёта P5b, в гейт не идёт).
  Факт (P5c): 50.42 / 30.97.
  Факт (P5d): 48.40 / 29.82. База перед шагом (быстрый цикл, та же машина):
  44.02 — тренд не вырос, хотя шаг добавил 72 теста; полный до правок не
  мерился (первый замер — уже после). Вклад: core search_interest 54
  (фикстуры Wordstat/Apify, без сети), cli +7, web signals +9, шаблоны +2.
  Факт (P5e): 53.92 / 40.21 — тёплый прогон (1118 тестов, +50: core signals 26,
  web matrix 6, web gap-score 9, settings +3 в core и +3 в web, шаблоны 3).
  Первый прогон после правок был холодным: 85.04 / 47.55; база перед шагом
  (77.97 / 64.08) тоже холодная, поэтому сравнивать можно только «холодный с
  холодным», а тёплые цифры — то, что видит цикл разработки. Оба гейта
  (120/60) пройдены с запасом.
  Факт (P5f-1): 95.38 / 48.47 (тёплый прогон, 1163 теста, +45: cli niche 21,
  web niche 15, default_runner +4, шаблоны 3, экспорт +1, настройки +1).
  База перед шагом (HEAD fb5005e, холодный прогон): 95.52 / 69.57. Гейты 120/60
  пройдены; быстрый цикл не вырос — «тёплый» на 21 с быстрее холодной базы.
  Факт (P4d, 2026-10-21): 68.92 / 53.86 (тёплый прогон, 1191 тест: 1189 passed,
  2 skipped; +26: sync_skill 9, demo_mode 17). База перед шагом, та же машина:
  1144 passed / 19 deselected, 42.12 с быстрого цикла. Гейты 120/60 пройдены
  (быстрый цикл 53.86 при cap 60; тренд +11.7 с дали CLI-тесты демо — они
  запускают `main()` in-process с полным конвейером, ни одного подпроцесса).
  Полный прогон содержит PyInstaller-независимые проверки; сборка `.exe`
  проверяется отдельно, живым прогоном (см. §12).

  P4d-1 (промежуточная упаковка): по P4 caps было бы full ≤90 / fast ≤45, но
  фаза уже P5 — гейт 120/60. Факт (P4d-1): 68.92 / 53.86 (тёплый прогон).
  База до шага: 83.60 / 42.12 (машина была свободнее, поэтому тренд не
  показателен; абсолютные гейты 120/60 пройдены в обоих прогонах).

  P5 (закрыт): full ≤120, fast ≤60. Факт (P5f-2): 65.48 / 45.59.
  P4d-2 (закрыт 2026-10-21) — упаковка без правок кода: тесты и логика те же,
  что в P5f-2; прогон в этом шаге: **1204 passed, 2 skipped за 56.53 с**
  (полный, cap 120) и **1184 passed, 2 skipped, 20 deselected за 35.08 с**
  (быстрый, cap 60). 1206 собранных тестов. Код не менялся, поэтому цифры
  P5f-2 и P4d-2 сравнимы напрямую — тренда нет.

Коллекторы сигналов (P4/P5):

- Каждый коллектор — отдельный модуль с инжектируемым транспортом; тесты на
  фикстурах (реальные ответы API, снятые один раз); сеть в тестах — ноль.
- Graceful degradation: если ключа нет — коллектор возвращает пустой результат,
  сигнал помечается «не настроен», остальное приложение работает. Тест на
  «коллектор без ключа не бросает исключение».
- API-ключи: тест на 0600 (POSIX) / icacls (Windows), тест на «ключ не
  логируется», тест на «ключ не отдаётся через API» (только статус).
- API-ключи не попадают в экспорт БД и в дампы — тест на это.
- Фикстуры внешних API лежат в `tests/fixtures/http/` рядом с существующими
  (demo-режим отдаёт их вместо сети).
- P5: тест gap_score на фикстуре (детерминированная формула + breakdown) и тест
  на то, что при отсутствии части сигналов score считается по имеющимся
  компонентам и это честно помечено.

## 12. Упаковка и запуск

1. Разработка: `uv venv` → `uv pip install fastapi uvicorn jinja2 pywebview pythonnet httpx`
   (+ `pytest` для тестов). Запуск: `uv run python desktop.py`.
2. «Одной командой»: `run.bat` / `run.sh` в корне проекта (активация окружения + `desktop.py`).
3. Готовое приложение: PyInstaller **onedir** (не onefile — в этом проекте важна предсказуемость
   распаковки и загрузки WebView2), иконка, версия в `meta`. Опыт из скилла про PyInstaller для
   Windows: DLL держать в `_internal`, не чистить папку при запуске, тестировать на чистой машине.

   Сделано в P4d: `gapaudit.spec` (onedir, `console=False`), extra
   `uv pip install -e ".[packaging]"`, сборка `uv run pyinstaller gapaudit.spec` —
   2 м 15 с, `dist/gapaudit/` 37 МБ. В `datas` — `web/views`, `web/static`,
   `docs/API-KEYS.md` и `tests/fixtures` (фикстуры нужны демо-режиму внутри
   `.exe`: пути в `app/web.py` считаются от `__file__`, а в frozen-сборке это
   `_internal/`). Иконки нет — `docs/icon.ico` в проекте не существует, spec
   собирается без неё. Проверка «чистой машины»: копия `dist/gapaudit/` в
   отдельном каталоге, `gapaudit.exe --browser --port 8813` → `/api/health` 200
   (`db_ok: true`), `/api/matrix` 200, `/static/style.css` 200,
   `/docs/API-KEYS.md` 200; `--demo` в том же каталоге отдаёт 6 точек матрицы,
   2 каталога и 8 событий.

   P4d-2 (закрыт 2026-10-21): пересборка из HEAD `24d7e69` (внутри — P5f-2).
   `rm -rf dist/gapaudit build/gapaudit` → `uv run pyinstaller gapaudit.spec`:
   exit 0, **34.8 с**, `dist/gapaudit/` **37 МБ** (37 391 700 байт). Проверка
   «чистой машины» (копия в отдельный каталог, `gapaudit.exe --browser
   --port 8814` с `GAPAUDIT_DEMO=1` и своим `GAPAUDIT_DATA_DIR`, чтобы читать
   `session.token`): `/api/health` 200 (`db_ok: true`, `version 0.1.0`),
   `/api/matrix` 200 — **6 точек, у всех `gap_score`** (fastmcp 25.0,
   mypy 50.1, requests 82.1), `/niche` 200 (форма и фильтры в разметке),
   `POST /api/niche/find` → **503** (демо), `POST /api/signals/collect` → 202,
   `POST /api/niche/export` с чужим `job_id` → 404, `/static/style.css` 200,
   `/docs/API-KEYS.md` 200. Сид демо пишется после `session.token`, поэтому
   матрицу в проверке читаем с ожиданием (иначе гонка и «0 точек»).

   **Найдено в P4d-2 (не исправлялось: код шага заморожен) — задачи в `.exe`
   не выполняются.** `_default_runner` собирает argv как
   `[sys.executable, "-m", module, ...]`, а в frozen-сборке `sys.executable` —
   сам `gapaudit.exe`, у которого нет флага `-m`. Живая проверка:
   `POST /api/signals/collect` → 202, через 3 с задача `failed`,
   `error="exit code 2"`. Значит, в собранном приложении не работают
   `packages.check`, `catalog.build`, `registry.scan`, `github.verify`,
   `monitor.diff`, `signals.collect`, `niche.find` (всё, что идёт
   подпроцессом). Экраны, матрица, история, настройки, API-ключи и demo-режим
   работают. Фикс — диспетчер CLI в `desktop.main()` до `argparse` для
   frozen-сборки (перехват `-m` и вызов `main()` модуля) + тест + пересборка;
   отдельный шаг (P4d-3/P6), в P4d-2 не делался — правка кода была запрещена.

4. Зависимость на WebView2 Runtime: на этой машине есть, на чужой может не быть — либо требование
   в README, либо установщик Evergreen Runtime как шаг установки.
5. Docker — опционален: нужен только для режима «без окна» (`--browser`), полезен для серверных
   прогонов по расписанию.
6. Данные: по умолчанию `%USERPROFILE%\GapAudit-data\` (база, логи, каталоги) — не в проекте и не в
   `AppData\Local\hermes`, чтобы пользователь мог открыть папку в проводнике (это уже было
   проблемой со скиллом). Суффикс `-data` отличает каталог данных от одноимённого каталога
   проекта; путь меняется в настройках (`--data-dir`, `GAPAUDIT_DATA_DIR`), guard не даёт
   указать корень проекта (§16, п. 1).

## 13. Этапы, оценки и критерии готовности

| Этап | Содержание | Оценка | Готово, когда |
|---|---|---|---|
| P0 | рефакторинг в `core`, SQLite-схема, кэш в БД, CLI-обёртки, smoke-тесты CLI | 2 дня | шесть CLI дают прежний результат, `core` импортируется без сети, схема создаётся с нуля |
| P1 | очередь задач (подпроцессы, статусы, отмена, логи), SSE, эндпоинты задач | 3 дня | задача `packages.check` доходит до `done`, переживает перезапуск, отменяется, прогресс виден в SSE |
| P2 | pywebview-оболочка (порт, токен, health, закрытие), экраны «Задачи» и «Сейчас решать» | 2 дня | окно открывается одной командой, матрица строится из БД, кнопка «обновить» запускает задачу |
| P3 | «Пакеты», «Каталоги», «Мониторинг», «Сравнение», фильтры, экспорт CSV/MD | 4 дня | каталог на 300+ записей листается, события собираются в таймлайн, экспорт открывается в Excel |
| P4 | упаковка onedir, sync со скиллом, demo-режим, README «Установка на чистой машине», API Keys Store, momentum, cross-ecosystem | 6 дней | приложение запускается на чистой машине из `dist\`, скилл обновлён синхронизацией, `compat_check` зелёный, ключ вводится в UI и проверяется |
| P5 | сигналы ниш: jobs (Adzuna), patents (PatentsView), funding (SEC EDGAR), search_interest (Yandex Wordstat / Google Trends), gap_score, экран «Поиск ниши» | 13–14 дней | сигнал появляется в карточке пакета, без ключа приложение работает, gap_score в матрице |
| P6 | **Долгосрочный план (отложен 2026-10-21)**: MCP-сервер (stdio), расписание внутри приложения, уведомления, тёмная тема | 3–5 дней | задача по расписанию выполняется без открытого окна |

Итого до P4: **около 17 рабочих дней** (было 14 — добавлены API Keys Store,
momentum, cross-ecosystem), из них половина — P0–P1, то есть не UI. До P5 — ещё
13–14 дней, до P6 — ещё 3–5.

P4a (закрыт 2026-10-20) — API Keys Store, 792 теста.

P4b (закрыт 2026-10-20) — momentum, 839 тестов.

P4c (закрыт 2026-10-20) — cross-ecosystem, 884 теста.

P5a (закрыт 2026-10-20) — jobs (Adzuna).

P5b (закрыт 2026-10-20) — patents (PatentsView).

P5c (закрыт 2026-10-20) — funding (SEC EDGAR).

P5d (закрыт 2026-10-20) — search_interest (Wordstat + Apify).

P5e (закрыт 2026-10-20) — Gap Score (веса в Settings + UI, размер точки).

P5f-1 (закрыт 2026-10-20) — Niche Finder, первая итерация: `cli/niche.py`
(`kind = niche.find`), `POST /api/niche/find`, `GET /api/niche/result/{job_id}`,
экран `/niche`, 11-я колонка `gap_score` в экспорте матрицы. Фильтры, карточка
и экспорт из Niche Finder — P5f-2.

P4d-1 (закрыт 2026-10-21) — упаковка (промежуточно): PyInstaller onedir
(`gapaudit.spec`, сборка 2 м 15 с, `dist/gapaudit/` 37 МБ, пакет проверен в
отдельном каталоге), `tools/sync_skill.py` (32 файла в скилл, копия
запускается), demo-режим (`--demo`/`GAPAUDIT_DEMO=1`, данные из фикстур, сеть
выключена), CI (pytest + mypy + compat + build), README (установка из `.exe`,
demo, CI, статус).

P5f-2 (закрыт 2026-10-21) — Niche Finder до конца: momentum из `Store`
(read-only, graceful degradation), `stats.found`/`stats.checked` на экране
(«Проверено N из M»), фильтры результата (momentum > 0, без MCP, gap ≥ X),
`POST /api/niche/export` (CSV `utf-8-sig` / Markdown), кнопка «Собрать» в
карточке для несобранных сигналов, 503 для `/api/niche/find` в demo-режиме,
job `build` в CI (windows-latest, PyInstaller + smoke-тест по `/api/health`).

P4d-2 (закрыт 2026-10-21) — пересборка `.exe`, **P4 закрыт**: сборка из HEAD
`24d7e69` (внутри — все фичи P5f-2), проверка в отдельном каталоге
(`/api/health` c `db_ok: true`, `/api/matrix` — 6 точек с `gap_score`, `/niche`
200, `POST /api/signals/collect` → 202, `POST /api/niche/find` → 503 в демо,
`POST /api/niche/export` с чужим `job_id` → 404), README обновлён («P4 закрыт»,
«P5 закрыт», что нового). Код не менялся — только упаковка.

## 14. Риски и что с ними делать

| Риск | Реальность | Что делаем |
|---|---|---|
| WebView2 отсутствует на чужой машине | На этой есть 155.x, у других может не быть | Проверка при старте + понятное сообщение со ссылкой на Evergreen Runtime; в P4 — шаг установки |
| pywebview и потоки | Требует главного потока; бэкенд обязан быть в демоне | `webview.start()` только в `main`, uvicorn в `daemon=True`; проверка ожиданием `/api/health` |
| Лимиты чужих API | GitHub Search 30/мин, PyPI-статистика отдаёт 429 | Троттлинг по хосту, очередь, показ «ждём лимит N с», повторы с backoff (уже есть в коде) |
| Starlette 1.9 и `httpx2` | При импорте `TestClient` Starlette 1.9 предупреждает «install httpx2 instead»; `httpx` закреплён `<0.29` | Предупреждение косметическое, тесты зелёные; миграция на `httpx2` — отдельный шаг 6, только если пакет станет стабильным |
| Крэш или закрытие во время обхода | Обход реестра — 40 минут | Подпроцессы + чекпоинт каждые 25 страниц + статус `interrupted` и продолжение с курсора |
| Каталог реестра огромен (158 062 записи) | В DOM не влезет ни при какой вёрстке | Хранение в SQLite, серверная пагинация и фильтры, экспорт вместо прокрутки |
| Устаревший кэш как «свежие данные» | TTL 12 часов, уже путало вывод в CLI | Метка «данные от HH:MM · обновить» на всех экранах, кнопка принудительного обновления |
| Шумные вердикты на коротких именах | «uv», «ruff» дают мусорные находки | Всегда показывать строку находки рядом с вердиктом, а не только вердикт |
| Два места правды (проект и скилл) | Копии расходятся | `sync_skill.py` + `compat_check.py` как обязательный гейт перед коммитом |
| Соблазн «доработать по ходу» | Объём вырастет вдвое | Замораживаем экраны после P2; новые идеи — в раздел «после P5» |
| SSE `done` vs запись observations (gotcha 38) | Клиенты делают reload с задержкой 400 мс | При добавлении новых хуков на `done` — проверить, что задержка достаточна (в 2b замер: запись укладывается в 50–100 мс на свободной машине, 400 мс — с запасом) |
| Лимиты и ToS внешних API (Adzuna, PatentsView, EDGAR, Yandex Wordstat, Google Trends) | Adzuna: 25 hits/мин, 250/сутки, 1000/нед, 2500/мес; EDGAR: 10/сек; Yandex Wordstat: квота (по данным сообщества 10/сек, 1000/сутки); PatentsView: 45/мин; Google Trends (Apify) — оплата за результат | Троттлинг по хосту (уже есть в `core/http`), кэш ответов, показ «ждём лимит N с», при ошибке — пустой результат, приложение работает |
| ToS Adzuna запрещает агрегацию счётчиков вакансий для постоянных исследований без письменного согласия | Free tier — только «Personal or academic research» с атрибуцией «The Adzuna API» | Не собирать вакансии в аналитику без письменного согласия Adzuna; в UI показывать источник и дату; при претензиях — отключить сигнал `jobs` |
| Формат ответов внешних API может измениться | `compat_check` защищает только от расхождений с legacy, не от чужих API | Коллектор при сбое возвращает `[]` и статус `error`; тесты на фикстурах — снятые ответы, а не живые вызовы |
| EDGAR требует User-Agent с email | Без него — 403 | Хранить в `api_keys.json` → `providers.edgar.meta.user_agent`; подставлять заголовок |
| Yandex Wordstat требует OAuth-токен и ClientId | Заявка на доступ, квота, `429` с `Time to refill` | Хранить `encrypted_value` = OAuth-токен, `meta.client_id` = ClientId; при 429 ждать указанное время |
| Google Trends: официальный API — alpha по заявке | Доступ выдают ограниченному числу разработчиков | Альтернатива — Apify (платно): API-токен Apify + `actor_id`; пользователь выбирает сам |
| PatentsView (NIH/NSF, `search.patentsview.org`) — НЕ USPTO (`api.uspto.gov`) | Это разные API, и ключи у них не взаимозаменяемы | В текущей версии подключён только PatentsView. Ключ — с `patentsview.org/apis/key-request`. Не путать при получении ключа (§18.2, `docs/API-KEYS.md`) |
| Утечка ключей при копировании БД | БД копируют целиком | Ключи — в отдельном файле `<data_dir>/api_keys.json` (0600), в SQLite их нет; при экспорте БД ключи не утекают |
| «Шифрование» ключей — base64, а не криптография | Base64 — обратимо без ключа | P4 — base64; P5 — опционально Fernet с ключом, производным от machine-id. Честно документировать: «ключи защищены правами файла, не шифрованием» |
| Гео (PyPI by country / BigQuery) | Отклонено | Не реализуем; в §16 п. 6 закрыто как «отклонено» |

## 15. Метрики, по которым судим об успехе

- время от запуска приложения до ответа на вопрос «что делать» — секунды (из кэша), а не 10–20 минут терминальных прогонов;
- доля задач, не потерявших прогресс после перезапуска — цель 100% (через чекпоинты);
- число наблюдений в `observations` (история растёт — значит инструмент используют, а не открыли один раз);
- сколько раз сработало событие «появился MCP» — это единственная метрика, которая напрямую меняет решения;
- CLI по-прежнему зелёный: `compat_check` проходит, коды выхода не изменились.

## 16. Открытые вопросы (нужны решения до P2)

1. Каталог данных: по умолчанию `%USERPROFILE%\GapAudit-data` (с суффиксом `-data`, чтобы не
   совпадать с каталогом проекта). Переопределяется через `--data-dir` и `GAPAUDIT_DATA_DIR`.
   Guard: `resolve_data_dir()` бросает `ValueError`, если путь совпадает с корнем проекта. [решено 19.10.2026]
2. Нужны ли графики (локальный Chart.js) или достаточно таблиц и таймлайна?
3. Расписание: внутри приложения или оставить внешнему cron (Hermes) с вызовом CLI?
4. Язык интерфейса — только русский или нужен английский (для будущей публикации)?
5. Порог «живости» по умолчанию: оставить универсальный `100/нед` или пресеты «узкая ниша» / «массовая»?

6. Гео (PyPI by country / BigQuery) — ОТКЛОНЕНО. Причина: узкая применимость
   (только PyPI), требует GCP-аккаунт, ценность ниже, чем momentum + jobs +
   search_interest. [отклонено 20.10.2026]
7. API-ключи: хранить в `api_keys.json` (0600) или в SQLite? РЕШЕНО: в
   `api_keys.json`, НЕ в БД — чтобы экспорт БД не утекал и `core/` не зависел от
   `sqlite3`. Таблицы `api_keys` в схеме нет (§5, миграция 3). [решено 20.10.2026]
8. Приоритет сигналов: momentum → cross_ecosystem → jobs → patents → funding →
   search_interest → gap_score. ОБОСНОВАНО: momentum и cross_ecosystem
   работают на существующих данных (P4, без ключей), jobs — бесплатный API с
   регистрацией (P5), patents — бесплатный API (P5), funding — «грязный» SEC
   EDGAR (P5), search_interest — зависит от Yandex Wordstat (бесплатно, по
   заявке) и Google Trends (alpha или платно) (P5). [решено 20.10.2026]

## 17. Что делаем первым делом

1. `uv venv` и зависимости, проверка, что пустое окно pywebview открывается (15 минут — снимает риск «не заведётся»).
2. P0: разнос существующих скриптов на `core` + обёртки, smoke-тест CLI — без него UI будет строиться на песке.
3. SQLite-схема и запись наблюдений из `check_predecessors`.
4. P1: очередь и SSE.
5. P2: окно и первый настоящий экран — матрица на данных, которые уже собраны.

## 18. Сигналы ниш и пользовательские API

### 18.1. Принципы

- Сигналы ДОПОЛНЯЮТ базовые данные (adoption, flag, verdict), не заменяют их.
  Базовая матрица работает всегда, даже без ключей.
- Каждый коллектор — независимый модуль в `core/collectors/`. Отказ одного не
  ломает другие.
- Graceful degradation: нет ключа — нет сигнала, остальное работает.
  Пользователь видит «сигнал не настроен» со ссылкой на `docs/API-KEYS.md`.
- Никакого LLM. Все сигналы — детерминированные запросы к API + арифметика.
- `core/` остаётся чистым: коллекторы получают транспорт и данные параметрами,
  про `sqlite3` и про `app/` не знают (gotcha 19). Чтение из БД — в `app/`.

### 18.2. API Keys Store

**Где хранится:** `<data_dir>/api_keys.json` (права 0600), НЕ в SQLite — чтобы
при экспорте БД ключи не утекали (решение — §16 п. 7).

**Формат файла:**

```json
{
  "version": 1,
  "providers": {
    "adzuna": {"encrypted_value": "<base64: app_id + app_key>", "added_at": "...",
                "meta": {}},
    "patentsview": {"encrypted_value": "<base64>", "added_at": "...", "meta": {}},
    "edgar": {"encrypted_value": "", "added_at": "...",
              "meta": {"user_agent": "GapAudit your-email@example.com"}},
    "yandex_wordstat": {"encrypted_value": "<base64>", "added_at": "...",
                         "meta": {"client_id": "..."}},
    "google_trends": {"encrypted_value": "<base64>", "added_at": "...",
                      "meta": {}},
    "apify": {"encrypted_value": "<base64>", "added_at": "...",
              "meta": {"actor_id": "..."}}
  }
}
```

**Шифрование:** P4 — base64 (не крипто, но не plaintext). P5 — опционально
Fernet с ключом, производным от machine-id. Честно документировать: «ключи
защищены правами файла, не шифрованием».

**Что секретное, а что `meta`:** `meta` — несекретные поля
(`yandex_wordstat.client_id`, `apify.actor_id`, `edgar.user_agent`). Всё, что
является паролем или его половиной (`adzuna.app_id`, `adzuna.app_key`,
`uspto.api_key`, `yandex_wordstat.token`, `google_trends.api_key`,
`apify.api_token`) — в `encrypted_value`. Adzuna использует `app_id` + `app_key`
как пару секретов: `app_id` не даёт доступа отдельно, но и открытым текстом не
хранится. `meta` — публичный канал (возвращается в `GET` как `meta_public`),
секреты туда не попадают.

**Провайдеры и зачем:**

- `adzuna` — jobs (спрос на труд по странам). Ключ: `app_id` + `app_key`.
  Лимиты free tier: 25 hits/мин, 250 hits/сутки, 1000/нед, 2500/мес. ВАЖНО: ToS
  Adzuna запрещают агрегацию счётчиков вакансий для постоянных исследований без
  письменного согласия; для «Personal or academic research» нужна атрибуция
  «The Adzuna API» со ссылкой.
- `patentsview` — patents (патентный импульс). Ключ: `api_key` (бесплатно, заявка
  на patentsview.org). Лимит: 45 запросов/мин (уточнить при подключении).
  Сам API — PatentsView (NIH/NSF): имена сервисов и что вводить — в
  `docs/API-KEYS.md`.
- `edgar` — funding (раунды). Не ключ, а User-Agent с email (требование SEC).
  Хранить в `meta.user_agent`.
- `yandex_wordstat` — search_interest (динамика и регионы, РФ). Бесплатно, по
  заявке. Нужны OAuth-токен и ClientId. Квота: при превышении — `429` с
  `Time to refill: N seconds`.
- `google_trends` — search_interest (динамика, глобально). Официальный API —
  alpha по заявке (бесплатно, но ограниченный доступ). Альтернатива — платные
  Apify/DataForSEO (пользователь решает сам).
- `apify` — обёртка для Google Trends через Apify (платно, но сразу). Нужен
  API-токен Apify и `actor_id` для Google Trends Scraper.

**UI:** Настройки → API-ключи. Список провайдеров, поле ввода, кнопки
«Сохранить» / «Проверить» / «Удалить». Статус: «не настроен» / «настроен» /
«проверка пройдена» / «ошибка». Ссылка на `docs/API-KEYS.md`. Значения ключей
через API не отдаются — только статус.

### 18.3. Сигналы

#### Momentum (P4, 3 часа)

**Что:** Δ adoption за 7/30/90 дней по истории observations. Не «сколько
скачиваний», а «как быстро растёт».

**Источник:** уже есть в Store — `get_observations_history(name, limit=...)`.

**Формула:** `(latest - prev) / prev * 100` за окно, где `prev` — ближайший
замер, отстоящий от `latest` на 7/30/90 дней. Если истории меньше окна —
меньше точность, честно показывать «N замеров».

**UI:** в карточке пакета — числа и стрелки (↑ +40%/30д, ↓ -5%/7д). В матрице —
цвет точки (зелёный/серый/красный). В `GET /api/matrix` — поле `momentum_30d`.

**Стоимость:** 3 часа (запрос + UI).
**Без ключа:** работает всегда (данные уже есть).

#### Cross-ecosystem momentum (P4, 1–2 дня)

**Что:** сравнение adoption для одного «концепта» в PyPI / npm / GitHub.
Например, «vector-db»: PyPI +40%, npm +5%, GitHub +20% → Python-экосистема
обгоняет JS.

**Источник:** таблица `concept_packages` (mapping вручную или через эвристику)
+ существующие observations.

**Формула (уточнение P4c, реализовано в `core/collectors/cross_ecosystem.py`):**
для пакета `p` с concept `C`:

- найти все пакеты `p'` в **других экосистемах** с тем же concept `C`
  (`concept_packages`; своя экосистема — не соперник, иначе «PyPI обгоняет
  npm» мерялось бы с другим PyPI-пакетом);
- для каждого `p'` вычислить momentum (7/30/90 через `compute_momentum` из
  observations); у `p'` без истории — `momentum_30d: null`, а не 0;
- `cross_ecosystem = {concept, ecosystem (своя), peers: [{name, ecosystem,
  momentum_30d, adoption_week}], comparison, self_momentum_30d,
  self_adoption_week}` — свои значения (`self_*`) отдаются в том же объекте,
  чтобы карточка рисовала «3 полосы» одним ответом;
- `comparison` — **сравнение относительных momentum, не деление на ноль**:
  `faster` если `momentum_30d(p) > max(peers) + 5`, `slower` если
  `momentum_30d(p) < min(peers) − 5`, `same` в пределах порога ±5 (шум, как в
  momentum), `unknown` — если ни у пакета, ни у соседей momentum не посчитан.
  В UI токены переводятся: `faster` → «растёт быстрее всех», `slower` →
  «отстаёт»;
- у пакета **нет concept** → `cross_ecosystem = None` (сигнал «не настроен»);
  concept есть, но нет соседей → объект с пустыми `peers` и
  `comparison = "unknown"` (сигнал настроен, данных мало). UI различает:
  «не настроен» vs «только в этой экосистеме»: показывать таблицу с одной
  своей строкой честнее, чем прятать блок.

**UI:** в карточке пакета — 3 полосы (PyPI/npm/GitHub) с Δ adoption. В
`GET /api/signals/{package}` и в signals карточки (`GET /api/packages/{name}`)
— поле `cross_ecosystem`.

**Стоимость:** 1–2 дня (mapping + UI).
**Без ключа:** работает всегда (данные из observations).

#### Jobs (P5, 2 дня)

**Что:** количество вакансий по ключевому слову, по странам. Прямой гео-сигнал
спроса на труд: «в Германии 200 вакансий с Rust, в Италии — 12».

**Источник:** Adzuna API (`https://api.adzuna.com/v1/api/jobs/{country}/search/1`),
покрытие: UK, US, DE, FR, NL, AU, BR, IN, IT, ES, CA.

**Формула:** `count` по каждой стране, top-5. Хранить в
`signal_observations(signal_kind='jobs', detail=json)`.

**UI:** в карточке пакета — top-5 стран с числом вакансий. В
`GET /api/signals/{package}` — `jobs` (объект `{total, by_country:
[{country, count, label}], configured, error}` | `null`) +
`signals_status.jobs`.

**Стоимость:** 2 дня (клиент + UI + тесты на фикстурах).
**Без ключа:** сигнал «не настроен», приложение работает.

#### Patents (P5b, 2–3 дня)

**Что:** патентный импульс — растёт или падает число filings по теме за 3 года.

**Источник:** PatentsView API (`https://search.patentsview.org/api/v1/patent/`),
POST с JSON `{q, f, o}`, заголовок `X-Api-Key`, лимит 45 запросов/мин (§14).
НЕ путать с `api.uspto.gov` — это другой API. Провайдер: `patentsview`
(`api_key`). Формат ответа проверен по исходникам PatentsView/PatentSearch-API:
`total_hits` (сколько записей совпало — что нам и нужно), `count`/`error`;
опция пагинации — `o.size` (`per_page` старого API здесь молча игнорируется;
проверено по исходникам PVAPIViews.py — параметр читается как `size`).

**Формула (реализовано в `core/collectors/patents.py`):** для каждого
завершённого календарного года окна `[current − 3, current − 1]` — POST с
`patent_date` в этом году, берётся счётчик; `impulse = (count(последний год) /
count(базовый год) − 1) · 100 %`. Окно — завершённые годы, а не «последние
12 мес»: раскатывающееся окно давало бы неполный текущий год и ложный спад.
Нет базы (0 в базовом году), не оба конца окна собраны или успешных лет < 2 —
`impulse_pct: null`. Хранить в
`signal_observations(signal_kind='patents', value=total, detail=json)` —
`detail` = `{total, by_year: [{year, count}], impulse_pct, configured, error}`.
Три состояния (§0.3): нет ключа — `configured=False` (запросов нет);
часть годов упала — `error=null`, `total` по успешным; все упали —
`error='all requests failed'`, `configured=True`.

**UI:** в карточке — блок «Patents · патентный импульс»: таблица по годам +
число патентов + строка источника + «Импульс: +40% за 3 года». Три
состояния различаются по `signals_status` (см. также §0.4 ниже).

**Стоимость:** 2–3 дня (клиент + парсинг + UI).
**Без ключа:** «не настроен», приложение работает.

Кнопка «Собрать» в карточке пакета — P5f (Niche Finder).
В P5a–e блок сигнала только показывает статус и данные, если
они есть. UI различает not_configured (ключ не настроен,
ссылка на Настройки) и not_collected (ключ настроен, но
сигнал не собирали — текст «ещё не собирали», кнопка — P5f).

#### Funding (P5, 3–5 дней)

**Что:** раунды финансирования компаний, упоминающих технологию. «В этой нише
3 раунда за квартал» = горячо.

**Источник:** SEC EDGAR — полнотекстовый поиск
(`https://efts.sec.gov/LATEST/search-index?q=...&forms=D`; Form D — уведомления
о предложениях с суммой). Провайдер: `edgar` (User-Agent с email, не ключ).

**Формула:** count filings за квартал, суммарный объём (если указан). Хранить в
`signal_observations(signal_kind='funding', detail=json)`.

**Реализовано (P5c, `core/collectors/funding.py`):** GET
`https://efts.sec.gov/LATEST/search-index?q=…&forms=D&dateRange=custom&startdt=…&enddt=…`
по каждому кварталу окна; счётчик — `hits.total.value` (проверено живым
запросом 20.10.2026; без `User-Agent` сервер отвечает 403). Окно — последние
4 **завершённых** календарных квартала (текущий неполон и дал бы ложный спад,
то же правило, что у окна лет в patents). `trend_pct = (count(последний
квартал) / count(первый квартал) − 1) · 100`; `null`, если база 0, не собраны
оба конца окна или успешных кварталов < 2. Между запросами — пауза 0.11 с
(лимит SEC 10/сек, §14); сон инжектируется, тесты не ждут.

**Счётчики, не разбор filing'ов:** P5c реализует СЧЁТЧИКИ Filing Form D
(`hits.total.value` из `efts.sec.gov` search-index), не разбор XML filing'ов.
Детальные раунды (компания, сумма, дата) — замысел плана, отложены в P5f или
отдельный шаг, если понадобится. Счётчики разрежены по природе: Form D
содержит имена эмитентов и адреса, технослова редки (проверено: rust — 8,
requests — 0). В P5e при весах gap_score учитывать низкую заполняемость
funding.

**Окно:** завершённые кварталы `[current_q − quarters_back, current_q − 1]`,
не включая текущий (неполный) квартал. Формула
`trend_pct = (count(last_q) / count(first_q) − 1) × 100`; `None`, если база 0
или успешных кварталов < 2. Это согласуется с окном patents (завершённые
годы).

**Отличие от замысла:** счётчики по кварталам, без сумм раундов и имён
компаний — они лежат в XML самих filing'ов, и их разбор (нормализация
разнородного XML, оценка 3–5 дней в §18.4) — отдельная работа после P6, не
P5c. `configured=False` (= `not_configured` в UI) означает «User-Agent
EDGAR не введён», а не «раундов нет».

**UI:** в карточке — блок «Funding · раунды»: таблица «квартал → filing'ов»,
строка источника («SEC EDGAR») и «Тренд: +X% за 4 квартала»; три состояния
по `signals_status`.

**UI (замысел плана):** последние раунды (компания, сумма, дата) — в P5c не
реализовано (см. «Отличие от замысла» выше): карточка показывает счётчики по
кварталам, без сумм и имён.

**Стоимость:** 3–5 дней (EDGAR отдаёт XML/JSON неоднородно, нужна нормализация).
**Без ключа:** «не настроен».

#### Search interest (P5, 3–4 дня)

**Что:** динамика поискового интереса и разбивка по регионам для ключевой фразы
ниши. «В этой стране интерес растёт, в этой — стагнирует».

**Источники (пользователь выбирает, какой API настроить):**

- **Yandex Wordstat** — бесплатно, по заявке. OAuth-токен + ClientId. Даёт
  частотность, динамику, срезы по регионам и устройствам. При превышении квоты —
  `429` с `Time to refill: N seconds`. Идеален для РФ.
- **Google Trends (официальный)** — alpha по заявке (бесплатно, но
  ограниченный доступ). Данные consistently scaled: это абсолютный объём
  интереса, а не индекс 0–100 из веб-интерфейса.
- **Google Trends через Apify** — платно, но сразу. Нужен API-токен Apify и
  `actor_id` для Google Trends Scraper. Пользователь решает, платить или нет.

**Формула:** для каждой страны/региона — значение интереса, динамика за
12 месяцев. Хранить в `signal_observations(signal_kind='search_interest',
detail=json)` с разбивкой по регионам.

**UI:** в карточке пакета — график динамики (12 мес) и top-5 регионов/стран. В
`GET /api/signals/{package}` — `search_interest: {source, regions: [{region,
value}], trend: [...]}`.

**Стоимость:** 3–4 дня (два разных клиента + нормализация + UI).
**Без ключа:** «не настроен».

**Реализовано (P5d, `core/collectors/search_interest.py`):** два независимых
клиента в одном сигнале, оба собираются, если настроены (не «или», а «и»).

- Wordstat — `POST https://api.wordstat.yandex.net/v1/topRequests` по каждому
  из 5 регионов (`regions: [id]`, `devices: ["all"]`, `Authorization: Bearer`),
  счётчик — `totalCount`, `points` — top-5 по убыванию, `total` — сумма по всем
  успешным регионам. Две правки относительно псевдокода шага, обе по
  документации Яндекса: параметр называется **`regions`** и это СПИСОК чисел (в
  псевдокоде — `region` строкой), а `client_id` в запросе не передаётся вовсе
  (он привязан к токену при регистрации приложения) — остаётся обязательным
  полем учётных данных и условием «настроен».
- Google Trends — один синхронный запуск актора Apify
  (`run-sync-get-dataset-items`, тело `{searchTerms: [query], geo: "",
  timeRange: "today 12-m"}`), ответ — датасет (список элементов): `points` из
  `interestByRegion`, тренд — по ряду `interestOverTime`, `total` — сумма ряда.
- `trend_pct` у Wordstat — `null`: `topRequests` отдаёт срез за 30 дней, а ряд
  за 12 месяцев живёт в `/v1/dynamics` (другая форма ответа и ещё один запрос
  на провайдера) — отложено, зафиксировано в отчёте P5d.
- Официальный Google Trends alpha (вариант A) **не реализован**: ключ вводится
  в Настройках, но коллектора нет — CLI отдаёт источник с `error` =
  «google_trends official alpha not implemented yet (только Apify)», чтобы
  введённый ключ не выглядел бесполезным.

Три состояния у каждого источника не смешиваются: нет ключа —
`configured=false` (запросов нет); часть регионов упала — `error=null`, `total`
по успешным; всё упало — `error='all requests failed'`, `total=0`.

**Wordstat `trend_pct = None` (уточнение P5e):** `/v1/topRequests` — срез за 30
дней, ряда за 12 месяцев в нём нет. Динамика — `/v1/dynamics` (другая форма
ответа, follow-up). Если пользователь настроил только Wordstat — график
динамики не строится, показывается только срез top-регионов. Google Trends
через Apify — график динамики есть (`interestOverTime`).

**Составной статус (уточнение P5e).** `signals_status.search_interest` —
составной:

- `"ok"` если хотя бы один источник `configured=True` (даже если у него
  `error` без points — ключ валидный, API не ответил: это не «не настроен»);
- `"not_configured"` если ни один источник не настроен;
- `"not_collected"` если observation нет.

Это согласуется с gotcha 66 (AGENTS.md): `configured=False` + `error` — не
«не настроен», а «настроен, но ошибка». UI различает.

**UI (P5d):** блок «Search interest · поисковый интерес» с двумя подблоками —
«Yandex Wordstat (РФ)» и «Google Trends (глобально)»; в каждом таблица top-5
регионов + «Тренд 12 мес: +X%» (если источник его даёт), либо «Не настроен —
см. Настройки → API-ключи», либо «Ошибка: …», либо «Данных нет». Если не
настроен ни один источник — одна строка «Ни один источник не настроен».

#### Gap Score (P5, 1–2 дня)

**Что:** интегральная метрика 0–100. Ответ на вопрос «ниша свободна?» одним
числом.

**Формула (веса в Settings, настраиваемые):**

```
score_raw = w_adoption * norm(adoption_week)
          + w_momentum * norm(momentum_30d)
          + w_no_flag  * (1 if flag == False else 0)
          + w_jobs     * norm(jobs_total)
          + w_search   * norm(search_interest_trend)
          + w_funding  * norm(funding_count)      # вес отрицательный
          + w_patents  * norm(patents_percent)    # вес отрицательный

score = 100 * score_raw / sum(|w_i|)   # нормировка в 0..100
```

`norm` — min-max по всем пакетам. Веса по умолчанию: adoption=1, momentum=1,
no_flag=2, jobs=0.5, search=0.3, funding=-0.5, patents=-0.3. Знак веса задан
явно: отрицательный вес уменьшает score (то есть «ниша занята»). Компоненты, для
которых сигнал не настроен, не участвуют в сумме, а `score_raw` нормируется по
сумме модулей ИСПОЛЬЗОВАННЫХ весов — и в UI честно показывается, сколько
компонент учтено («учтено 3 из 7 компонент»).

**UI:** в матрице — размер точки = gap_score. В карточке — breakdown по
компонентам (что дало плюс, что минус) и список пропущенных компонент.

**Стоимость:** 1–2 дня (агрегация + UI).
**Без ключей:** работает частично (momentum + no_flag + adoption), без
jobs/patents/funding/search_interest.

**Реализовано (P5e, `core/signals.py`):** `GapScore`, `GapComponents`,
`GapWeights`, `Normalizer`, `compute_gap_score`, `compute_gap_scores_batch`,
`default_weights_from_settings`. Веса — поля `Settings.gap_weight_*` (дефолты
§18.3 выше) и такие же поля в форме «Настройки → Пороги»; `PUT /api/settings`
принимает только конечные числа (NaN/inf — 400).

Решения, которых в формуле не было, и почему:

- **усечение в 0..100**: при штрафных весах сумма бывает отрицательной
  (минимум −0.8/5.6 ≈ −14 при дефолтах), а шкала обещана 0..100 — «занято по
  всем штрафным компонентам» выражается нулём;
- **вырожденный диапазон** (одно значение в выборке, min == max) даёт norm 0.5,
  а не деление на ноль;
- **`None` вместо нуля**: сигнал, которого нет, и сигнал с `error` (gotcha 66)
  не входят ни в сумму, ни в знаменатель — «ключ есть, API не ответил» не
  понижает балл;
- **выборка нормировки** — точки матрицы (`adoption_week > 0`) и только они:
  `GET /api/gap-score` и карточка считают min/max по тому же набору, поэтому
  число на карточке совпадает с числом у точки на матрице (паритетный тест
  `test_score_matches_matrix_point`);
- **`no_flag` не нормируется** (1.0 / 0.0 / None): это булев признак, а не ряд.

**Известное ограничение линейного min-max (уточнено в P5f-1).** Выброс в
adoption (fastmcp 12000/нед против ruff 1000/нед) прижимает остальных к нулю:
на выборках меньше ~50 пакетов один outlier делает метрику почти бинарной.
Опция `normalization: "minmax" | "rank"` в Settings — P6, если пользователь
попросит: ранговый перцентиль устойчивее к выбросам, но менее чувствителен к
абсолютным значениям. В P5e/P5f формула не меняется — только min-max.

**Почему усечение и что значит ноль (уточнено в P5f-1).** Минимум при
дефолтных весах — −0.8/5.6 ≈ −14 баллов, а шкала обещана 0..100, поэтому
результат усечён снизу: ноль означает «занято по всем штрафным компонентам»,
а не «данных нет» (последнее — `null`). Вырожденный диапазон (min == max)
даёт `norm = 0.5`, а не деление на ноль. «Первый непустой источник»
`search_interest` — источник с `error` пропускается: его `None` означает
«данных нет», а не «интерес не растёт» (gotcha 66).

**API (P5e):** в точках `GET /api/matrix` появились `gap_score` (float|null) и
`gap_breakdown` (семь компонентов + `weights_used` + `components_available`);
карточка `GET /api/packages/{name}` отдаёт те же два поля; новый
`GET /api/gap-score?names=a,b` (2–3 имени, как `/api/compare`) возвращает
`{scores: […], missing: […]}` с нормировкой по всем пакетам. Экспорт матрицы
(`POST /api/matrix/export`) в P5e колонок не менял; в P5f-1 добавлена
одиннадцатая колонка `gap_score` (§0.1 того шага): число в файле — то же, что
размер точки на экране (тот же `_attach_gap_scores`), пакет без score — пустая
строка. Первые девять колонок и десятая (`momentum_30d`) не сдвинулись.

**UI (P5e):** размер точки `r = 4 + gap/20` (4..9 px; без score — прежние 6),
в `<title>` точки «gap 87»; легенда под матрицей «● размер = gap score (0..100) ·
цвет = momentum · пунктир = порог adoption»; в карточке сразу под именем —
блок «Gap Score: 87 / 100» с breakdown («+ adoption 0.90 … − funding 0.20») и
счётчиком «(3 из 7)».

**Известное ограничение `signals.collect` (зафиксировано в P4d-2):**
`signals.collect` с ошибкой внешнего API → `done`, но данных нет (graceful
degradation на уровне задачи, §18.1). UI после «Задача создана» показывает
статус задачи (`done`), а не наличие данных; если источник вернул `error`,
карточка покажет «Ошибка: …» внутри блока сигнала. В P6 — различать
«собрано» и «собрано с ошибкой» в UI (после `done` автоматически перечитать
карточку через 400 мс, как в matrix). Это UX-полировка, не баг: задача не
падает из-за чужого API — менять семантику задачи не нужно.

### 18.4. Порядок реализации

| Этап | Что | Оценка |
|---|---|---|
| P4a | API Keys Store (файл, base64, UI, тесты) — ЗАКРЫТ 2026-10-20 | 1 день |
| P4b | Momentum (core + UI + matrix) — ЗАКРЫТ 2026-10-20 | 3 часа |
| P4c | Cross-ecosystem (mapping + UI) — ЗАКРЫТ 2026-10-20 | 1–2 дня |
| P5a | Jobs (Adzuna) — ЗАКРЫТ 2026-10-20 | 2 дня |
| P5b | Patents (PatentsView) — ЗАКРЫТ 2026-10-20 | 2–3 дня |
| P5c | Funding (SEC EDGAR) — ЗАКРЫТ 2026-10-20 | 3–5 дней |
| P5d | Search interest (Yandex Wordstat + Google Trends/Apify) — ЗАКРЫТ 2026-10-20 | 3–4 дня |
| P5e | Gap Score (Settings веса + UI) — ЗАКРЫТ 2026-10-20 | 1–2 дня |
| P5f-1 | Niche Finder, первая итерация (конвейер + эндпоинты + экран) — ЗАКРЫТ 2026-10-20 | 3–4 дня |
| P4d-1 | Упаковка (промежуточно): onedir, sync, demo, CI — ЗАКРЫТ 2026-10-21 | 1 день |
| P5f-2 | Niche Finder: momentum из Store, checked/found, фильтры, экспорт CSV/MD, «Собрать» — ЗАКРЫТ 2026-10-21 | 3–4 дня |
| P4d-2 | Пересборка `.exe` после P5f-2, закрытие P4 — ЗАКРЫТ 2026-10-21 | 1 час |
| P6 | MCP-сервер, расписание, уведомления — **ОТЛОЖЕН в долгосрочный план (2026-10-21)** | 2–3 дня |

### 18.5. Безопасность ключей

- Ключи хранятся в `<data_dir>/api_keys.json` с правами 0600 (POSIX) / icacls
  (Windows). Файл — в `.gitignore`.
- Не логировать значения ключей.
- В API — только статус («настроен»/«не настроен»), не значение.
- При экспорте БД ключи не попадают в экспорт (в SQLite их нет вообще).
- Честно документировать: «шифрование — base64, защита — права файла, не
  криптография».
- **Demo-режим: временный каталог, а не `:memory:` (P4d-1).** `app/web.py` был
  в списке «не трогать», а расширение lifespan — это правка `web.py`. Временный
  каталог (создаётся `desktop.prepare_demo`, удаляется при выходе) работает:
  данные не утекают, `%USERPROFILE%\GapAudit-data` не читается и не пишется,
  `settings.json`/`api_keys.json` не загружаются. Если когда-нибудь
  понадобится `:memory:` — это одна строка в lifespan:
  `Store(":memory:")` + `seed_demo_store(store)` при `GAPAUDIT_DEMO=1`.
  В демо-режиме `POST /api/niche/find` отвечает 503 (P5f-2): Niche Finder
  целиком живёт в CLI-подпроцессе и требует сети.
- **Валидация NaN/inf — во всех float-полях Settings (P5f-1, §0.5).**
  `json.loads` штатно разбирает `NaN`/`Infinity`, поэтому «битое значение в
  файле» — общая проблема, а не только у `gap_weight_*`: `math.isfinite` в
  `core/settings._coerce` отбрасывает нечисловые значения при загрузке, а
  `PUT /api/settings` отвечает 400 на NaN/inf в любом float-поле (тело
  приходится слать сырым: `httpx` отказывается сериализовать `NaN`).
- **`get` vs `public_meta` (уточнено в P5c).** `get(provider)` домешивает
  `public_meta` для несекретных полей (`edgar.user_agent`,
  `yandex_wordstat.client_id`, `apify.actor_id`) и возвращает полный набор
  объявленных полей, если они есть. `public_meta(provider)` — точный канал
  только для `meta`, без расшифровки `encrypted_value`. Для чтения
  User-Agent / `client_id` / `actor_id` оба работают; для чтения секретов
  (`token`, `api_key`, `api_token`) — только `get`. Пример: P5c использовал
  `public_meta("edgar")`, это правильно, но `get("edgar")` тоже вернул бы
  `user_agent` — предпосылка о пустом `get` была неверна (проверено на
  реальном `Store`).

### 18.6. Инструкции для получения ключей

См. `docs/API-KEYS.md` — пошаговые инструкции для Adzuna, PatentsView, SEC
EDGAR, Yandex Wordstat, Google Trends (официальный alpha и Apify). Ссылка из
UI «Настройки → API-ключи».

## 19. Поиск ниши (Niche Finder)

### 19.1. Зачем

Сейчас сценарии поиска ниш разрознены: «Сейчас решать» (верхний уровень),
«Каталоги» (незнакомая область), «Мониторинг» (динамика), «Сравнение» (отсев).
Пользователь прыгает между экранами. Niche Finder связывает их в один сквозной
workflow: «ввёл фразу → получил ранжированный список подсфер с рекомендацией».

### 19.2. Что делает

Один kind задачи — `niche.find` — выполняет конвейер:

1. `catalog.build` по фразе — находит все проекты, чьё описание содержит фразу
   (не только имена).
2. `packages.check` по найденным именам — adoption, verdict, flag.
3. `signals.collect` по именам — momentum, cross_eco, jobs, patents, funding,
   search_interest (что настроено).
4. Расчёт `gap_score` по собранным данным.
5. Ранжирование + фильтры — сортировка по gap_score, фильтры «только растущие»,
   «без MCP», «без funding».

Всё это — задача (подпроцесс), а не работа в обработчике запроса: инвариант 6
(«никакой сетевой работы в обработчиках»). `POST /api/niche/find` отвечает
`202 {"job_id"}`, результат читается `GET /api/niche/result/{job_id}`, прогресс —
SSE.

### 19.3. UI

```
┌─────────────────────────────────────────────────────────────┐
│ Поиск ниши                                                   │
│                                                              │
│ [vector database________________]  [Найти нишу]              │
│                                                              │
│ Фильтры:  ☑ только растущие (momentum > 0)                   │
│           ☑ без MCP                                          │
│           ☑ без funding (никто не вложился)                  │
│           ☐ только патенты растут                            │
│                                                              │
│ ─────────────────────────────────────────────────────────── │
│                                                              │
│ Найдено 12 подсфер в «vector database»:                      │
│                                                              │
│  1. pinecone-client          gap 87  ↑ +42%/30д  MCP нет     │
│     └ adoption 2.1M/нед, funding: нет, patents: +5%          │
│                                                              │
│  2. weaviate-client          gap 74  ↑ +28%/30д  MCP нет     │
│     └ adoption 890k/нед, funding: 1 раунд Q3, patents: +12%  │
│                                                              │
│  3. chromadb                 gap 52  →  0%/30д   MCP есть    │
│     └ adoption 4.2M/нед, funding: 2 раунда — ЗАНЯТО          │
│                                                              │
│  [Экспорт CSV]  [Добавить всех в матрицу]                    │
└─────────────────────────────────────────────────────────────┘
```

### 19.4. Эндпоинты

Реализовано в P5f-1 (форма тела отличается от первоначального наброска ниже —
см. §19.7):

- `POST /api/niche/find` — тело `{"query": "vector database", "min_stars": 20,
  "pages": 2, "sources": ["github","npm","registry"],
  "signal_kinds": ["jobs","patents","funding","search_interest"], "limit": 50}`;
  ответ `202 {"job_id", "status": "queued"}`. Валидация: `query` — непустая
  строка, `min_stars >= 0`, `pages` 1..10, `limit` 1..500, значения `sources`
  и `signal_kinds` — по наборам `NICHE_SOURCES` / `NICHE_SIGNAL_KINDS`
  (`app/web.py`); всё остальное — 400 до постановки задачи.
- `GET /api/niche/result/{job_id}` — `result.json` задачи (`{query, items,
  stats}`, items отсортированы по `gap_score` DESC). Задача идёт — `202
  {status, progress, message, error}`; задачи нет — 404; задача `done` без
  файла — 500 (баг задачи, а не «ничего не нашли»).
- `POST /api/niche/export?job_id=<id>&format=csv|md` (P5f-2) — файл результата
  задачи: CSV (`utf-8-sig`, колонки `name, gap_score, momentum_30d, flag,
  adoption_week, verdict, registry_name`) или Markdown (шапка «найдено /
  проверено» + таблица тех же колонок). `Content-Disposition: attachment`,
  `Cache-Control: no-store`. Коды: 400 — формат не `csv`/`md`; 404 — задачи нет
  или её `result.json` отсутствует/не читается (это ответ на запрос
  пользователя, а не сбой сервера). Фильтры экрана в файл не переносятся:
  экспорт = «сохранить всё, что нашлось».
- Фильтры результата (momentum > 0, без MCP, gap ≥ X, P5f-2) — **клиентские**:
  `niche.js` перерисовывает уже полученный `result.json`, новых эндпоинтов и
  запросов нет. Поэтому набросок с `filters` в теле `POST /api/niche/find`
  (и переименованием `query` → `q`) так и не реализован: одна правда вместо
  трёх имён одного значения.

### 19.5. Стоимость

3–4 дня (P5, после signals). Это не новый источник данных — это UI поверх уже
собранного.

### 19.6. Без ключей

Работает частично: momentum + cross_eco + flag + adoption. Jobs/patents/
funding/search_interest показывают «не настроен», но gap_score считается (с
меньшим числом компонент, честно помеченным — «учтено 3 из 7 компонент»).

### 19.7. Реализовано в P5f-1/P5f-2 и осознанные упрощения

**Что сделано (первая итерация):** `cli/niche.py` (kind `niche.find`),
таблицы `app/jobs.py`, `POST /api/niche/find`, `GET /api/niche/result/{job_id}`,
экран `/niche` (`web/views/niche.html`, `web/static/niche.js`), пункт навигации
«Поиск ниши», 11-я колонка `gap_score` в `POST /api/matrix/export`, 4 шага
прогресса (`catalog.build` → `packages.check` → `signals.collect <вид>` →
`gap_score` → `done`). Кнопка «Собрать» в карточке, фильтры экрана и экспорт
CSV/MD из Niche Finder были отложены в P5f-1 и **сделаны в P5f-2** (см. ниже).

**Упрощения и ограничения (каждое — решение, а не недоделка):**

1. ~~**momentum в CLI = `None`.**~~ **Снято в P5f-2:** CLI читает историю
   наблюдений из `<data_dir>/gapaudit.db` (`Store` только на чтение,
   `_momentum_from_store`) и считает ту же `compute_momentum`, что матрица.
   Базы нет / она битая — `None` и строка в stderr (graceful degradation),
   база не создаётся чтением. Momentum пакета в Niche Finder совпадает с
   `/api/matrix`. gap_score совпадает только при **двух** условиях сразу:
   тот же набор пакетов (нормировка min-max по набору, gotcha 67(a)) и те же
   входные числа — матрица берёт последнее *сохранённое* наблюдение, а Niche
   Finder делает свежую проверку реестров, поэтому adoption у одного и того же
   имени может отличаться (живой прогон P5f-2: momentum 50.0 = 50.0, adoption
   600 000 из истории против 60 174 375 из PyPI). Совпадение при равных входах
   держит тест `test_niche_momentum_and_score_match_matrix`.
2. **cross_ecosystem не собирается.** Его нет в формуле §18.3, а без `Store`
   считать его не из чего; сигнал остаётся экранным (карточка, матрица).
3. **Проверяются первые 50 строк каталога по звёздам** (`CHECK_LIMIT`).
   Живой прогон по фразе «test impact analysis»: 130 найденных строк, проверка
   всех упирается в лимиты PyPI/npm/pypistats — adoption не приходит ни у одной
   строки, все score равны 100 и ранжирования нет. `stats.found` считает весь
   каталог (`len(items)`), `stats.checked` — сколько реально проверено. В P5f-2
   экран показывает это прямо: «Проверено 50 из 139 по звёздам. Увеличьте лимит
   или проверяйте по требованию». Батчи с паузами — P6 (отложен).
4. **Имена для реестров — имя репозитория, не `owner/repo`.** У GitHub-строки
   id — полный путь, и запрос `pypi/<owner>/<repo>` всегда 404 (adoption
   терялся целиком). `cli.niche._registry_name` берёт последний сегмент;
   scoped-имена npm (`@scope/pkg`) не трогает. Отображение в результате —
   по-прежнему id каталога.
5. **Сортировка при равном score:** измеренная ниша выше неизмеренной
   (`adoption_week is None`), затем имя. Без этого верх списка занимали строки
   со score 100 по единственной компоненте «MCP нет» и реальные данные уезжали
   вниз.
6. **`niche.find` не пишет в observations** (не входит в `_POST_PROCESS_KINDS`):
   он читает существующие сигналы и складывает результат в `result.json`
   задачи. Запись наблюдений — дело `packages.check`/`signals.collect`.

**Что добавлено в P5f-2 (закрыт 2026-10-21):**

- **momentum из `Store`** (`cli/niche.py::_momentum_from_store`): история
  наблюдений читается по имени реестра, база только читается и не создаётся;
  нет базы/битая — `None` и строка в stderr, задача завершается с кодом 0.
  `items` теперь несут `registry_name` (под каким именем искали в реестрах).
- **`stats.found` = `len(catalog["items"])`**, `stats.checked` — сколько
  проверено (`CHECK_LIMIT`). Экран показывает «Проверено N из M…».
- **Фильтры результата**: «только с momentum > 0», «без MCP», «gap ≥ X» —
  клиентские, перерисовка из `result.json` без новых запросов.
- **Экспорт**: `POST /api/niche/export` (CSV `utf-8-sig` / Markdown) и две
  кнопки на экране; файл скачивается браузером (`Blob` + `<a download>`).
- **Кнопка «Собрать»** в карточке пакета для сигналов в состоянии
  `not_collected` (jobs, patents, funding, search_interest): ставит
  `signals.collect` по одному виду и показывает «Задача создана»/ошибку.
  Для `not_configured` кнопки нет — там нужен ключ, а не задача.
- **503 в demo-режиме** для `POST /api/niche/find`: Niche Finder — CLI-подпроцесс
  с сетью, демо работает на фикстурах. Экран показывает причину и не поллит.
- **CI job `build`** (windows-latest): `uv pip install --system -e ".[dev,packaging]"`,
  `pyinstaller gapaudit.spec`, затем живой smoke: `.exe --browser --port 8899`
  → `/api/health` 200 → `Stop-Process`.
- Тесты: +5 cli, +7 web (экспорт ×4, demo 503, совпадение с матрицей,
  «Собрать» ×2), +3 шаблоны; всего **1206** собранных (1204 passed, 2 skipped;
  было 1191 в P4d-1 — дельта +15). Цифра сверена в P4d-2: ранняя запись
  «1208 (1189 + 19)» считала только passed и завышала дельту.
