"""Каркас экранов (шаг 2a): base.html, статика и экран «Задачи».

Проверяется только серверная сторона: Jinja2 отдаёт разметку, ``/static``
отдаёт файлы. Содержимое таблицы рисует ``jobs.js`` в браузере, поэтому
здесь проверяется наличие контейнеров и подсказок, а не строк задач
(браузерных тестов в проекте нет — UI проверяется живым запуском).

Сеть — ноль, подпроцессы — ноль: клиент из conftest (фейковый runner,
каталог данных в ``tmp_path``, воркеры не поднимаются).
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_index_renders_base_layout(client: TestClient) -> None:
    """GET / — каркас: вся навигация из семи живых пунктов, «Задачи» активны."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<nav" in response.text
    assert "Задачи" in response.text
    # «Сейчас решать» — рабочий пункт с шага 2b (раньше был disabled P2b)
    assert '<a href="/matrix">Сейчас решать</a>' in response.text
    # 3a: «Пакеты» и «Мониторинг» стали живыми ссылками
    assert '<a href="/packages">Пакеты</a>' in response.text
    assert '<a href="/monitor">Мониторинг</a>' in response.text
    # 3b: «Каталоги» тоже стала живой ссылкой
    assert '<a href="/catalogs">Каталоги</a>' in response.text
    # 3c: «Сравнение» — последний выключенный пункт P3 стал ссылкой
    assert '<a href="/compare">Сравнение</a>' in response.text
    assert "disabled" not in response.text


def test_index_nav_marks_current_page(client: TestClient) -> None:
    """``active`` — только на текущем экране (base.html смотрит на request.url.path)."""
    response = client.get("/")

    assert '<a href="/" class="active">Задачи</a>' in response.text
    assert response.text.count('class="active"') == 1


def test_stylesheet_is_served(client: TestClient) -> None:
    """Тёмная тема отдаётся локально (никаких CDN) и содержит переменные."""
    response = client.get("/static/style.css")

    assert response.status_code == 200
    assert "--bg:" in response.text
    assert "--bg-elev:" in response.text


def test_app_js_is_served(client: TestClient) -> None:
    """Общий клиент API: объект GapAudit и токен из query (DESIGN §7, вариант A)."""
    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "GapAudit" in response.text
    assert "X-GapAudit-Token" in response.text
    assert "location.search" in response.text


def test_jobs_js_is_served(client: TestClient) -> None:
    """Экранный скрипт ходит в /api/jobs и умеет отмену/продолжение."""
    response = client.get("/static/jobs.js")

    assert response.status_code == 200
    assert "/api/jobs" in response.text
    assert "/cancel" in response.text
    assert "/resume" in response.text


def test_index_has_jobs_containers_and_empty_state(client: TestClient) -> None:
    """Контейнеры таблицы и подсказка пустого списка — в шаблоне, не в тексте."""
    response = client.get("/")

    assert 'id="jobs-body"' in response.text
    assert '<div class="empty">' in response.text
    assert 'id="counter"' in response.text
    assert 'id="refresh"' in response.text
    # подвал и шапка: индикаторы health и каталог данных
    assert 'id="health-db"' in response.text
    assert 'id="health-gh"' in response.text
    assert 'id="data-dir"' in response.text

    # пустое состояние и подсказка «как поставить задачу» живут в jobs.js
    script = client.get("/static/jobs.js").text
    assert "Нет задач" in script
    assert "POST /api/jobs" in script


# --- экран «Сейчас решать» (шаг 2b) -----------------------------------------


def test_matrix_screen_renders_containers(client: TestClient) -> None:
    """GET /matrix: контейнеры матрицы, таблицы кандидатов и карточки на месте."""
    response = client.get("/matrix")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Сейчас решать" in response.text
    # навигация: матрица активна, «Задачи» — нет
    assert '<a href="/matrix" class="active">Сейчас решать</a>' in response.text
    assert response.text.count('class="active"') == 1

    assert 'id="matrix-svg"' in response.text
    assert 'id="card-overlay"' in response.text
    assert 'id="candidates-body"' in response.text
    assert 'id="matrix-empty"' in response.text
    assert 'id="count-high-no-flag"' in response.text
    # скрипт экрана подключён, sse.js не дублируется (он в base.html)
    assert "/static/matrix.js" in response.text
    assert response.text.count("/static/sse.js") == 1


def test_matrix_js_is_served(client: TestClient) -> None:
    """Экранный скрипт ходит в /api/matrix, карточку отдаёт card.js, умеет SSE."""
    response = client.get("/static/matrix.js")

    assert response.status_code == 200
    assert "/api/matrix" in response.text
    # карточка с шага 3a живёт в общем модуле: матрица только зовёт его
    assert "GapAuditCard.open" in response.text
    assert "GapAuditSSE.subscribe" in response.text
    # логарифмическая ось и порог из ответа, а не зашитые в скрипт числа
    assert "Math.log10" in response.text
    assert "thresholds.alive_weekly" in response.text


def test_sse_js_is_served(client: TestClient) -> None:
    """Общий модуль SSE отдаётся локально и ходит в /api/stream с токеном."""
    response = client.get("/static/sse.js")

    assert response.status_code == 200
    assert "GapAuditSSE" in response.text
    assert "/api/stream?token=" in response.text


def test_app_js_fills_footer_version(client: TestClient) -> None:
    """Подвал показывает версию из /api/health, а не статичный «GapAudit» (§0.1)."""
    script = client.get("/static/app.js").text

    assert "setText('version', 'GapAudit v' + h.version)" in script


# --- экран «Настройки» и F5 (шаг 2c) ----------------------------------------


def test_settings_screen_renders_form(client: TestClient) -> None:
    """GET /settings: форма порогов, контейнеры readonly-полей и скрипт экрана.

    Поля ввода перечислены по именам: ``settings.js`` ищет их через
    ``form.elements.<name>``, и переименование в шаблоне дало бы пустую форму
    без единой ошибки в консоли.
    """
    response = client.get("/settings")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<a href="/settings" class="active">Настройки</a>' in response.text
    assert response.text.count('class="active"') == 1

    for name in (
        "alive_weekly", "alive_monthly", "dead_weekly", "dead_monthly",
        "stale_days", "adoption_delta_pct", "cache_ttl_hours",
        "max_parallel_jobs", "registry_limit", "keywords",
    ):
        assert f'name="{name}"' in response.text, name
    for element_id in ("settings-form", "settings-msg", "ro-data-dir",
                       "ro-db-path", "ro-version", "reset"):
        assert f'id="{element_id}"' in response.text, element_id
    assert "/static/settings.js" in response.text


def test_f5_refresh_hook_is_wired_on_every_screen(client: TestClient) -> None:
    """F5 перехвачен в app.js, и каждый экранный скрипт отдаёт свой refresh.

    Без второй половины F5 просто перезагрузил бы страницу: экран не успевает
    объявить ``window.__gapauditRefresh`` до обработчика клавиши, и «Обновить»
    теряет выбранный фильтр и открытую карточку.
    """
    app_js = client.get("/static/app.js").text
    assert "e.key !== 'F5'" in app_js
    assert "preventDefault" in app_js
    assert "window.__gapauditRefresh" in app_js
    assert "location.reload()" in app_js

    for script_path in ("/static/jobs.js", "/static/matrix.js",
                        "/static/settings.js", "/static/packages.js",
                        "/static/monitor.js", "/static/catalogs.js",
                        "/static/catalog_detail.js", "/static/compare.js"):
        script = client.get(script_path).text
        assert "window.__gapauditRefresh = load" in script, script_path


# --- экраны «Пакеты» и «Мониторинг» (шаг 3a) --------------------------------


def test_packages_screen_renders_containers(client: TestClient) -> None:
    """GET /packages: поиск, тело таблицы, пагинация, скрипт экрана."""
    response = client.get("/packages")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<a href="/packages" class="active">Пакеты</a>' in response.text
    assert response.text.count('class="active"') == 1
    for element_id in ("packages-q", "packages-body", "packages-pagination",
                       "packages-counter", "refresh"):
        assert f'id="{element_id}"' in response.text, element_id
    assert "/static/packages.js" in response.text
    # card.js общий и подключён в base.html — второй раз его быть не должно
    assert response.text.count("/static/card.js") == 1


def test_monitor_screen_renders_containers(client: TestClient) -> None:
    """GET /monitor: чипсы видов событий, таймлайн, пагинация, скрипт экрана."""
    response = client.get("/monitor")

    assert response.status_code == 200
    assert '<a href="/monitor" class="active">Мониторинг</a>' in response.text
    assert response.text.count('class="active"') == 1  # только пункт навигации
    for element_id in ("kind-filter", "monitor-timeline", "monitor-pagination",
                       "monitor-name", "monitor-counter"):
        assert f'id="{element_id}"' in response.text, element_id
    for kind in ("release", "flag_appeared", "flag_removed", "adoption_change",
                 "verdict_change", "new", "missing"):
        assert f'data-kind="{kind}"' in response.text, kind
    assert "/static/monitor.js" in response.text


def test_card_js_is_served_and_exports_module(client: TestClient) -> None:
    """Общий модуль карточки: оверлей берётся из base.html, API — три метода."""
    response = client.get("/static/card.js")

    assert response.status_code == 200
    assert "GapAuditCard" in response.text
    assert "card-overlay" in response.text
    assert "/api/packages/" in response.text
    # данные — только через textContent (gotcha 37): innerHTML для них нет
    assert "innerHTML" in response.text  # очистка контейнера, не данные
    assert "createTextNode" in response.text or "textContent" in response.text


def test_packages_and_monitor_js_are_served(client: TestClient) -> None:
    """Экранные скрипты ходят в свои эндпоинты и открывают общую карточку."""
    packages = client.get("/static/packages.js")
    monitor = client.get("/static/monitor.js")

    assert packages.status_code == 200
    assert "/api/packages/search" in packages.text
    assert "GapAuditCard.open" in packages.text
    assert monitor.status_code == 200
    assert "/api/events" in monitor.text
    assert "GapAuditCard.open" in monitor.text


def test_token_survives_navigation_between_screens(client: TestClient) -> None:
    """Токен из ``?token=`` запоминается и переживает клик по навигации.

    Пункты навигации — обычные ``<a href="/matrix">`` без query: после клика
    ``location.search`` пуст, и без запоминания каждый экран получал 403, а окно
    показывало пустые матрицу и форму настроек (обнаружено живым прогоном 2c).
    """
    script = client.get("/static/app.js").text

    assert "sessionStorage.setItem" in script
    assert "sessionStorage.getItem" in script
    assert "resolveToken" in script
    # токен из query приоритетнее: новый запуск окна приносит новый токен
    assert script.index("location.search") < script.index("sessionStorage.getItem")


# --- экраны «Каталоги» (шаг 3b) ---------------------------------------------


def test_catalogs_screen_renders_list_and_form(client: TestClient) -> None:
    """GET /catalogs: форма сборки, таблица списка, скрипт экрана."""
    response = client.get("/catalogs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<a href="/catalogs" class="active">Каталоги</a>' in response.text
    assert response.text.count('class="active"') == 1
    for element_id in ("new-catalog", "new-catalog-form", "catalog-form",
                       "catalogs-body", "catalog-msg", "refresh"):
        assert f'id="{element_id}"' in response.text, element_id
    # поля формы ищутся по name из catalogs.js — переименование дало бы
    # форму, которая молча отправляет пустое тело
    for name in ("phrases", "min_stars", "pages"):
        assert f'name="{name}"' in response.text, name
    assert "/static/catalogs.js" in response.text
    # форма скрыта до «Новый каталог» — иначе она занимает весь экран списка
    assert 'id="new-catalog-form" class="new-catalog-form" hidden' in response.text


def test_catalog_detail_screen_renders_filters(client: TestClient) -> None:
    """GET /catalogs/{id}: фильтры, таблица элементов, пагинация, экспорт."""
    response = client.get("/catalogs/abc123")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # пункт навигации жив и на детальном экране — id каталога не должен
    # превращать шапку в «ни один экран не активен»
    assert '<a href="/catalogs" class="active">Каталоги</a>' in response.text
    assert response.text.count('class="active"') == 1  # чипс — class="chip active"
    for element_id in ("catalog-title", "alive-filter", "filter-stars",
                       "filter-q", "items-body", "items-pagination",
                       "items-counter", "export-csv", "export-md"):
        assert f'id="{element_id}"' in response.text, element_id
    for alive in ("", "true", "false"):
        assert f'data-alive="{alive}"' in response.text, alive
    assert 'href="/catalogs"' in response.text  # «← К списку»
    assert "/static/catalog_detail.js" in response.text
    assert response.text.count("/static/card.js") == 1


def test_catalogs_js_uses_dom_not_innerhtml_for_data(client: TestClient) -> None:
    """Скрипты каталогов собирают строки DOM-методами (gotcha 37)."""
    for path in ("/static/catalogs.js", "/static/catalog_detail.js"):
        script = client.get(path).text
        assert "createElement" in script, path
        assert "textContent" in script, path
        # innerHTML встречается только как очистка контейнера
        assert "innerHTML = ''" in script, path
        assert "innerHTML =" in script, path

    detail = client.get("/static/catalog_detail.js").text
    assert "/api/catalogs/" in detail
    assert "/export?format=" in detail
    assert "X-GapAudit-Token" in detail
    assert "URL.createObjectURL" in detail
    # каталога может ещё не быть: экран смотрит статус задачи с тем же id
    assert "/api/jobs/" in detail


def test_monitor_screen_has_export_buttons(client: TestClient) -> None:
    """«Мониторинг» выгружает таймлайн: кнопки CSV/JSON + эндпоинт в скрипте.

    §9 п. 5 требует «выгрузку» на экране: эндпоинт без кнопки — это
    возможность, которой нельзя воспользоваться из окна.
    """
    page = client.get("/monitor").text

    assert 'id="export-csv"' in page
    assert 'id="export-json"' in page
    script = client.get("/static/monitor.js").text
    assert "/api/events/export" in script
    # фильтры экрана переносятся в выгрузку — «выгрузить то, что вижу»
    assert "state.kind" in script
    assert "state.name" in script
    assert "X-GapAudit-Token" in script


def test_matrix_screen_has_export_button(client: TestClient) -> None:
    """«Сейчас решать» выгружает точки матрицы: кнопка CSV + эндпоинт в скрипте."""
    page = client.get("/matrix").text

    assert 'id="export-csv"' in page
    script = client.get("/static/matrix.js").text
    assert "/api/matrix/export" in script
    assert "X-GapAudit-Token" in script


# --- экран «Сравнение» (шаг 3c) ----------------------------------------------


def test_compare_screen_renders_form_and_table(client: TestClient) -> None:
    """GET /compare: форма имён, шапка/тело таблицы, экспорт, скрипт экрана.

    Колонки рисует compare.js — в шаблоне только контейнеры. Тест держит их id:
    ``compare.js`` падает в ``null.value``/``null.appendChild``, и экран
    остаётся пустым молча (ошибка живёт в консоли WebView2, не в ответе).
    """
    response = client.get("/compare")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<a href="/compare" class="active">Сравнение</a>' in response.text
    assert response.text.count('class="active"') == 1
    for element_id in ("compare-form", "compare-msg", "compare-head",
                       "compare-body", "export-csv", "refresh"):
        assert f'id="{element_id}"' in response.text, element_id
    assert 'name="names"' in response.text
    assert "/static/compare.js" in response.text
    assert response.text.count("/static/card.js") == 1


def test_compare_js_uses_dom_not_innerhtml_for_data(client: TestClient) -> None:
    """compare.js собирает таблицу DOM-методами и умеет экспорт (gotcha 37)."""
    script = client.get("/static/compare.js").text

    assert "createElement" in script
    assert "textContent" in script
    # для данных innerHTML не используется вообще — даже как очистка
    assert "innerHTML" not in script
    assert "/api/compare" in script
    assert "/api/compare/export" in script
    assert "X-GapAudit-Token" in script
    assert "URL.createObjectURL" in script



# --- momentum в матрице, карточке и вкладках настроек (шаг P4b) --------------


def test_matrix_screen_uses_momentum_color(client: TestClient) -> None:
    """Точка матрицы красится по momentum: цвет приходит из API, JS его рисует.

    Проверяется связка «страница отдаёт скрипт» + «скрипт знает про
    momentum_color»: сам рендер в SVG проверяется живым прогоном, здесь — что
    шаблон и модуль не разъехались (``matrix.js`` подключён, поле читается).
    """
    page = client.get("/matrix")

    assert page.status_code == 200
    assert "/static/matrix.js" in page.text

    script = client.get("/static/matrix.js").text
    assert "momentum_color" in script
    assert "momentumFill(point)" in script
    # До P4b цвет означал признак ниши: старый тернарник не должен вернуться.
    assert "point.flag ? '#f87171' : '#4ade80'" not in script


def test_card_script_renders_momentum_block(client: TestClient) -> None:
    """Карточка пакета показывает блок «Momentum» из signals.momentum."""
    script = client.get("/static/card.js").text

    assert "card-momentum" in script
    assert "signals.momentum" in script
    assert "label_30d" in script
    assert "Мало данных" in script


def test_card_script_renders_cross_ecosystem_block(client: TestClient) -> None:
    """Карточка пакета рисует «3 полосы» cross-ecosystem из signals (шаг P4c)."""
    script = client.get("/static/card.js").text

    assert "card-cross-eco" in script
    assert "cross-eco-table" in script
    assert "signals.cross_ecosystem" in script
    assert "self_momentum_30d" in script
    # имя пакета и числа — только textContent (gotcha 37): innerHTML допустим
    # лишь для статической шапки таблицы без данных
    assert "tdName.textContent = r.name" in script


def test_style_has_cross_eco_table_rules(client: TestClient) -> None:
    """Стили таблицы cross-ecosystem подключены: класс из card.js есть в CSS."""
    stylesheet = client.get("/static/style.css").text

    assert ".cross-eco-table" in stylesheet
    assert ".cross-eco-table .self" in stylesheet


# --- блок Jobs в карточке (шаг P5a) ------------------------------------------


def test_card_script_renders_jobs_block(client: TestClient) -> None:
    """Карточка пакета рисует «Jobs · спрос на труд» из signals.jobs (P5a).

    Три состояния: таблица стран (собран), ссылка «Настройки → API-ключи»
    (ключ не настроен по signals_status), блок не рисуется (ещё не собирали).
    Строки стран и числа — только textContent (gotcha 37).
    """
    script = client.get("/static/card.js").text

    assert "card-jobs" in script
    assert "jobs-table" in script
    assert "signals.jobs" in script
    assert "signals_status" in script
    assert "API-ключ Adzuna не настроен" in script
    assert "The Adzuna API" in script  # атрибуция ToS (§14)
    assert "tdC.textContent = row.label" in script


def test_style_has_jobs_table_rules(client: TestClient) -> None:
    """Стили таблицы jobs подключены: класс из card.js есть в CSS."""
    stylesheet = client.get("/static/style.css").text

    assert ".card-jobs" in stylesheet
    assert ".jobs-table" in stylesheet
    assert ".jobs-table td.mono" in stylesheet


# --- блок Patents в карточке (шаг P5b) ---------------------------------------


def test_card_script_renders_patents_block(client: TestClient) -> None:
    """Карточка пакета рисует «Patents · патентный импульс» из signals.patents.

    Три состояния по signals_status: таблица по годам + импульс (собран),
    ссылка «Настройки → API-ключи» (ключ не настроен), «Ещё не собирали»
    (ключ есть, данных нет). Годы и числа — только textContent (gotcha 37),
    innerHTML — только у статической шапки таблицы.
    """
    script = client.get("/static/card.js").text

    assert "card-patents" in script
    assert "patents-table" in script
    assert "signals.patents" in script
    assert "signals_status.patents" in script
    assert "API-ключ PatentsView не настроен" in script
    assert "Ещё не собирали" in script
    assert "Импульс: " in script
    assert "PatentsView API" in script  # строка источника (§18.3)
    assert "tdY.textContent = row.year" in script


def test_style_has_patents_table_rules(client: TestClient) -> None:
    """Стили таблицы patents подключены: класс из card.js есть в CSS."""
    stylesheet = client.get("/static/style.css").text

    assert ".card-patents" in stylesheet
    assert ".patents-table" in stylesheet
    assert ".patents-table td.mono" in stylesheet


# --- блок Funding в карточке (шаг P5c) ---------------------------------------


def test_card_script_renders_funding_block(client: TestClient) -> None:
    """Карточка пакета рисует «Funding · раунды» из signals.funding (P5c).

    Три состояния по signals_status: таблица по кварталам + тренд (собран),
    ссылка «Настройки → API-ключи» (User-Agent не настроен), «Ещё не
    собирали» (User-Agent есть, данных нет). Кварталы и числа — только
    textContent (gotcha 37), innerHTML — только у статической шапки таблицы.
    """
    script = client.get("/static/card.js").text

    assert "card-funding" in script
    assert "funding-table" in script
    assert "signals.funding" in script
    assert "signals_status.funding" in script
    assert "User-Agent SEC EDGAR не настроен" in script
    assert "SEC EDGAR" in script  # строка источника (§18.3)
    assert "tdQ.textContent = 'Q' + row.quarter" in script
    assert "Тренд: " in script


def test_style_has_funding_table_rules(client: TestClient) -> None:
    """Стили таблицы funding подключены: класс из card.js есть в CSS."""
    stylesheet = client.get("/static/style.css").text

    assert ".card-funding" in stylesheet
    assert ".funding-table" in stylesheet
    assert ".funding-table td.mono" in stylesheet


def test_settings_tabs_switch(client: TestClient) -> None:
    """Две вкладки настроек: активная ровно одна, переключение — классом.

    Класс активной вкладки — ``tab-active``, а не ``active``: ``class="active"``
    на странице ровно один и принадлежит пункту навигации (тест выше это
    проверяет), и вторая «активная» сущность сломала бы его счёт.
    """
    page = client.get("/settings")

    assert page.text.count('data-tab="thresholds"') == 1
    assert page.text.count('data-tab="api-keys"') == 1
    assert page.text.count("tab-active") == 1
    assert 'class="tab-btn tab-active"' in page.text
    assert page.text.count('class="active"') == 1      # только пункт навигации

    script = client.get("/static/settings.js").text
    # Переключение живёт в showTab: активная получает tab-active, снятая — нет.
    assert "button.className = on ? 'tab-btn tab-active' : 'tab-btn'" in script
    assert "panel.hidden = panel.getAttribute('data-tab-panel') !== name" in script


def test_settings_links_to_api_keys_docs_with_anchor(client: TestClient) -> None:
    """Ссылка «как получить ключ» ведёт на якорь провайдера в API-KEYS.md."""
    script = client.get("/static/settings.js").text

    assert "'/docs/API-KEYS.md'" in script
    assert "'#' + anchor" in script
    # Документ существует и отдаётся (шаг P4a), а якоря появились в P4b.
    doc = client.get("/docs/API-KEYS.md")
    assert doc.status_code == 200
    for anchor in ("adzuna", "patentsview", "sec-edgar",
                   "yandex-wordstat", "google-trends", "apify"):
        assert f'<a id="{anchor}"></a>' in doc.text, anchor


def test_other_docs_files_are_not_served(client: TestClient) -> None:
    """``/docs/{file}`` — белый список: остальные файлы ``docs/`` отдают 404."""
    assert client.get("/docs/DEVPLAN.md").status_code == 404
    assert client.get("/docs/screenshots/step-p4b-matrix-momentum.png").status_code == 404
