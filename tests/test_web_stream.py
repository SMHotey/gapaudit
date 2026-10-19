"""SSE ``GET /api/stream`` (шаг 3b): события очереди, heartbeat, отписка.

Поток событий: ``JobQueue._emit`` (воркер-поток или тест) → ``EventCallback``
→ ``loop.call_soon_threadsafe`` → ``asyncio.Queue`` → генератор. Переход между
потоками здесь реально происходит: запрос обслуживает поток TestClient, а
``_emit`` вызывается из потока теста.

Сердцебиение не ждёт 15 секунд: тест опускает ``SSE_HEARTBEAT_INTERVAL`` до
0.1 с (в противном случае сюита провисала бы на реальном таймере).
Сеть — ноль, подпроцессы — ноль.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from fastapi.testclient import TestClient

from app import web as web_module
from tests.conftest import app_state, auth

FAKE_JOB_ID = "0" * 32


def _event_and_data(lines: list[str]) -> tuple[str, dict]:
    """Найти в прочитанных строках пару ``event:``/``data:`` и распарсить data."""
    for index, line in enumerate(lines):
        if not line.startswith("event: "):
            continue
        event_type = line[len("event: ") :]
        for following in lines[index + 1 :]:
            if following.startswith("data: "):
                return event_type, json.loads(following[len("data: ") :])
    raise AssertionError(f"полной пары event/data среди строк нет: {lines!r}")


def _read_until(
    client: TestClient,
    ready: Callable[[list[str]], bool],
    *,
    trigger: Callable[[], None] | None = None,
) -> list[str]:
    """Открыть поток, выполнить ``trigger``, читать строки до условия."""
    with client.stream("GET", "/api/stream", headers=auth()) as response:
        assert response.status_code == 200
        if trigger is not None:
            trigger()
        collected: list[str] = []
        for line in response.iter_lines():
            collected.append(line)
            if ready(collected):
                return collected
    raise AssertionError(f"условие не наступило; прочитано: {collected!r}")


def test_stream_response_headers(client: TestClient) -> None:
    with client.stream("GET", "/api/stream", headers=auth()) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"


def test_submit_emits_created_event(client: TestClient) -> None:
    """POST /api/jobs из другого потока → в потоке события ``created``."""
    result: dict = {}

    def post_job() -> None:
        result["response"] = client.post(
            "/api/jobs",
            json={"kind": "monitor.diff", "params": {"diff_from": "raw/predecessors.json"}},
            headers=auth(),
        )

    poster = threading.Thread(target=post_job, daemon=True)
    lines = _read_until(
        client,
        lambda seen: any(line.startswith("event: created") for line in seen)
        and any(line.startswith("data: ") for line in seen),
        trigger=poster.start,
    )
    # событие приходит раньше, чем POST-ответ дописан в другом потоке:
    # дожидаемся потока, иначе result ещё пуст
    poster.join(timeout=5.0)
    assert not poster.is_alive()

    assert "response" in result, "POST не дошёл до сервера"
    assert result["response"].status_code == 202
    job_id = result["response"].json()["job_id"]

    event_type, payload = _event_and_data(lines)
    assert event_type == "created"
    assert payload["job_id"] == job_id
    assert payload["status"] == "queued"


def test_emit_progress_reaches_stream(client: TestClient) -> None:
    queue = app_state(client).queue

    lines = _read_until(
        client,
        lambda seen: any(line.startswith("data: ") for line in seen),
        trigger=lambda: queue._emit(
            FAKE_JOB_ID, "progress", {"progress": 0.5, "message": "половина"}
        ),
    )

    event_type, payload = _event_and_data(lines)
    assert event_type == "progress"
    assert payload == {
        "job_id": FAKE_JOB_ID,
        "progress": 0.5,
        "message": "половина",
    }


def test_heartbeat_comment_arrives_without_waiting_15_seconds(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(web_module, "SSE_HEARTBEAT_INTERVAL", 0.1)

    with client.stream("GET", "/api/stream", headers=auth()) as response:
        assert response.status_code == 200
        first_line = next(response.iter_lines())

    assert first_line == ": keep-alive"


def test_stream_keeps_event_order_without_rewriting_it(client: TestClient) -> None:
    """``started`` после ``done`` доходит как есть — поток не «чинит» порядок.

    Инвариант AGENTS gotcha 24 / DEVPLAN §6: тип события и статус задачи —
    разные вещи; SSE только транслирует, ничего не перезаписывает.
    """
    queue = app_state(client).queue

    def emit_pair() -> None:
        queue._emit(FAKE_JOB_ID, "done", {"status": "done"})
        queue._emit(FAKE_JOB_ID, "started", {"status": "running"})

    lines = _read_until(
        client,
        lambda seen: sum(line.startswith("event: ") for line in seen) >= 2
        and sum(line.startswith("data: ") for line in seen) >= 2,
        trigger=emit_pair,
    )

    events = [line[len("event: ") :] for line in lines if line.startswith("event: ")]
    assert events == ["done", "started"]
    statuses = [
        json.loads(line[len("data: ") :])["status"]
        for line in lines
        if line.startswith("data: ")
    ]
    assert statuses == ["done", "running"]


def test_closing_stream_unsubscribes(client: TestClient) -> None:
    queue = app_state(client).queue

    with client.stream("GET", "/api/stream", headers=auth()) as response:
        assert response.status_code == 200
        assert len(queue._subscribers) == 1

    # закрытие соединения должно дойти до генератора и снять подписку
    deadline = time.monotonic() + 1.0
    while queue._subscribers and time.monotonic() < deadline:
        time.sleep(0.02)

    assert queue._subscribers == []
