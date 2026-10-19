"""Чистая логика ``desktop.py`` (шаг 3c) без окна и без бэкенда.

Три функции, ради которых весь ``desktop.py`` импортируется в тест:

* :func:`should_confirm_close` — спрашивать ли пользователя при закрытии;
* :func:`format_title` — заголовок «GapAudit — N задач» с русским склонением;
* :func:`is_e_abort` — «окно закрыто до инициализации WebView2» — не ошибка.

Плюс :func:`count_active`, которым считается и заголовок, и диалог закрытия.
Окно не создаётся ни в одном тесте (живая проверка окна — вручную и в
``tests/test_desktop_browser_smoke.py``), сеть не используется.
"""

from __future__ import annotations

import desktop


def test_should_confirm_close_without_jobs_is_false() -> None:
    assert desktop.should_confirm_close(0) is False


def test_should_confirm_close_with_jobs_is_true() -> None:
    assert desktop.should_confirm_close(1) is True
    assert desktop.should_confirm_close(5) is True


def test_format_title_without_active_jobs() -> None:
    assert desktop.format_title(0) == "GapAudit"
    assert desktop.format_title(-3) == "GapAudit"


def test_format_title_counts_active_jobs_in_russian() -> None:
    assert desktop.format_title(1) == "GapAudit — 1 задача"
    assert desktop.format_title(3) == "GapAudit — 3 задачи"
    assert desktop.format_title(5) == "GapAudit — 5 задач"
    # 11-14 не подчиняются правилу «1» и «2-4» — десятичная пара решает
    assert desktop.format_title(11) == "GapAudit — 11 задач"
    assert desktop.format_title(22) == "GapAudit — 22 задачи"


def test_is_e_abort_detects_webview2_abort() -> None:
    assert desktop.is_e_abort(Exception("E_ABORT")) is True
    assert desktop.is_e_abort(Exception("0x80004004")) is True
    assert desktop.is_e_abort(Exception("e_abort (0x80004004)")) is True


def test_is_e_abort_rejects_unrelated_errors() -> None:
    assert desktop.is_e_abort(Exception("other")) is False
    assert desktop.is_e_abort(Exception()) is False
    # «abort» без E_ перед ним — не E_ABORT: подстрока E_ABORT здесь не встречается
    assert desktop.is_e_abort(ValueError("operation aborted")) is False


def test_is_e_abort_accepts_hresult_code() -> None:
    class HResult(Exception):
        hresult = desktop.E_ABORT_HRESULT

    class SignedResult(Exception):
        hresult = desktop.E_ABORT_HRESULT_SIGNED

    assert desktop.is_e_abort(HResult()) is True
    assert desktop.is_e_abort(SignedResult()) is True


def test_count_active_counts_only_queued_and_running() -> None:
    jobs = [
        {"status": "running"},
        {"status": "queued"},
        {"status": "interrupted"},
        {"status": "done"},
        {"status": "failed"},
        {"status": "cancelled"},
    ]

    assert desktop.count_active(jobs) == 2
    assert desktop.count_active([]) == 0
