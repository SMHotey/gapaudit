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
    """GET / — каркас: навигация, активный пункт «Задачи», отключённые — P3/2c."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<nav" in response.text
    assert "Задачи" in response.text
    # «Сейчас решать» — рабочий пункт с шага 2b (раньше был disabled P2b)
    assert '<a href="/matrix">Сейчас решать</a>' in response.text
    # будущие экраны: ссылка есть, но выключена (aria-disabled + title)
    assert 'class="disabled" title="P3: экран пакетов"' in response.text
    assert "Пакеты" in response.text


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
    """Экранный скрипт ходит в /api/matrix, карточку и умеет SSE."""
    response = client.get("/static/matrix.js")

    assert response.status_code == 200
    assert "/api/matrix" in response.text
    assert "/api/packages/" in response.text
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
                        "/static/settings.js"):
        script = client.get(script_path).text
        assert "window.__gapauditRefresh = load" in script, script_path


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

