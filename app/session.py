"""Сессионный токен для внешних клиентов (шаг 3b): ``<data_dir>/session.token``.

Внешний клиент (Cursor/Claude, DEVPLAN §7 «Как клиенты находят GapAudit») не
участвует в запуске приложения и поэтому не видит токен, который ``desktop.py``
генерирует в памяти. Решение: тот же токен кладётся в файл ``session.token``
рядом с базой данных, а при закрытии окна файл удаляется — токен живёт ровно
столько, сколько живёт приложение.

Права: ``0600`` через :func:`os.chmod`; на Windows chmod не ограничивает
ACL, поэтому дополнительно гоняется
``icacls <file> /inheritance:r /grant:r <domain>\\<user>:F`` (без проверки кода
выхода: разные локали Windows отвечают по-разному, а нам важно только то, что
команда не упала с исключением). Если ``icacls`` в системе нет — одна строка
предупреждения в stderr, запись токена не прерывается.

Все три функции идемпотентны по отношению к отсутствию файла: внешний клиент
может читать токен в момент, когда приложение ещё не стартовало или уже
закрылось, — это не ошибка, а ``None``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

__all__ = ["TOKEN_FILE_NAME", "read_token", "remove_token", "write_token"]

#: Имя файла токена внутри каталога данных.
TOKEN_FILE_NAME = "session.token"


def write_token(data_dir: Path, token: str) -> Path:
    """Записать токен в ``<data_dir>/session.token`` с правами 0600.

    Родительские каталоги создаются (на первой машине каталога данных ещё
    может не быть). Возвращает путь к файлу.
    """
    path = Path(data_dir) / TOKEN_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o600)
    except OSError as error:
        print(
            f"[gapaudit] не удалось выставить права 0600 на {path}: {error}",
            file=sys.stderr,
        )
    if os.name == "nt":
        _restrict_windows_acl(path)
    return path


def read_token(data_dir: Path) -> str | None:
    """Прочитать токен; отсутствующий или пустой файл — ``None``."""
    path = Path(data_dir) / TOKEN_FILE_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        print(f"[gapaudit] не удалось прочитать {path}: {error}", file=sys.stderr)
        return None
    token = text.strip()
    return token or None


def remove_token(data_dir: Path) -> None:
    """Удалить файл токена. Идемпотентно: нет файла — тихо выходим."""
    path = Path(data_dir) / TOKEN_FILE_NAME
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        # Ошибка прав/блокировки: токен остался на диске — это стоит увидеть.
        print(f"[gapaudit] не удалось удалить {path}: {error}", file=sys.stderr)


# --- Windows ------------------------------------------------------------------


def _restrict_windows_acl(path: Path) -> None:
    """Снять наследование и отдать файл только владельцу (icacls).

    Код выхода не проверяем (см. docstring модуля); важны только исключения.
    """
    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    who = f"{domain}\\{user}" if domain and user else user
    if not who:
        print(
            "[gapaudit] предупреждение: имя пользователя не найдено — "
            f"права session.token не ограничены: {path}",
            file=sys.stderr,
        )
        return
    argv = ["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"]
    try:
        subprocess.run(  # noqa: S603 - argv из констант и имени пользователя
            argv, capture_output=True, timeout=20, check=False
        )
    except FileNotFoundError:
        print(
            f"[gapaudit] предупреждение: icacls не найден — права session.token "
            f"не ограничены: {path}",
            file=sys.stderr,
        )
    except OSError as error:
        print(
            f"[gapaudit] предупреждение: icacls не выполнен ({error}) — "
            f"права session.token не ограничены: {path}",
            file=sys.stderr,
        )
