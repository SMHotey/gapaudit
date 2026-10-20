"""Контракт экранов (шаг 2a, расширен в 2b, 2c и 3a): защита от мёртвых веток.

История: в шаге 3c ``POST /api/jobs/{id}/resume`` отвечал 501, и экран
показывал «продолжение не реализовано». В 3d-b эндпоинт стал настоящим,
ветка умерла. Тест держит пять фактов:

* в отдаваемых страницах и отдаваемых скриптах нет кода 501 — иначе вернулась
  заглушка;
* коды ошибок кнопки «Продолжить» (404 «задачи нет», 409 «не interrupted
  или нет лога») описаны в шаблоне, а не только в JS: ``jobs.js`` читает их
  из ``window.GapAuditJobsConfig``;
* у каждого живого экрана на месте его контейнеры (матрица — SVG, оверлей
  карточки — один на все экраны в ``base.html``, таблица кандидатов, счётчик
  «ВОТ СЮДА», поиск и пагинация «Пакетов», таймлайн «Мониторинга»);
* каждая живая ссылка навигации ведёт на существующий маршрут, а после 3c
  живых пунктов семь: «Задачи», «Сейчас решать», «Пакеты», «Мониторинг»,
  «Каталоги», «Сравнение», «Настройки»;
* выключенных пунктов не осталось: P3 закрыт, и «серая» ссылка без ``href``
  вернулась бы как регрессия.
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
    WEB / "views" / "catalogs.html",
    WEB / "views" / "catalog_detail.html",
    WEB / "views" / "compare.html",
    WEB / "views" / "monitor.html",
    WEB / "views" / "packages.html",
    WEB / "views" / "settings.html",
    WEB / "static" / "app.js",
    WEB / "static" / "card.js",
    WEB / "static" / "catalog_detail.js",
    WEB / "static" / "catalogs.js",
    WEB / "static" / "compare.js",
    WEB / "static" / "jobs.js",
    WEB / "static" / "matrix.js",
    WEB / "static" / "monitor.js",
    WEB / "static" / "niche.js",
    WEB / "static" / "packages.js",
    WEB / "static" / "settings.js",
    WEB / "static" / "sse.js",
    WEB / "static" / "style.css",
)

#: Пункты навигации: подпись -> маршрут. Остальные обязаны быть выключены.
#: 3a: «Пакеты» и «Мониторинг» стали живыми — экраны появились.
#: 3b: «Каталоги» стала живой; 3c закрыл P3 — «Сравнение» тоже живая ссылка,
#: выключенных пунктов в шапке больше нет.
NAV_ROUTES = {
    "Задачи": "/",
    "Сейчас решать": "/matrix",
    "Пакеты": "/packages",
    "Мониторинг": "/monitor",
    "Каталоги": "/catalogs",
    "Сравнение": "/compare",
    "Поиск ниши": "/niche",
    "Настройки": "/settings",
}

#: Порядок пунктов в шапке — часть контракта: он совпадает с §9 DEVPLAN.
NAV_ORDER = [
    "Задачи", "Сейчас решать", "Пакеты", "Мониторинг",
    "Каталоги", "Сравнение", "Поиск ниши", "Настройки",
]

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

    for url in ("/", "/matrix", "/packages", "/monitor", "/catalogs", "/compare",
                "/niche", "/settings"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert "501" not in response.text, url


def test_matrix_page_keeps_its_containers(client: TestClient) -> None:
    """Матрица: контейнеры SVG, карточки и таблицы кандидатов не исчезли.

    Контракт экрана «Сейчас решать»: без любого из этих id ``matrix.js``
    падает в ``null.querySelector``, и экран остаётся пустым — молча, потому
    что ошибка живёт в консоли WebView2, а не в ответе сервера.
    """
    response = client.get("/matrix")

    assert response.status_code == 200
    assert "501" not in response.text
    for element_id in (
        "matrix-svg", "card-overlay", "candidates-body", "count-high-no-flag",
    ):
        assert f'id="{element_id}"' in response.text, element_id


def test_navigation_has_no_disabled_items(client: TestClient) -> None:
    """Все восемь пунктов шапки — живые ссылки: выключенных нет (P5f-1: niche).

    Тест держит обе стороны одной ошибки: забытая активация (пункт остался
    ``disabled``) и ссылка на несуществующий маршрут (``href`` без страницы) —
    обе ломают навигацию одинаково тихо.
    """
    page = client.get("/").text
    anchors = [
        (attrs, re.sub(r"<[^>]+>", "", label).strip())
        for attrs, label in _ANCHOR.findall(page)
    ]
    nav = [(attrs, label) for attrs, label in anchors if label in NAV_ROUTES]
    others = [
        (attrs, label) for attrs, label in anchors
        if label and label not in NAV_ROUTES and label != "GapAudit"
    ]

    assert [label for _, label in nav] == [
        "Задачи", "Сейчас решать", "Пакеты", "Мониторинг", "Каталоги",
        "Сравнение", "Поиск ниши", "Настройки",
    ]
    assert others == [], "в шапке не осталось выключенных пунктов (3c закрыл P3)"
    for attrs, label in nav:
        href = _HREF.search(attrs)
        assert href is not None, f"пункт {label!r} без href"
        assert href.group(1) == NAV_ROUTES[label], label
        assert "disabled" not in attrs, label
        assert "aria-disabled" not in attrs, label


def test_navigation_links_point_to_existing_routes(client: TestClient) -> None:
    """Навигация: каждый пункт ведёт на существующий экран и подсвечен один.

    Тест, который ловит забытую активацию: в 2a «Сейчас решать» была
    ``disabled``, в 2b — рабочая ссылка на ``/matrix``; в 3c то же случилось бы
    со «Сравнением», если бы экран появился, а шапку не тронули.
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
        # маршрут действительно существует
        assert client.get(route).status_code == 200, route

    # ровно один активный пункт на каждом экране — шапка не «подсвечивает всё»
    for url in ("/", "/matrix", "/packages", "/monitor", "/catalogs",
                "/catalogs/abc123", "/compare", "/settings"):
        assert client.get(url).text.count('class="active"') == 1, url


def test_nav_order_matches_plan(client: TestClient) -> None:
    """Порядок пунктов шапки — как в DEVPLAN §9 (это порядок вопросов, не алфавит)."""
    page = client.get("/").text
    labels = [
        re.sub(r"<[^>]+>", "", label).strip()
        for _attrs, label in _ANCHOR.findall(page)
    ]
    assert [label for label in labels if label in NAV_ORDER] == NAV_ORDER


def test_card_overlay_exists_once_on_every_screen(client: TestClient) -> None:
    """Оверлей карточки — один на страницу (шаг 3a: переехал в base.html).

    Два элемента с ``id="card-overlay"`` (копия в base.html плюс копия в
    шаблоне экрана) дали бы пустую вторую карточку: ``getElementById`` находит
    первую, а вторая остаётся висеть поверх страницы.
    """
    for url in ("/", "/matrix", "/packages", "/monitor", "/catalogs", "/compare",
                "/niche", "/settings"):
        page = client.get(url).text
        assert page.count('id="card-overlay"') == 1, url
        assert page.count('id="card-content"') == 1, url
        assert page.count("/static/card.js") == 1, url
