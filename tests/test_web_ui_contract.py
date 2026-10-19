"""Контракт экрана «Задачи» (шаг 2a, §0.6): защита от возврата мёртвой ветки.

История: в шаге 3c ``POST /api/jobs/{id}/resume`` отвечал 501, и экран
показывал «продолжение не реализовано». В 3d-b эндпоинт стал настоящим,
ветка умерла. Тест держит два факта:

* в отдаваемой странице нет кода 501 — иначе вернулась заглушка;
* коды ошибок кнопки «Продолжить» (404 «задачи нет», 409 «не interrupted
  или нет лога») описаны в шаблоне, а не только в JS: ``jobs.js`` читает их
  из ``window.GapAuditJobsConfig``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


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
