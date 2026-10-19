"""Тесты core/settings.py — дефолты, файл, окружение, валидация (шаг 1e).

Сети нет; файлы пишутся только в ``tmp_path``. Импорт модуля ничего не читает и
не создаёт — это отдельно проверяется последним тестом.
"""

from __future__ import annotations

import importlib
import json
import pathlib

import core.settings as settings_module
from core.diffing import VerdictConfig
from core.settings import (
    DATA_DIR_NAME,
    ENV_CACHE_TTL_H,
    ENV_DATA_DIR,
    ENV_KEYWORDS,
    ENV_MAX_JOBS,
    SETTINGS_FILENAME,
    Settings,
    from_env,
    load,
    resolve_data_dir,
    save,
    to_dict,
)


def write(path: pathlib.Path, payload: str) -> pathlib.Path:
    path.write_text(payload, encoding="utf-8")
    return path


# --- дефолты и мост к VerdictConfig --------------------------------------------------------


def test_defaults_are_legacy_values() -> None:
    settings = Settings()

    assert settings.alive_weekly == 100
    assert settings.alive_monthly == 400
    assert settings.dead_weekly == 10
    assert settings.dead_monthly == 40
    assert settings.stale_days == 540
    assert settings.adoption_delta_pct == 25.0
    assert settings.keywords == ("mcp", "model context protocol")
    assert settings.data_dir == ""
    assert settings.cache_ttl_hours == 12.0
    assert settings.max_parallel_jobs == 2
    assert settings.registry_limit == 100
    assert settings.language == "ru"
    assert settings.load_error is None


def test_to_verdict_config_returns_verdict_config_with_same_thresholds() -> None:
    config = Settings().to_verdict_config()

    assert isinstance(config, VerdictConfig)
    assert config == VerdictConfig()
    assert config.alive_weekly == 100
    assert config.adoption_delta_pct == 25.0


def test_to_verdict_config_passes_changed_thresholds() -> None:
    config = Settings(stale_days=100, alive_weekly=7, dead_monthly=3, adoption_delta_pct=5.0).to_verdict_config()

    assert config.stale_days == 100
    assert config.alive_weekly == 7
    assert config.dead_monthly == 3
    assert config.adoption_delta_pct == 5.0
    # Остальное — дефолты VerdictConfig, не что-то иное.
    assert config.alive_monthly == VerdictConfig().alive_monthly
    assert config.dead_weekly == VerdictConfig().dead_weekly


# --- save / load ---------------------------------------------------------------------------


def test_save_then_load_round_trip(tmp_path: pathlib.Path) -> None:
    path = tmp_path / SETTINGS_FILENAME
    original = Settings(
        alive_weekly=50,
        keywords=("mcp", "агент"),
        data_dir=str(tmp_path / "data"),
        cache_ttl_hours=1.5,
        max_parallel_jobs=4,
        language="en",
    )

    save(original, path)
    restored = load(path)

    assert restored == original
    assert restored.keywords == ("mcp", "агент")
    assert restored.data_dir == str(tmp_path / "data")
    assert restored.cache_ttl_hours == 1.5
    assert restored.load_error is None
    for field_name in to_dict(original):
        assert getattr(restored, field_name) == getattr(original, field_name), field_name


def test_save_writes_sorted_keys_and_keeps_cyrillic(tmp_path: pathlib.Path) -> None:
    path = tmp_path / SETTINGS_FILENAME

    save(Settings(keywords=("mcp", "агент")), path)

    text = path.read_text(encoding="utf-8")
    assert "агент" in text  # ensure_ascii=False
    assert json.loads(text)["keywords"] == ["mcp", "агент"]  # кортеж -> список
    keys = list(json.loads(text))
    assert keys == sorted(keys)  # sort_keys=True: файл стабилен между прогонами


def test_save_creates_parent_directories(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "sub" / "nested" / SETTINGS_FILENAME

    save(Settings(alive_weekly=11), path)

    assert path.is_file()
    assert load(path).alive_weekly == 11


def test_save_leaves_no_tmp_files(tmp_path: pathlib.Path) -> None:
    save(Settings(), tmp_path / SETTINGS_FILENAME)

    assert [item.name for item in tmp_path.iterdir()] == [SETTINGS_FILENAME]


def test_load_missing_file_returns_defaults(tmp_path: pathlib.Path) -> None:
    settings = load(tmp_path / "нет-такого-файла.json")

    assert settings == Settings()
    assert settings.load_error is None


def test_load_broken_json_returns_defaults_with_error(tmp_path: pathlib.Path) -> None:
    path = write(tmp_path / SETTINGS_FILENAME, '{"alive_weekly": 50,')

    settings = load(path)

    # Настройки — дефолтные (сравниваем без load_error: это поле-состояние).
    assert to_dict(settings) == to_dict(Settings())
    assert settings.load_error is not None
    assert "JSON" in settings.load_error


def test_load_non_object_json_returns_defaults_with_error(tmp_path: pathlib.Path) -> None:
    path = write(tmp_path / SETTINGS_FILENAME, "[1, 2, 3]")

    settings = load(path)

    assert to_dict(settings) == to_dict(Settings())
    assert settings.load_error is not None


def test_load_partial_json_fills_rest_with_defaults(tmp_path: pathlib.Path) -> None:
    path = write(tmp_path / SETTINGS_FILENAME, '{"alive_weekly": 50}')

    settings = load(path)

    assert settings == Settings(alive_weekly=50)
    assert settings.dead_weekly == 10 and settings.registry_limit == 100


def test_load_ignores_unknown_fields(tmp_path: pathlib.Path) -> None:
    """Ключи из будущих версий не ломают загрузку и не попадают в настройки."""
    path = write(tmp_path / SETTINGS_FILENAME, '{"unknown_field": 1, "alive_weekly": 50, "schedules": []}')

    settings = load(path)

    assert settings.alive_weekly == 50
    assert settings.load_error is None
    assert not hasattr(settings, "unknown_field")


def test_load_wrong_type_falls_back_to_default_and_reports(tmp_path: pathlib.Path) -> None:
    """Строка вместо числа — дефолт поля и причина в load_error, но не исключение."""
    path = write(
        tmp_path / SETTINGS_FILENAME,
        '{"alive_weekly": "сто", "cache_ttl_hours": true, "keywords": "mcp", "language": "ru"}',
    )

    settings = load(path)

    assert settings.alive_weekly == 100
    assert settings.cache_ttl_hours == 12.0
    assert settings.keywords == ("mcp", "model context protocol")
    assert settings.language == "ru"  # валидное поле всё равно применилось
    assert settings.load_error is not None
    assert "alive_weekly" in settings.load_error and "keywords" in settings.load_error


def test_load_error_is_not_written_to_file(tmp_path: pathlib.Path) -> None:
    """load_error — состояние загрузки, а не настройка: в JSON его нет."""
    path = write(tmp_path / SETTINGS_FILENAME, "{}")
    settings = load(path)

    save(settings, path)

    assert "load_error" not in json.loads(path.read_text(encoding="utf-8"))


def test_from_env_data_dir() -> None:
    assert from_env({ENV_DATA_DIR: "/tmp/x"}).data_dir == "/tmp/x"
    assert from_env({ENV_DATA_DIR: "   "}).data_dir == ""


def test_from_env_keywords() -> None:
    assert from_env({ENV_KEYWORDS: "foo,bar"}).keywords == ("foo", "bar")
    assert from_env({ENV_KEYWORDS: " foo , bar , "}).keywords == ("foo", "bar")
    assert from_env({ENV_KEYWORDS: ""}).keywords == Settings().keywords


def test_from_env_ttl_and_jobs() -> None:
    env = {ENV_CACHE_TTL_H: "0.5", ENV_MAX_JOBS: "3"}

    settings = from_env(env)

    assert settings.cache_ttl_hours == 0.5
    assert settings.max_parallel_jobs == 3


def test_from_env_ignores_bad_values_instead_of_failing() -> None:
    """Опечатка в переменной окружения не валит прогон: поле остаётся дефолтным."""
    env = {ENV_MAX_JOBS: "not-int", ENV_CACHE_TTL_H: "много"}

    settings = from_env(env)

    assert settings.max_parallel_jobs == 2
    assert settings.cache_ttl_hours == 12.0
    assert settings.load_error is None


def test_from_env_empty_and_foreign_vars_give_defaults() -> None:
    assert from_env({}) == Settings()
    assert from_env({"GAPAUDIT_SOMETHING_ELSE": "1", "PATH": "/usr/bin"}).__dict__ == Settings().__dict__


# --- каталог данных ------------------------------------------------------------------------


def test_resolve_data_dir_default_ends_with_gapaudit(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    resolved = resolve_data_dir(Settings())

    assert resolved.name == DATA_DIR_NAME
    assert resolved == tmp_path / DATA_DIR_NAME
    # Каталог не создаётся: это делает save() (или хранилище шага 2).
    assert not resolved.exists()


def test_resolve_data_dir_from_settings(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "custom"

    assert resolve_data_dir(Settings(data_dir=str(target))) == target
    assert resolve_data_dir(Settings(data_dir="/tmp/x")) == pathlib.Path("/tmp/x")


def test_resolve_data_dir_expands_home(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert resolve_data_dir(Settings(data_dir="~/GapAudit")) == tmp_path / "GapAudit"


# --- чистота импорта -------------------------------------------------------------------------


def test_import_creates_no_files_and_does_not_read_env(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Импорт модуля — без побочных эффектов: ни файлов, ни окружения."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "env-dir"))
    monkeypatch.setenv(ENV_MAX_JOBS, "9")

    importlib.reload(settings_module)

    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path / "env-dir").exists()
    assert settings_module.Settings().data_dir == ""  # конструктор окружение не читает
    assert settings_module.Settings().max_parallel_jobs == 2


def test_from_env_zero_jobs_and_negative_ttl_keep_defaults() -> None:
    """Ноль параллельных задач смысла не имеет, TTL отрицательным быть не может."""
    assert from_env({ENV_MAX_JOBS: "0"}).max_parallel_jobs == 2
    assert from_env({ENV_CACHE_TTL_H: "-1"}).cache_ttl_hours == 12.0


def test_to_dict_excludes_load_error() -> None:
    payload = to_dict(Settings(load_error="что-то"))

    assert "load_error" not in payload
    assert payload["keywords"] == ["mcp", "model context protocol"]
    assert payload["alive_weekly"] == 100


def test_round_trip_through_default_data_dir(tmp_path: pathlib.Path, monkeypatch) -> None:
    """Канонический путь настроек: каталог данных + SETTINGS_FILENAME (им создаёт save)."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = resolve_data_dir(Settings()) / SETTINGS_FILENAME

    save(Settings(registry_limit=25), path)

    assert path == tmp_path / DATA_DIR_NAME / SETTINGS_FILENAME
    assert load(path).registry_limit == 25
