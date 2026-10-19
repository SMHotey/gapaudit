"""Фикстура ``_env_isolation`` (AGENTS gotcha 33) — прямой тест её механики.

Обычные тесты доказывают косвенно: сьют зелёный, значит утечка ``GAPAUDIT_*``
из шелла не мешает. Здесь проверяется сама фикстура — что она действительно
удаляет ключи на входе и возвращает их на выходе. Без этого «изоляция» могла
бы быть пустым генератором, и никто бы не заметил.

Генератор фикстуры берётся через ``__wrapped__``: pytest прячет исходную
функцию за декоратором, а нам нужен именно её код, а не обвязка с scope.
Второй ``next()`` доводит генератор до конца — именно тогда срабатывает
восстановление окружения.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import _env_isolation


def test_fixture_strips_environment_keys_before_yield() -> None:
    """Ключ, выставленный «в шелле», на входе в тест исчезает из окружения."""
    os.environ["GAPAUDIT_FAKE_HTTP"] = "1"
    generator = _env_isolation.__wrapped__()  # type: ignore[attr-defined]
    try:
        next(generator)  # дошли до yield — тело фикстуры уже отработало
        assert "GAPAUDIT_FAKE_HTTP" not in os.environ
    finally:
        os.environ.pop("GAPAUDIT_FAKE_HTTP", None)


def test_fixture_restores_environment_after_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """На выходе окружение возвращается байт-в-байт: что было, то и стало."""
    monkeypatch.setenv("GAPAUDIT_FAKE_HTTP", "1")
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", "C:/tmp/from-shell")
    monkeypatch.setenv("GAPAUDIT_MAX_JOBS", "3")
    before = dict(os.environ)

    generator = _env_isolation.__wrapped__()  # type: ignore[attr-defined]
    next(generator)
    assert "GAPAUDIT_DATA_DIR" not in os.environ
    assert "GAPAUDIT_MAX_JOBS" not in os.environ

    with pytest.raises(StopIteration):
        next(generator)  # хвост фикстуры: pop всех ключей + update(saved)

    assert dict(os.environ) == before
