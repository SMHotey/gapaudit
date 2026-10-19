"""Конфиг приложения и CLI: один dataclass, JSON-файл, env-перекрытие (шаг 1e).

Зачем отдельный модуль: пороги вердикта, признаки ниши, каталог данных и лимиты
задач нужны сразу трём потребителям — CLI-обёрткам (шаг 1f), очереди задач (P1)
и экрану настроек (P4). В аргументах командной строки им тесно: UI меняет их мышью,
а CLI читает из файла.

:class:`Settings` — это «всё вместе»: он собирает пороги, которые дальше уезжают
в :class:`core.diffing.VerdictConfig` (:meth:`Settings.to_verdict_config`). Сам
``VerdictConfig`` остаётся отдельным объектом: его знает ``core.packages`` и
``core.diffing``, и менять там форму нельзя.

Правила шага (как у остальных модулей ``core``):

* импорт не читает файлы, не смотрит в окружение и не создаёт каталогов — это
  делают явные :func:`load`, :func:`save`, :func:`from_env`;
* модуль ничего не печатает: проблема с файлом возвращается в поле
  ``Settings.load_error`` (CLI и UI есть что показать), а не в stdout;
* неизвестные ключи JSON игнорируются, отсутствующие заполняются дефолтами —
  файл, записанный более новой версией, не должен валить старую;
* значение не того типа не роняет загрузку: поле остаётся дефолтным, причина
  уходит в ``load_error`` (лучше работать на дефолте, чем не запуститься).

``load_error`` — состояние загрузки, а не настройка: в JSON он не пишется, иначе
«битый файл» впечатался бы в файл навсегда.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # только для аннотаций: на импорте core.diffing не нужен
    from core.diffing import VerdictConfig

__all__ = [
    "DATA_DIR_NAME",
    "ENV_CACHE_TTL_H",
    "ENV_DATA_DIR",
    "ENV_KEYWORDS",
    "ENV_MAX_JOBS",
    "SETTINGS_FILENAME",
    "Settings",
    "from_env",
    "load",
    "resolve_data_dir",
    "save",
    "to_dict",
]

#: Каталог данных по умолчанию — рядом с пользователем (DEVPLAN §12, п. 6).
DATA_DIR_NAME = "GapAudit"
#: Имя файла настроек внутри каталога данных.
SETTINGS_FILENAME = "settings.json"

ENV_DATA_DIR = "GAPAUDIT_DATA_DIR"
ENV_CACHE_TTL_H = "GAPAUDIT_CACHE_TTL_H"
ENV_KEYWORDS = "GAPAUDIT_KEYWORDS"
ENV_MAX_JOBS = "GAPAUDIT_MAX_JOBS"

#: Поле-состояние, а не настройка: в JSON не пишется и из JSON не читается.
_RUNTIME_FIELDS = ("load_error",)


@dataclass
class Settings:
    """Настройки приложения: пороги, признаки, данные, задачи, UI.

    Дефолты — значения legacy-скриптов (пороги :class:`VerdictConfig`, классы
    каталога), поэтому «пустой конфиг» ведёт себя как привычный CLI.
    """

    # --- пороги вердикта (уезжают в VerdictConfig) ---
    alive_weekly: int = 100
    alive_monthly: int = 400
    dead_weekly: int = 10
    dead_monthly: int = 40
    stale_days: int = 540
    adoption_delta_pct: float = 25.0

    # --- признаки ниши (подстроки в имени/описании) ---
    keywords: tuple[str, ...] = ("mcp", "model context protocol")

    # --- данные и кэш ---
    data_dir: str = ""  # пусто — каталог по умолчанию, см. resolve_data_dir()
    cache_ttl_hours: float = 12.0

    # --- задачи ---
    max_parallel_jobs: int = 2

    # --- реестр MCP ---
    registry_limit: int = 100

    # --- UI ---
    language: str = "ru"  # ru | en (en ещё не реализован, но поле есть)

    # --- состояние загрузки (в файл не пишется) ---
    load_error: str | None = None

    def to_verdict_config(self) -> VerdictConfig:
        """Пороги вердикта отдельным объектом для ``core.diffing``/``core.packages``.

        Пороги живут и здесь, и в :class:`VerdictConfig`, и это единственное место,
        где они связываются: CLI и UI читают/пишут один файл настроек, а метод
        отдаёт из него то, что понимают чистые функции вердикта.
        """
        from core.diffing import VerdictConfig  # ленивый импорт: без цикла на импорте

        return VerdictConfig(
            alive_weekly=self.alive_weekly,
            alive_monthly=self.alive_monthly,
            dead_weekly=self.dead_weekly,
            dead_monthly=self.dead_monthly,
            stale_days=self.stale_days,
            adoption_delta_pct=self.adoption_delta_pct,
        )


def resolve_data_dir(settings: Settings) -> Path:
    """Каталог данных: из настроек, иначе ``~/GapAudit`` рядом с пользователем.

    Каталог НЕ создаётся: его создаёт :func:`save` (или слой хранилища шага 2),
    иначе обычное чтение настроек оставляло бы мусор на диске.
    """
    configured = (settings.data_dir or "").strip()
    if configured:
        return Path(configured).expanduser()
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    return Path(home) / DATA_DIR_NAME


def to_dict(settings: Settings) -> dict[str, Any]:
    """Настройки словарём для JSON: кортеж ключевых слов становится списком."""
    payload = asdict(settings)
    for name in _RUNTIME_FIELDS:
        payload.pop(name, None)
    payload["keywords"] = list(settings.keywords)
    return payload


def save(settings: Settings, path: str | Path) -> None:
    """Записать настройки в JSON: создаёт родительский каталог, пишет атомарно.

    Атомарность (tmp-файл рядом + ``os.replace``) — как у кэша: настройки читает
    и UI, и подпроцесс задачи, и половина файла на диске означала бы «битый JSON»
    у одного из них. Формат: ``indent=2``, ``sort_keys=True`` (диффы в git читаемые),
    ``ensure_ascii=False`` (кириллица в ключевых словах — как есть).
    """
    target = Path(path)
    directory = target.parent if str(target.parent) else Path(".")
    directory.mkdir(parents=True, exist_ok=True)

    handle_fd, tmp_path = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(to_dict(settings), handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load(path: str | Path) -> Settings:
    """Прочитать настройки; отсутствие файла и мусор — дефолты, а не исключение.

    Отсутствующий файл — нормальная ситуация первого запуска (``Settings()``).
    Битый JSON, чужой корень (не объект) и значения не того типа дают ``Settings()``
    с причиной в ``load_error``: CLI/UI покажет предупреждение, но запустится.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as error:
        return Settings(load_error=f"не удалось прочитать {target}: {error}")

    try:
        payload = json.loads(text)
    except ValueError as error:
        return Settings(load_error=f"битый JSON в {target}: {error}")
    if not isinstance(payload, dict):
        return Settings(load_error=f"{target}: ожидался объект JSON, пришло {type(payload).__name__}")

    return _from_payload(payload)


def from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Настройки из окружения — перекрытие для demo и CI.

    Понимает четыре переменные: ``GAPAUDIT_DATA_DIR`` — каталог данных,
    ``GAPAUDIT_CACHE_TTL_H`` — TTL кэша в часах, ``GAPAUDIT_KEYWORDS`` — признаки
    ниши через запятую, ``GAPAUDIT_MAX_JOBS`` — параллелизм очереди. Остальные
    ``GAPAUDIT_*`` игнорируются.

    Непонятное значение — не ошибка: поле остаётся дефолтным (опечатка в переменной
    окружения не должна валить прогон в CI). ``env`` можно передать словарём —
    тесты и demo так обходятся без правки окружения процесса.

    Возвращаются только дефолты плюс то, что реально задано в окружении: настройки
    из файла к этому результату применяет вызывающий (CLI 1f, P1) — порядок
    «файл, затем окружение» задан там, где видны оба источника.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    settings = Settings()

    data_dir = source.get(ENV_DATA_DIR)
    if isinstance(data_dir, str) and data_dir.strip():
        settings.data_dir = data_dir.strip()

    ttl = _env_float(source.get(ENV_CACHE_TTL_H))
    if ttl is not None and ttl >= 0:
        settings.cache_ttl_hours = ttl

    keywords = source.get(ENV_KEYWORDS)
    if isinstance(keywords, str):
        words = tuple(word.strip() for word in keywords.split(",") if word.strip())
        if words:
            settings.keywords = words

    jobs = _env_int(source.get(ENV_MAX_JOBS))
    if jobs is not None and jobs > 0:
        settings.max_parallel_jobs = jobs

    return settings


# --- внутреннее ---------------------------------------------------------------


def _from_payload(payload: dict[str, Any]) -> Settings:
    """Собрать настройки из словаря JSON: известные поля, остальное — дефолты."""
    settings = Settings()
    problems: list[str] = []

    for spec in fields(Settings):
        name = spec.name
        if name in _RUNTIME_FIELDS or name not in payload:
            continue
        if name == "keywords":
            words = _as_keywords(payload[name])
            if words is None:
                problems.append(f"{name}: ожидался список строк")
            else:
                settings.keywords = words
            continue
        value, ok = _coerce(spec.default, payload[name])
        if ok:
            setattr(settings, name, value)
        else:
            problems.append(f"{name}: ожидалось {_type_name(spec.default)}, пришло {payload[name]!r}")

    if problems:
        settings.load_error = "; ".join(problems)
    return settings


def _coerce(default: Any, value: Any) -> tuple[Any, bool]:
    """Привести значение из JSON к типу поля; ``(значение, получилось)``.

    Строгий разбор: ``"100"`` для числа — ошибка, а не «100». Типы из JSON и типы
    полей совпадают один-в-один, и любая подмена здесь означала бы, что файл
    настроек и код расходятся незаметно. ``bool`` числом не считается (``true``
    не должно становиться ``1``).
    """
    if isinstance(default, bool) or value is None:
        return default, False
    if isinstance(default, int):
        if isinstance(value, bool):
            return default, False
        if isinstance(value, int):
            return value, True
        if isinstance(value, float) and value.is_integer():
            return int(value), True
        return default, False
    if isinstance(default, float):
        if isinstance(value, bool):
            return default, False
        if isinstance(value, (int, float)):
            return float(value), True
        return default, False
    if isinstance(default, str):
        return (value, True) if isinstance(value, str) else (default, False)
    return default, False


def _as_keywords(value: Any) -> tuple[str, ...] | None:
    """Ключевые слова из JSON-списка; ``None`` — значение не подходит.

    Пустой список допустим: пользователь вправе не отмечать признаки ниши вообще
    (тогда слой «признак появился» просто ничего не покажет).
    """
    if isinstance(value, str):
        return None
    if not isinstance(value, (list, tuple)):
        return None
    words: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        if item:
            words.append(item)
    return tuple(words)


def _type_name(default: Any) -> str:
    return {int: "целое", float: "число", str: "строка"}.get(type(default), "значение")


def _env_int(text: str | None) -> int | None:
    """Целое из переменной окружения; мусор — ``None`` (поле остаётся дефолтным)."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _env_float(text: str | None) -> float | None:
    """Число из переменной окружения; мусор — ``None``."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return float(text.strip())
    except ValueError:
        return None
