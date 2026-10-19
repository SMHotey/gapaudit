"""Сквозная проверка подключения SSE (шаг 2b): экраны не забыли подписку.

Сам поток (``GET /api/stream``) покрыт ``tests/test_web_stream.py`` и
``tests/test_web_stream_worker.py``; здесь проверяется клиентская сторона —
то, что ломается молча: скрипт лежит рядом, но ни один экран его не подключил,
или ``sse.js`` подключён дважды (два EventSource на одну страницу → каждое
событие обрабатывается дважды: таблица перестраивается вдвое чаще).

Сеть — ноль, подпроцессы — ноль.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_sse_module_is_loaded_once_after_app_js(client: TestClient) -> None:
    """sse.js идёт после app.js (нужен ``window.GapAudit.token``) и ровно один раз."""
    for path in ("/", "/matrix"):
        page = client.get(path).text

        assert page.count('/static/sse.js') == 1, path
        assert page.index('/static/app.js') < page.index('/static/sse.js'), path
        assert "/static/matrix.js" in page or "/static/jobs.js" in page, path


def test_sse_module_reads_token_and_stream_path(client: TestClient) -> None:
    """Общий модуль берёт токен из app.js и ходит в /api/stream через query."""
    script = client.get("/static/sse.js").text

    assert script.count("GapAuditSSE") >= 1
    assert "window.GapAudit && window.GapAudit.token" in script
    assert "/api/stream?token=" in script
    # переподключение: EventSource перезапускается сам, явный CLOSED — наш
    assert "EventSource.CLOSED" in script


def test_both_screens_subscribe_to_events(client: TestClient) -> None:
    """Оба экранных скрипта подписываются на поток, а не только «лежат рядом»."""
    jobs = client.get("/static/jobs.js").text
    matrix = client.get("/static/matrix.js").text

    assert "window.GapAuditSSE.subscribe" in jobs
    assert "window.GapAuditSSE.subscribe" in matrix
    # «Задачи» перестраивают список только на событиях статуса, progress — точечно
    assert "'progress'" in jobs
    assert "applyProgress" in jobs
    # «Сейчас решать» перечитывает матрицу на done (наблюдения пишет пул lifespan)
    assert "'done'" in matrix
