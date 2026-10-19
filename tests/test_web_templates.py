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
    """GET / — каркас: навигация, активный пункт «Задачи», отключённые — 2b/P3."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<nav" in response.text
    assert "Задачи" in response.text
    # будущие экраны: ссылка есть, но выключена (aria-disabled + title)
    assert 'class="disabled" title="P2b: матрица adoption x flag"' in response.text
    assert "Сейчас решать" in response.text


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
