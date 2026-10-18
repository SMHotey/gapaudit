# GapAudit

Локальное приложение для аудита ниш экосистем (MCP и не только) поверх скилла
`ecosystem-gap-audit`: очередь длинных задач с прогрессом, накопленная история
замеров, сравнение ниш, каталоги, мониторинг изменений.

**Текущее состояние — шаг 1a: транспортный слой.** Есть каркас окна (шаг 0.1) и
`core/http.py` + `core/cache.py` — HTTP-клиент с кэшем, TTL, троттлингом по хосту
и повторами. Логики метода (registry/github/packages) и базы данных пока нет:
это следующие шаги.

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

## Требования

- **Python 3.11.x** (в `pyproject.toml` границы `>=3.11,<3.13`). На 3.13 проект
  не запускаем: `pythonnet` может не работать.
- **WebView2 Runtime** — на Windows 10/11 обычно уже установлен. Если окно не
  открывается или белое, поставьте
  [Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/).
- **Авторизованный `gh` CLI** (шаг 1e и дальше): без него работает только
  `legacy/check_predecessors.py`, лимиты GitHub Search падают до 60 запросов/час.
  Проверка: `gh auth status`.
- Сеть — только к публичным API (GitHub, npm, реестр MCP, PyPI). Никаких CDN,
  Node.js и Docker в этом проекте нет.

## Режимы запуска

```
uv run python desktop.py               # обычное окно 1280x860
uv run python desktop.py --dev         # devtools WebView2 и подробные логи uvicorn
uv run python desktop.py --browser     # без окна, открыть системный браузер
uv run python desktop.py --port 8790   # фиксированный порт вместо свободного
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
core/                 логика метода: чистые функции (шаг 1a — http.py, cache.py)
cli/                  те же шесть скриптов, что и в скилле (шаги 1a–1e)
app/web.py            FastAPI: /api/health, главная страница, миддлварь токена
web/views/            Jinja2-шаблоны (index.html — экран «каркас работает»)
web/static/           локальная статика: CSS, JS, без CDN
tools/                служебные скрипты проекта (dump_code.py и др.)
tests/                тесты; tests/fixtures/http/ — реальные ответы API для тестов
docs/screenshots/     скриншоты проверок шагов (коммитятся)
data/                 рабочая папка dev-режима (в .gitignore)
legacy/               6 исходных скриптов скилла, копия байт-в-байт (эталон для шага 1e)
desktop.py            точка входа: uvicorn в демон-потоке + окно pywebview
```

## Дальше по плану

Шаг 1 — рефакторинг `legacy/*` в `core/` + CLI-обёртки (шаги 1a–1e), затем
SQLite-схема (шаг 2). `DEVPLAN.md` — источник истины по объёму работ.
