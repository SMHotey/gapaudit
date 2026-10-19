"""Контракт экранов (шаг 2a, расширен в 2b): защита от возврата мёртвых веток.

История: в шаге 3c ``POST /api/jobs/{id}/resume`` отвечал 501, и экран
показывал «продолжение не реализовано». В 3d-b эндпоинт стал настоящим,
ветка умерла. Тест держит четыре факта:

* в отдаваемых страницах и отдаваемых скриптах нет кода 501 — иначе вернулась
  заглушка;
* коды ошибок кнопки «Продолжить» (404 «задачи нет», 409 «не interrupted
  или нет лога») описаны в шаблоне, а не только в JS: ``jobs.js`` читает их
  из ``window.GapAuditJobsConfig``;
* каждая ссылка навигации ведёт на существующий маршрут (в 2b «Сейчас решать»
  перестала быть выключенной — экран появился);
* выключенные пункты помечены ``aria-disabled`` — «серая» ссылка без этого
  атрибута читается скринридером как рабочая.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

WEB = Path(__file__).resolve().parent.parent / "web"

#: Файлы экранов, которые реально уезжают в браузер (шаблоны + статика).
RENDERED_FILES = (
    WEB / "views" / "base.html",
    WEB / "views" / "index.html",
    WEB / "views" / "matrix.html",
    WEB / "static" / "app.js",
    WEB / "static" / "jobs.js",
    WEB / "static" / "matrix.js",
    WEB / "static" / "sse.js",
    WEB / "static" / "style.css",
)

#: Пункты навигации: подпись -> маршрут. Остальные обязаны быть выключены.
NAV_ROUTES = {"Задачи": "/", "Сейчас решать": "/matrix"}

_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.DOTALL)
_HREF = re.compile(r'href="([^"]+)"')


def test_no_dead_501_branch_on_index(client: TestClient) -> None:
    """Мёртвая ветка «resume ещё не реализован» не возвращается."""
    response = client.get("/")

    assert response.status_code == 200
    assert "501" not in response.text


def test_resume_error_codes_and_ids_are_present(client: TestClient) -> None:
    """404/409 описаны в странице, контейнеры фильтра и обновления на месте."""
    response = client.get("/")

    assert response.status_code == 200
    assert "409" in response.text
    assert "404" in response.text
    assert 'id="jobs"' in response.text
    assert 'id="counter"' in response.text
    assert 'id="refresh"' in response.text


def test_no_501_in_any_rendered_file(client: TestClient) -> None:
    """Ни один файл экранов и ни одна отданная страница не содержит «501».

    Проверка дешёвая и повторяется на двух уровнях: файл на диске (ловит
    заглушку в исходнике) и ответ сервера (ловит заглушку, подставленную
    шаблонизатором).
    """
    for path in RENDERED_FILES:
        assert path.exists(), path
        assert "501" not in path.read_text(encoding="utf-8"), path

    for url in ("/", "/matrix"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert "501" not in response.text, url


def test_navigation_links_point_to_existing_routes(client: TestClient) -> None:
    """Навигация: живые пункты ведут на существующие экраны, остальные выключены.

    Тест, который ловит забытую активацию: в 2a «Сейчас решать» была
    ``disabled``, в 2b — рабочая ссылка на ``/matrix``. Если экран появился, а
    навигацию не тронули, пункт останется выключенным и тест скажет об этом.
    """
    page = client.get("/").text
    anchors = [
        (attrs, re.sub(r"<[^>]+>", "", label).strip())
        for attrs, label in _ANCHOR.findall(page)
    ]
    nav = [(attrs, label) for attrs, label in anchors if label in NAV_ROUTES]
    assert {label for _, label in nav} == set(NAV_ROUTES)

    for attrs, label in nav:
        href = _HREF.search(attrs)
        assert href is not None, f"пункт {label!r} без href"
        route = href.group(1)
        assert route == NAV_ROUTES[label], label
        assert "aria-disabled" not in attrs, label
        # маршрут действительно существует
        assert client.get(route).status_code == 200, route

    # Все прочие пункты навигации — выключены явно.
    others = [
        (attrs, label) for attrs, label in anchors
        if label and label not in NAV_ROUTES and label != "GapAudit"
    ]
    assert others, "в навигации должны остаться выключенные пункты P3/2c"
    for attrs, label in others:
        assert 'aria-disabled="true"' in attrs, label
        assert "disabled" in attrs, label
        assert "href" not in attrs, label

    for url in ("/", "/matrix"):
        assert client.get(url).text.count('class="active"') == 1, url
