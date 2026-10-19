"""GapAudit — точка входа приложения (шаг 0.1: только каркас).

Порядок работы (см. DEVPLAN.md §8):

1. берём свободный порт у ОС (не хардкодим);
2. генерируем сессионный токен;
3. поднимаем uvicorn в демон-потоке, приложение видит токен и порт;
4. ждём готовности ``/api/health`` (до 10 секунд) — иначе будет белый экран;
5. сразу после готовности health пишем токен в ``<data_dir>/session.token``
   (права 0600) — так его находят внешние клиенты (DEVPLAN §7, шаг 3b);
6. открываем окно pywebview (или системный браузер в режиме ``--browser``);
7. по закрытию окна удаляем ``session.token``, останавливаем сервер и выходим
   с кодом 0 — токен живёт ровно столько, сколько живёт приложение.

``webview.start()`` вызывается только из ``main()`` и только в главном потоке —
pywebview этого требует, поэтому бэкенд обязан жить в демоне.

Запуск:
  python desktop.py              обычное приложение
  python desktop.py --dev        devtools включены, логи uvicorn подробные
  python desktop.py --browser    без окна, открыть системный браузер
  python desktop.py --port 8790  фиксированный порт вместо свободного
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx
import uvicorn
import webview

from app.session import remove_token, write_token
from app.web import VERSION, create_app
from core.settings import Settings, from_env, resolve_data_dir

HOST = "127.0.0.1"
WINDOW_TITLE = "GapAudit"
HEALTH_PATH = "/api/health"
HEALTH_TIMEOUT = 10.0
HEALTH_POLL_INTERVAL = 0.1
SHUTDOWN_JOIN_TIMEOUT = 3.0


def free_port(host: str = HOST) -> int:
    """Свободный порт: ОС сама сообщает номер после bind на порт 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def make_server(app, port: int, dev: bool) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host=HOST,
        port=port,
        log_level="debug" if dev else "warning",
        access_log=dev,
    )
    server = uvicorn.Server(config)
    # В этом потоке сигналы обрабатывать нечем: главный поток занят окном.
    # В uvicorn >= 0.30 такого атрибута уже нет (он сам пропускает установку
    # обработчиков вне главного потока), но на старых версиях флаг нужен.
    if hasattr(server, "install_signal_handlers"):
        server.install_signal_handlers = False
    return server


def start_server(server: uvicorn.Server) -> threading.Thread:
    thread = threading.Thread(target=server.run, name="gapaudit-uvicorn", daemon=True)
    thread.start()
    return thread


def data_dir_for(settings: Settings | None = None) -> Path:
    """Каталог данных текущего запуска.

    Настройки берутся из окружения (``GAPAUDIT_DATA_DIR`` и др.), а не
    голым ``Settings()``: иначе явно заданный каталог данных пользователем
    был бы проигнорирован и перезаписан значением по умолчанию.
    """
    resolved = resolve_data_dir(settings if settings is not None else from_env())
    # один каталог на весь шаг: его же читает lifespan приложения
    # (``GAPAUDIT_DATA_DIR`` → ``core.settings.from_env``) и ``app/session.py``
    os.environ["GAPAUDIT_DATA_DIR"] = str(resolved)
    return resolved


def health_url(port: int) -> str:
    return f"http://{HOST}:{port}{HEALTH_PATH}"


def wait_health(port: int, token: str, timeout: float = HEALTH_TIMEOUT) -> bool:
    """Ждём ``/api/health`` до ``timeout`` секунд, опрашивая раз в 100 мс."""
    deadline = time.monotonic() + timeout
    headers = {"X-GapAudit-Token": token}
    with httpx.Client(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(health_url(port), headers=headers)
                if response.status_code == 200 and response.json().get("ok"):
                    return True
            except Exception:
                pass
            time.sleep(HEALTH_POLL_INTERVAL)
    return False


def stop_server(server: uvicorn.Server, thread: threading.Thread | None) -> None:
    server.should_exit = True
    if thread is not None and thread.is_alive():
        thread.join(timeout=SHUTDOWN_JOIN_TIMEOUT)
    print("[gapaudit] сервер остановлен")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="desktop.py", description="GapAudit (каркас 0.1)")
    parser.add_argument("--dev", action="store_true", help="devtools и подробные логи")
    parser.add_argument("--browser", action="store_true", help="без окна, открыть системный браузер")
    parser.add_argument("--port", type=int, default=None, help="фиксированный порт (по умолчанию свободный)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    token = secrets.token_urlsafe(32)
    port = args.port or free_port()
    data_dir = data_dir_for()
    app = create_app(token=token, port=port)
    server = make_server(app, port, args.dev)
    thread = start_server(server)

    url = f"http://{HOST}:{port}/?token={token}"
    print(f"[gapaudit] версия {VERSION}, режим {'браузер' if args.browser else 'окно'}")

    if not wait_health(port, token):
        print(
            f"[gapaudit] ОШИБКА: {HEALTH_PATH} не ответил за {HEALTH_TIMEOUT:.0f} с "
            f"(порт {port})",
            file=sys.stderr,
        )
        stop_server(server, thread)
        return 1

    # внешний клиент (Cursor/Claude) находит токен только здесь — см. шаг 3b
    token_path = write_token(data_dir, token)
    print(f"[gapaudit] бэкенд готов: {url}")
    print(f"[gapaudit] токен для внешних клиентов: {token_path}")

    if args.browser:
        webbrowser.open(url)
        print("[gapaudit] окно не открывается, Ctrl+C — остановить сервер")
        try:
            while thread.is_alive():
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("[gapaudit] получен Ctrl+C")
    else:
        webview.create_window(
            WINDOW_TITLE,
            url=url,
            width=1280,
            height=860,
            min_size=(1024, 720),
            # TODO(step-3): диалог подтверждения закрытия вернём вместе со своей
            # JS-реализацией, когда появятся задачи (сейчас подтверждать нечего).
            confirm_close=False,
        )
        webview.start(debug=args.dev)

    # окно закрыто: токен на диске больше не должен вести к живому бэкенду
    remove_token(data_dir)
    stop_server(server, thread)
    return 0


if __name__ == "__main__":
    sys.exit(main())
