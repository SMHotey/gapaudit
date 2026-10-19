"""Тесты ``app/session.py`` (шаг 3b): файл ``<data_dir>/session.token``.

Сеть — ноль; права проверяются только там, где ОС их реально хранит
(на Windows режим файла задаёт ACL, а не биты — см. skipif).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.session import TOKEN_FILE_NAME, read_token, remove_token, write_token

TOKEN = "s3ssion-token_abc-123"


def test_round_trip_and_parent_dirs_created(tmp_path: Path) -> None:
    data_dir = tmp_path / "nested" / "data"

    path = write_token(data_dir, TOKEN)

    assert path == data_dir / TOKEN_FILE_NAME
    assert path.is_file()
    assert read_token(data_dir) == TOKEN


def test_read_missing_returns_none(tmp_path: Path) -> None:
    assert read_token(tmp_path / "нет-такого") is None
    assert read_token(tmp_path) is None  # каталог есть, файла нет


def test_write_overwrites_previous_token(tmp_path: Path) -> None:
    write_token(tmp_path, "старый")
    write_token(tmp_path, "новый")

    assert read_token(tmp_path) == "новый"


def test_remove_is_idempotent(tmp_path: Path) -> None:
    write_token(tmp_path, TOKEN)

    remove_token(tmp_path)
    remove_token(tmp_path)  # повторный вызов — не исключение
    remove_token(tmp_path / "нет-такого")

    assert read_token(tmp_path) is None


def test_read_empty_file_returns_none(tmp_path: Path) -> None:
    path = write_token(tmp_path, TOKEN)
    path.write_text("   \n", encoding="utf-8")

    assert read_token(tmp_path) is None


@pytest.mark.skipif(os.name == "nt", reason="Windows: права задаёт icacls, не биты режима")
def test_write_token_sets_owner_only_mode(tmp_path: Path) -> None:
    path = write_token(tmp_path, TOKEN)

    assert os.stat(path).st_mode & 0o777 == 0o600
