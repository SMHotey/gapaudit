"""GapAudit — точка входа приложения (шаг 3c: окно, задачи, shutdown).

Порядок работы (см. DEVPLAN.md §8):

1. берём свободный порт у ОС (не хардкодим);
2. генерируем сессионный токен;
3. поднимаем uvicorn в демон-потоке, приложение видит токен и порт;
4. ждём готовности ``/api/health`` (до 10 секунд) — иначе будет белый экран;
5. сразу после готовности health пишем токен в ``<data_dir>/session.token``
   (права 0600) — так его находят внешние клиенты (DEVPLAN §7, шаг 3b);
6. открываем окно pywebview (или системный браузер в режиме ``--browser``);
   в оконном режиме за титулом следит фоновый поток «GapAudit — N задач»,
   а закрытие с активными задачами спрашивает подтверждение (JS ``confirm``);
7. по закрытию окна гасим очередь задач, останавливаем сервер, удаляем
   ``session.token`` и выходим с кодом 0 — токен живёт ровно столько, сколько
   живёт приложение.

``webview.start()`` вызывается только из ``main()`` и только в главном потоке —
pywebview этого требует, поэтому бэкенд обязан жить в демоне.

Порядок остановки (AGENTS gotcha 27): окно закрыто → ``queue.stop`` →
uvicorn ``should_exit`` + join → ``remove_token`` → ``exit(0)``. Очередь,
которая не уложилась в таймаут, — предупреждение в stderr и всё равно
exit 0: незакрытое окно хуже незавершённых процессов. Закрытие окна раньше
~2 с после старта даёт ``E_ABORT (0x80004004)`` из WebView2 — это не ошибка
(gotcha 10): ловим, пишем в stderr и выходим с 0.

Запуск:
  python desktop.py              обычное приложение
  python desktop.py --dev        devtools включены, логи uvicorn подробные
  python desktop.py --browser    без окна, открыть системный браузер
  python desktop.py --port 8790  фиксированный порт вместо свободного
  python desktop.py --demo       демо-режим: данные из фикстур, сеть выключена,
                                 каталог данных — временный (P4d)

Переменные окружения: ``GAPAUDIT_DATA_DIR`` (каталог данных),
``GAPAUDIT_DEMO`` (1 — то же, что ``--demo``) и ``GAPAUDIT_NO_BROWSER``
(не открывать системный браузер в режиме ``--browser`` — этим пользуется
smoke-тест ``tests/test_desktop_browser_smoke``, чтобы проверка не открывала
пользователю лишнюю вкладку).
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

import httpx
import uvicorn
import webview

from app.session import remove_token, write_token
from app.web import TOKEN_HEADER, VERSION, create_app
from core.settings import Settings, from_env, resolve_data_dir

HOST = "127.0.0.1"
WINDOW_TITLE = "GapAudit"
HEALTH_PATH = "/api/health"
HEALTH_TIMEOUT = 10.0
HEALTH_POLL_INTERVAL = 0.1
SHUTDOWN_JOIN_TIMEOUT = 3.0

#: Сколько гасить очередь задач до «принудительно» (gotcha 27).
QUEUE_STOP_TIMEOUT = 5.0
#: Как часто обновлять счётчик задач в заголовке окна.
TITLE_INTERVAL = 2.0
#: Таймаут одного HTTP-запроса фонового потока заголовка.
TITLE_HTTP_TIMEOUT = 1.0
#: Код E_ABORT WebView2 (0x80004004) в беззнаковом и знаковом виде.
E_ABORT_HRESULT = 0x80004004
E_ABORT_HRESULT_SIGNED = -2147467260

#: Статусы, которые считаются «активными» для заголовка и диалога закрытия.
#: ``interrupted`` не считается: это ждущая решения задача, а не работающая.
ACTIVE_STATUSES = ("queued", "running")

NO_BROWSER_ENV = "GAPAUDIT_NO_BROWSER"
#: Демо-режим (шаг P4d): данные из фикстур, каталог данных — временный.
DEMO_ENV = "GAPAUDIT_DEMO"


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


# --- чистая логика (тестируется без окна, tests/test_desktop_logic.py) ----------


def _jobs_word(count: int) -> str:
    """Склонение «задача/задачи/задач»: русская морфология числа."""
    last_two = count % 100
    last = count % 10
    if last == 1 and last_two != 11:
        return "задача"
    if 2 <= last <= 4 and not 12 <= last_two <= 14:
        return "задачи"
    return "задач"


def format_title(active_jobs: int) -> str:
    """``'GapAudit'`` без задач, ``'GapAudit — N задач(и)'`` с ними."""
    if active_jobs <= 0:
        return WINDOW_TITLE
    return f"{WINDOW_TITLE} — {active_jobs} {_jobs_word(active_jobs)}"


def should_confirm_close(active_jobs: int) -> bool:
    """True — показать диалог «Остановить и выйти / Остаться»; False — закрыть сразу."""
    return active_jobs > 0


def is_e_abort(exc: BaseException) -> bool:
    """True, если ``exc`` — ``E_ABORT`` WebView2 (закрытие окна до инициализации).

    Распознаём и текст (``"E_ABORT"``, ``"0x80004004"`` — в любом регистре), и
    числовой ``hresult``/``errno``/``code`` на самом исключении: разные слои
    (.NET, pywebview, comtypes) отдают одно и то же разными типами.
    """
    text = str(exc).upper()
    if "E_ABORT" in text or "0X80004004" in text:
        return True
    for attribute in ("hresult", "errno", "code"):
        value = getattr(exc, attribute, None)
        if value in (E_ABORT_HRESULT, E_ABORT_HRESULT_SIGNED):
            return True
    return False


def count_active(jobs: list[dict[str, Any]]) -> int:
    """Сколько задач в списках ``queued``/``running`` (``interrupted`` не считаем)."""
    return sum(1 for job in jobs if job.get("status") in ACTIVE_STATUSES)


def active_jobs_count(application: Any) -> int:
    """Активные задачи из очереди в ``app.state`` (без HTTP, из процесса).

    ``queue`` появляется в ``app.state`` только после lifespan, поэтому до
    готовности health это 0, а не ошибка.
    """
    queue = getattr(getattr(application, "state", None), "queue", None)
    if queue is None:
        return 0
    return count_active([{"status": job.status} for job in queue.list(limit=1000)])


def poll_active_jobs(port: int, token: str, *, timeout: float = TITLE_HTTP_TIMEOUT) -> int:
    """Активные задачи через ``GET /api/jobs`` — так считает фоновый поток окна.

    Один запрос каждые ``TITLE_INTERVAL`` секунд на локальном адресе: дёшево
    и не требует доступа к ``app.state`` из другого потока.
    """
    response = httpx.get(
        f"http://{HOST}:{port}/api/jobs",
        headers={TOKEN_HEADER: token},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    return count_active(jobs if isinstance(jobs, list) else [])


def title_loop(
    window: Any,
    port: int,
    token: str,
    stop: threading.Event,
    *,
    interval: float = TITLE_INTERVAL,
    poll: Callable[[int, str], int] | None = None,
) -> None:
    """Фоновый поток заголовка: каждые ``interval`` секунд «GapAudit — N задач».

    ``stop`` — ``threading.Event``: поток выходит сразу после закрытия окна.
    Ошибка запроса (сервер уже гасится) не роняет поток: пропускаем такт.
    """
    fetch = poll if poll is not None else poll_active_jobs
    _update_title(window, port, token, fetch)
    while not stop.wait(interval):
        if not _update_title(window, port, token, fetch):
            return


def _update_title(
    window: Any,
    port: int,
    token: str,
    fetch: Callable[[int, str], int],
) -> bool:
    """Один такт: пересчитать активные задачи и написать заголовок. False — выйти."""
    try:
        active = fetch(port, token)
    except Exception:  # noqa: BLE001 - сервер гасится, это не ошибка потока
        return True
    try:
        window.set_title(format_title(active))
    except Exception:  # noqa: BLE001 - окно закрылось, поток больше не нужен
        return False
    return True


def make_closing_handler(application: Any, window: Any) -> Callable[[], bool]:
    """Обработчик ``window.events.closing``: спросить про активные задачи.

    pywebview вызывает обработчик в потоке UI (``Event(..., should_lock=True)``,
    см. ``webview/event.py``), а ``evaluate_js`` в Windows собирает результат
    через ``Control.Invoke`` + семафор — то есть ждёт ответа, который придёт в
    тот же поток UI. Прямой вызов ``evaluate_js`` внутри обработчика therefore
    подвешивал бы окно (deadlock), поэтому здесь только решение «закрываться
    или нет», а сам диалог идёт в фоне: подтверждено → ``window.destroy()``.

    Возвращает ``False`` (закрытие отменено) в момент, когда диалог ещё не
    показан, и ``True``, когда закрываться можно: задач нет или пользователь
    уже подтвердил выход.
    """
    state = {"asking": False, "approved": False}

    def ask_and_close() -> None:
        try:
            allowed = bool(window.evaluate_js("window.confirmClose()"))
        except Exception:  # noqa: BLE001 - окно в моменте уже умирает
            allowed = True  # блокировать закрытие из-за сбоя диалога нельзя
        if allowed:
            state["asking"] = False
            state["approved"] = True
            try:
                window.destroy()
            except Exception:  # noqa: BLE001 - окно могло закрыться само
                pass
        else:
            state["asking"] = False  # «Остаться»: следующий клик снова спросит

    def on_closing() -> bool:
        if state["approved"]:
            return True
        active = active_jobs_count(application)
        if not should_confirm_close(active):
            return True  # задач нет — закрываться можно сразу
        if state["asking"]:
            return False  # диалог уже висит, второй не открываем
        state["asking"] = True
        threading.Thread(
            target=ask_and_close, name="gapaudit-confirm-close", daemon=True
        ).start()
        return False  # немедленное закрытие отменяем, решит диалог

    return on_closing


def stop_queue(queue: Any, timeout: float = QUEUE_STOP_TIMEOUT) -> bool:
    """``queue.stop(timeout)``; False — очередь не уложилась в таймаут (gotcha 27)."""
    started = time.monotonic()
    queue.stop(timeout=timeout)
    return (time.monotonic() - started) < timeout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="desktop.py", description="GapAudit (каркас 0.1)")
    parser.add_argument("--dev", action="store_true", help="devtools и подробные логи")
    parser.add_argument("--browser", action="store_true", help="без окна, открыть системный браузер")
    parser.add_argument("--port", type=int, default=None, help="фиксированный порт (по умолчанию свободный)")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="демо-режим: данные из фикстур, сеть выключена, каталог данных временный",
    )
    return parser


def prepare_demo(env: dict[str, str] | None = None) -> Path:
    """Включить демо-режим: флаг в окружении + временный каталог данных (P4d).

    ``GAPAUDIT_DATA_DIR`` указывает на свежий каталог из ``tempfile``: демо не
    читает и не засоряет ``%USERPROFILE%\\GapAudit-data``, а ``store`` получает
    обычный файл (``:memory:`` не годится — ``Store`` открывает ``lifespan``).
    Возвращённый каталог вызывающий удаляет при выходе.
    """
    from tools.demo_mode import make_demo_data_dir

    target = os.environ if env is None else env
    target[DEMO_ENV] = "1"
    demo_dir = make_demo_data_dir()
    target["GAPAUDIT_DATA_DIR"] = str(demo_dir)
    return demo_dir


def seed_demo(application: Any) -> dict[str, int]:
    """Наполнить ``Store`` демо-данными после готовности health.

    Вызывается между ``wait_health`` и открытием окна: экран матрицы читает
    уже наполненную базу. Без ``app.state.store`` (приложение не стартовало)
    возвращает пустой словарь — это не ошибка запуска.
    """
    store = getattr(getattr(application, "state", None), "store", None)
    if store is None:
        return {}
    from tools.demo_mode import seed_demo_store

    return seed_demo_store(store)


def _dispatch_cli(argv: list[str]) -> int | None:
    """В frozen-сборке ``sys.executable`` = ``gapaudit.exe``. Если argv
    начинается с ``-m <module>``, импортировать модуль и вызвать ``main()``.
    Возвращает exit code или None (не CLI-режим).

    В source (не frozen) — возвращает None сразу: обычный
    ``python -m cli.*`` работает без этого диспетчера.

    Используется ``app/jobs.py::_default_runner``, который в frozen-сборке
    собирает ``[sys.executable, "-m", module, ...]``, то есть тот же argv,
    что и в source. Диспетчер перехватывает ``-m`` до argparse
    (AGENTS gotcha 75).
    """
    if not getattr(sys, "frozen", False):
        return None
    if len(argv) < 2 or argv[0] != "-m":
        return None
    # stdout/stderr у windowed-сборки без консоли — кодировка локали (cp1251)
    # в strict-режиме, а JSON-протокол и прогресс CLI содержат кириллицу и «→»
    # (core/catalog.py:350): без utf-8 первая же не-ASCII строка даёт
    # UnicodeEncodeError (пойман в P4d-3: niche.find падал на catalog.build).
    # Родитель (app/jobs.py::Popen) читает оба потока как utf-8 — кодировки
    # согласованы. В source dispatch не вызывается, потоки не трогаются.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - поток уже закрыт/не TextIO
                pass
    module_name = argv[1]
    rest = argv[2:]
    try:
        import importlib

        mod = importlib.import_module(module_name)
    except Exception as e:
        print(
            f"[gapaudit] не удалось импортировать {module_name}: {e}",
            file=sys.stderr,
        )
        return 2
    if not hasattr(mod, "main"):
        print(f"[gapaudit] {module_name} не имеет main()", file=sys.stderr)
        return 2
    try:
        return mod.main(rest)
    except SystemExit as e:
        return int(e.code) if e.code is not None else 0
    except Exception as e:
        print(f"[gapaudit] ошибка в {module_name}: {e}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    # Frozen-сборка: задачи шлют argv как [sys.executable, "-m", module, ...],
    # а sys.executable = сам .exe без -m (gotcha 75). Диспетчер перехватывает
    # "-m" до argparse; в source возвращает None — путь не меняется.
    effective = sys.argv[1:] if argv is None else argv
    cli_exit = _dispatch_cli(effective)
    if cli_exit is not None:
        return cli_exit
    args = build_parser().parse_args(effective)

    demo_dir: Path | None = None
    if args.demo:
        demo_dir = prepare_demo()
    demo = os.environ.get(DEMO_ENV) == "1"

    token = secrets.token_urlsafe(32)
    port = args.port or free_port()
    data_dir = data_dir_for()
    application = create_app(token=token, port=port)
    server = make_server(application, port, args.dev)
    thread = start_server(server)

    url = f"http://{HOST}:{port}/?token={token}"
    print(f"[gapaudit] версия {VERSION}, режим {'браузер' if args.browser else 'окно'}"
          f"{', демо' if demo else ''}")

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

    if demo:
        seeded = seed_demo(application)
        if seeded:
            print(
                "[gapaudit] демо: {observations} наблюдений, {events} событий, "
                "{catalogs} каталога ({items} строк)".format(**seeded)
            )

    stop_titles = threading.Event()
    try:
        if args.browser:
            if not os.environ.get(NO_BROWSER_ENV):
                webbrowser.open(url)
            print("[gapaudit] окно не открывается, Ctrl+C — остановить сервер")
            try:
                while thread.is_alive():
                    time.sleep(0.2)
            except KeyboardInterrupt:
                print("[gapaudit] получен Ctrl+C")
        else:
            window = webview.create_window(
                WINDOW_TITLE,
                url=url,
                width=1280,
                height=860,
                min_size=(1024, 720),
                # Подтверждение закрытия — своё (JS-диалог с задачами), а не
                # нативный OK/Cancel из pywebview: два диалога подряд путают.
                confirm_close=False,
            )
            if window is None:
                raise RuntimeError("pywebview не создал окно")
            window.events.closing += make_closing_handler(application, window)
            threading.Thread(
                target=title_loop,
                args=(window, port, token, stop_titles),
                name="gapaudit-title",
                daemon=True,
            ).start()
            webview.start(debug=args.dev)
    except Exception as exc:  # noqa: BLE001 - E_ABORT при закрытии <2 с (gotcha 10)
        if not is_e_abort(exc):
            raise
        print(
            "[gapaudit] окно закрыто до инициализации WebView2 — это нормально",
            file=sys.stderr,
        )
    finally:
        # порядок shutdown — AGENTS gotcha 27: очередь → uvicorn → токен → 0
        stop_titles.set()
        queue = getattr(getattr(application, "state", None), "queue", None)
        if queue is not None and not stop_queue(queue):
            print(
                "[gapaudit] не все задачи завершились, принудительно",
                file=sys.stderr,
            )
        stop_server(server, thread)
        remove_token(data_dir)
        if demo_dir is not None:
            # Демо-данные живут один запуск: временный каталог удаляется.
            shutil.rmtree(demo_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
