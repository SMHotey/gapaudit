# GapAudit

Локальное приложение для аудита ниш экосистем (MCP и не только) поверх скилла
`ecosystem-gap-audit`: очередь длинных задач с прогрессом, накопленная история
замеров, сравнение ниш, каталоги, мониторинг изменений.

**Текущее состояние — P4 закрыт (шаг P4d), P5 закрыт по сигналам.** Работают все
экраны (Задачи, Сейчас решать, Пакеты, Мониторинг, Каталоги, Сравнение, Поиск
ниши, Настройки), очередь задач с прогрессом через SSE, история замеров в
SQLite, вердикты, каталоги, мониторинг, экспорт (CSV/MD/JSON), шесть сигналов
ниш (momentum, cross-ecosystem, jobs, patents, funding, поисковый интерес),
Gap Score и Niche Finder. P4d добавил упаковку в `.exe` (PyInstaller onedir),
синхронизацию со скиллом, demo-режим и CI. `DEVPLAN.md` — источник истины по
объёму работ.

## Возможности

Базовое (работает сейчас):

- Матрица «Сейчас решать»: adoption × признак (MCP), 4 квадранта, карточка пакета.
- Вердикты ЖИВО / МЁРТВО / ЗАСТОЙ по adoption, датам релиза и свежести данных.
- Очередь задач с прогрессом (SSE), отменой, продолжением после перезапуска, логами.
- История замеров в SQLite: динамика adoption, события (релиз, скачок, «появился MCP»).
- Каталоги по фразам (GitHub, npm, реестр MCP) с фильтрами, пагинацией и экспортом CSV/MD.
- Сравнение 2–3 пакетов бок о бок, экспорт CSV.
- Мониторинг: таймлайн событий с фильтрами, выгрузка CSV/JSON.
- Настройки: пороги, ключевые слова, TTL кэша, лимиты.
- CLI остаётся автономным: те же команды, те же коды выхода, работа без БД и UI.

Сигналы ниш (P4/P5, работают; внешние источники — при наличии ключа):

- Momentum: Δ adoption за 7/30/90 дней — растёт или падает.
- Cross-ecosystem: сравнение PyPI/npm/GitHub для одного концепта.
- Jobs: спрос на труд по странам (Adzuna, нужен ключ).
- Patents: патентный импульс за 3 года (PatentsView, нужен ключ).
- Funding: раунды финансирования (SEC EDGAR, нужен User-Agent).
- Search interest: динамика и регионы поискового интереса
  (Yandex Wordstat — бесплатно по заявке; Google Trends — официальный
  alpha или платные Apify/DataForSEO).
- Gap Score: интегральная метрика 0–100.
- Поиск ниши (Niche Finder): фраза → ранжированный список подсфер.

**Пользовательские API-ключи:** настройка в приложении (Настройки →
API-ключи). Инструкции — [docs/API-KEYS.md](docs/API-KEYS.md). Без ключей
работает всё, кроме сигналов.

## Установка на чистой машине

Вариант A (рекомендуемый, через `uv`):

```
git clone <адрес-репозитория> gapaudit
cd gapaudit
uv venv --python 3.11
uv pip install -e ".[dev]"
uv run python desktop.py
```

Вариант B (без `uv`, только stdlib + pip):

```
git clone <адрес-репозитория> gapaudit
cd gapaudit
python -m venv .venv            # обязательно Python 3.11.x
.venv\Scripts\activate           # Windows;  в bash: source .venv/Scripts/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
python desktop.py
```

Запуск одной командой после установки: `run.bat` (Windows) или `./run.sh`.

### Проверка установки

```
.venv\Scripts\python.exe -c "import webview, fastapi, uvicorn, jinja2, httpx; print('imports ok')"
.venv\Scripts\python.exe -c "import webview, importlib.metadata as m; print('pywebview', m.version('pywebview'))"
```

Обратите внимание: пакет ставится как `pywebview`, а **импортируется как
`webview`** — модуля с именем `pywebview` не существует, поэтому команда
`python -c "import pywebview"` из DEVPLAN.md §12 выдаёт `ModuleNotFoundError`.
Это особенность самого пакета, а не ошибка окружения.

### Установка из .exe (Windows)

1. Соберите пакет (один раз ставится PyInstaller):

   ```
   uv pip install -e ".[packaging]"
   uv run pyinstaller gapaudit.spec
   ```

   Результат — `dist\gapaudit\` (`gapaudit.exe` + папка `_internal\`).

2. Скопируйте **всю папку** `dist\gapaudit\` на целевую машину и запустите
   `gapaudit.exe`. Папку `_internal\` не переставляйте и не чистите: в ней
   DLL, шаблоны экранов и статика.

3. Данные приложения — в `%USERPROFILE%\GapAudit-data\` (база, логи, каталоги).
   Ключи внешних API — в `<каталог данных>\api_keys.json`.

Требования на целевой машине: Windows 10/11 и WebView2 Runtime (в системе он
обычно уже есть; если окно белое — поставьте
[Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)).
Python и `uv` для запуска `.exe` не нужны.

Проверка собранного пакета: `gapaudit.exe --browser --port 8790` — сервер
отвечает на `http://127.0.0.1:8790/api/health`, окно не открывается.

### Demo-режим

Любые ответы — из `tests/fixtures/`, сеть выключена, данные приложения живут
во временном каталоге и удаляются при выходе:

```
uv run python desktop.py --demo          # окно с демо-данными
uv run python desktop.py --demo --browser --port 8790
GAPAUDIT_DEMO=1 uv run python -m cli.check_predecessors requests
```

Демо-данные: 18 наблюдений трёх замеров (momentum), 8 событий, 2 каталога.
`requests` — снимки настоящих ответов PyPI/npm/pypistats, остальные пакеты
демо-набора — синтетические числа (в таблице `tools/demo_mode.py`), подобранные
так, чтобы на матрице были видны рост, спад и стабильность.
Каталог данных пользователя (`%USERPROFILE%\GapAudit-data`) демо-режим не
читает и не трогает; `settings.json` и `api_keys.json` не загружаются.

### CI

GitHub Actions на push/PR в `master` (`.github/workflows/ci.yml`): установка
зависимостей через `uv`, `mypy core/ app/ cli/`, быстрый прогон
`pytest -q -m "not slow"`, полный `pytest -q` и `python -m tools.compat_check`
(паритет CLI с `legacy/`). Раннер — `ubuntu-latest`: окно WebView2 в CI не
открывается, тесты ходят в HTTP через `TestClient`, Windows-only проверки
помечены `skipif`.

### API-ключи (необязательно)

Базовые экраны работают без ключей. Чтобы включить сигналы ниш (вакансии,
патенты, финансирование, поисковый интерес), откройте **Настройки →
API-ключи**, вставьте ключ нужного провайдера и нажмите «Проверить».
Инструкции по получению ключей — [docs/API-KEYS.md](docs/API-KEYS.md).

Ключи хранятся в `<data_dir>/api_keys.json` (права 0600) и не попадают в базу
данных, поэтому при копировании БД они не утекают. Не публикуйте этот файл.

## Требования

- **Python 3.11.x** (в `pyproject.toml` границы `>=3.11,<3.13`). На 3.13 проект
  не запускаем: `pythonnet` может не работать.
- **WebView2 Runtime** — на Windows 10/11 обычно уже установлен. Если окно не
  открывается или белое, поставьте
  [Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/).
- **Авторизованный `gh` CLI** (шаг 1e и дальше): без него работает только
  `legacy/check_predecessors.py`, лимиты GitHub Search падают до 60 запросов/час.
  Проверка: `gh auth status`.
- Сеть: публичные API (GitHub, npm, реестр MCP, PyPI) — всегда. Опционально:
  Adzuna, PatentsView, SEC EDGAR, Yandex Wordstat, Google Trends — только если
  пользователь настроил ключи в приложении (Настройки → API-ключи, см.
  [docs/API-KEYS.md](docs/API-KEYS.md)). Без ключей работают все базовые
  функции и сигналы momentum / cross-ecosystem. Никаких CDN, Node.js и Docker
  в этом проекте нет.

## Режимы запуска

```
uv run python desktop.py               # обычное окно 1280x860
uv run python desktop.py --dev         # devtools WebView2 и подробные логи uvicorn
uv run python desktop.py --browser     # без окна, открыть системный браузер
uv run python desktop.py --port 8790   # фиксированный порт вместо свободного
uv run python desktop.py --demo        # демо-данные из фикстур, сеть выключена
```

Порт выбирает ОС (`socket.bind(("127.0.0.1", 0))`), к URL добавляется сессионный
токен: `http://127.0.0.1:<порт>/?token=...`. Все `/api/*` требуют этот токен
(заголовок `X-GapAudit-Token`, cookie `gapaudit_token` или query-параметр `token`),
а `Host`/`Origin` обязаны указывать на `127.0.0.1:<порт>` или `localhost:<порт>` —
иначе `403`. Это защита от DNS-rebinding.

## Если что-то не так

- `ModuleNotFoundError: No module named 'pywebview'` — пакет импортируется как
  `webview` (см. выше), это ожидаемо.
- `port ... in use` — занятый порт печатает ошибку и выходит с кодом 1 через
  10 секунд ожидания `/api/health`; повторите запуск без `--port` или возьмите
  другой порт.
- Вывод `desktop.py` при перенаправлении в файл буферизуется: для живого лога
  запускайте с `PYTHONUNBUFFERED=1` (`PYTHONUNBUFFERED=1 uv run python desktop.py > app.log`)
  или из терминала.
- Окно белое — проверьте WebView2 Runtime (ссылка в «Требованиях»).
- В `pytest` при импорте `TestClient` появляется предупреждение Starlette 1.9
  (`install httpx2 instead`) и `DeprecationWarning` про per-request cookies — это
  косметика: тесты проходят, `httpx` закреплён `<0.29`, миграция на `httpx2` —
  отдельный шаг, когда пакет станет стабильным (DEVPLAN §14).
- `data/screenshots/` больше не существует: скриншоты проверок шагов лежат в
  `docs/screenshots/` и коммитятся, а рабочая папка `data/` в `.gitignore` целиком.

## Карта проекта

```
core/                 логика метода: чистые функции (http, cache, registry,
                      github, packages, catalog, mcp_registry, scan, diffing,
                      settings); с P4 — core/collectors/ (сигналы ниш),
                      core/signals.py, core/api_keys.py
cli/                  те же шесть скриптов, что и в скилле (шаги 1a–1e)
app/                  приложение: store.py (SQLite), jobs.py (очередь),
                      web.py (FastAPI, /api/*, SSE)
web/views/            Jinja2-шаблоны экранов
web/static/           локальная статика: CSS, JS, без CDN
tools/                служебные скрипты: compat_check.py (паритет с legacy),
                      sync_skill.py (копия core/cli в скилл), demo_mode.py
                      (фикстуры и демо-данные)
tests/                тесты; tests/fixtures/http/ — реальные ответы API для тестов
docs/API-KEYS.md      как получить ключи внешних API
docs/screenshots/     скриншоты проверок шагов (коммитятся)
data/                 рабочая папка dev-режима (в .gitignore)
legacy/               6 исходных скриптов скилла, копия байт-в-байт (эталон)
desktop.py            точка входа: uvicorn в демон-потоке + окно pywebview
gapaudit.spec         сборка PyInstaller onedir (dist/gapaudit/)
.github/workflows/    CI: pytest + mypy + compat_check
```

## Статус

| Этап | Состояние |
|---|---|
| P0–P3 | закрыты (каркас, `core`, SQLite, очередь + SSE, экраны, экспорт) |
| P4 | закрыт: P4a API Keys Store, P4b momentum, P4c cross-ecosystem, **P4d — упаковка, sync со скиллом, demo-режим, CI, README** |
| P5 | закрыт: P5a jobs, P5b patents, P5c funding, P5d search_interest, P5e Gap Score, P5f-1 Niche Finder |
| P5f-2 | план: фильтры/экспорт Niche Finder, кнопка «Собрать» в карточке, momentum в CLI Niche Finder |
| P6 | **отложен в долгосрочный план (2026-10-21)**: MCP-сервер, расписание, уведомления, батчи с паузами. Причина — сначала прожить месяц с упакованным приложением |

`DEVPLAN.md` — источник истины по объёму работ (§4 статусы, §11 бюджеты тестов,
§12 упаковка, §13 этапы, §18 сигналы, §19 Niche Finder).
