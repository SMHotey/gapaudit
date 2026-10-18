"""PyPI, npm и статистика скачиваний — тонкий доменный слой над core.http (шаг 1b).

Порт legacy-функций ``pypi()``/``npm()``/``adoption()`` из ``check_predecessors.py``:
те же эндпоинты, те же поля результата, но без глобалов и без кэша на уровне модуля —
кэш и троттлинг живут в :class:`core.http.HttpClient`, который сюда инжектируется.

Разбор ответа вынесен в чистые функции ``*_raw``: они не ходят в сеть и принимают уже
разобранный JSON, поэтому тесты работают на фикстурах, а не на сети.

Важно про «нет данных»: ``adoption()`` возвращает ``None``, а не ``0``. «Скачиваний нет»
и «данных нет» — разные вердикты в UI, и подставлять ноль нельзя.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from core.http import HttpClient

__all__ = [
    "PRED_KEYWORDS",
    "RegistryClient",
    "adoption_raw",
    "npm_downloads_url",
    "npm_raw",
    "npm_url",
    "pypi_raw",
    "pypi_url",
    "pypistats_url",
]

#: Ключевые слова «есть MCP-поверхность» — как в legacy check_predecessors.
PRED_KEYWORDS = ("mcp", "model context protocol")

PYPI_API = "https://pypi.org/pypi/{name}/json"
NPM_API = "https://registry.npmjs.org/{name}"
PYPISTATS_API = "https://pypistats.org/api/packages/{name}/recent"
NPM_DOWNLOADS_API = "https://api.npmjs.org/downloads/point/last-week/{name}"

SUMMARY_LIMIT = 70
HOME_LIMIT = 80
DATE_LIMIT = 10


def pypi_url(name: str) -> str:
    return PYPI_API.format(name=urllib.parse.quote(name))


def npm_url(name: str) -> str:
    return NPM_API.format(name=urllib.parse.quote(name, safe="@/"))


def pypistats_url(name: str) -> str:
    return PYPISTATS_API.format(name=urllib.parse.quote(name))


def npm_downloads_url(name: str) -> str:
    return NPM_DOWNLOADS_API.format(name=urllib.parse.quote(name, safe="@/"))


def _text(value: Any, limit: int) -> str:
    """Строка для таблицы: None -> "", обрезка до ``limit`` символов."""
    return str(value or "")[:limit]


def _has_keyword(*parts: Any) -> bool:
    haystack = " ".join(str(part or "") for part in parts).lower()
    return any(keyword in haystack for keyword in PRED_KEYWORDS)


def pypi_raw(pypi_json: dict | None) -> dict | None:
    """Разобрать JSON PyPI в форму legacy ``pypi()`` (без поля ``raw``)."""
    if not isinstance(pypi_json, dict) or not pypi_json:
        return None
    info = pypi_json.get("info") or {}
    if not isinstance(info, dict):
        info = {}
    version = info.get("version") or "?"
    releases = (pypi_json.get("releases") or {}).get(version) or []
    first = releases[0] if isinstance(releases, list) and releases else None
    upload_time = first.get("upload_time") if isinstance(first, dict) else None
    return {
        "ver": version,
        # В legacy здесь был бы IndexError на пустом releases — теперь "?".
        "date": _text(upload_time, DATE_LIMIT) if upload_time else "?",
        "summary": _text(info.get("summary"), SUMMARY_LIMIT),
        "has_mcp": _has_keyword(info.get("summary"), info.get("description")),
        "home": _text(info.get("home_page"), HOME_LIMIT),
    }


def npm_raw(npm_json: dict | None) -> dict | None:
    """Разобрать JSON реестра npm в форму legacy ``npm()`` (без поля ``raw``)."""
    if not isinstance(npm_json, dict) or not npm_json or "error" in npm_json:
        return None
    latest = (npm_json.get("dist-tags") or {}).get("latest") or "?"
    versions = npm_json.get("versions") or {}
    vmeta = versions.get(latest, {}) if isinstance(versions, dict) else {}
    if not isinstance(vmeta, dict):
        vmeta = {}
    released = (npm_json.get("time") or {}).get(latest)
    return {
        "ver": latest,
        "date": _text(released, DATE_LIMIT) if released else "?",
        "summary": _text(vmeta.get("description"), SUMMARY_LIMIT),
        # legacy проверяет всю метаданные версии целиком, а не только описание
        "has_mcp": _has_keyword(json.dumps(vmeta)),
        "home": _text(vmeta.get("repository") or vmeta.get("homepage"), HOME_LIMIT),
    }


def adoption_raw(
    pypistats_json: dict | None,
    npm_last_week: int | None,
) -> tuple[int | None, int | None]:
    """(last_month, last_week) из pypistats, иначе из npm (месяц = неделя × 4).

    Как в legacy: если pypistats ответил документом, к npm не идём даже когда в нём
    нет полей — «ответ есть, данных нет» отличается от «ответа нет».
    """
    if pypistats_json:
        data = pypistats_json.get("data") or {}
        if isinstance(data, dict):
            return data.get("last_month"), data.get("last_week")
    if npm_last_week is not None:
        return npm_last_week * 4, npm_last_week
    return None, None


class RegistryClient:
    """Запросы к реестрам через инжектированный :class:`HttpClient`."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def pypi(self, name: str) -> dict | None:
        """Карточка пакета PyPI: ``{"ver","date","summary","has_mcp","home","raw"}``.

        ``None`` — если пакета нет (404) или сеть не ответила после всех попыток.
        """
        status, data = self.http.get(pypi_url(name))
        if status != 200 or not isinstance(data, dict):
            return None
        parsed = pypi_raw(data)
        if parsed is None:
            return None
        return {**parsed, "raw": data}

    def npm(self, name: str) -> dict | None:
        """Карточка пакета npm в той же форме, что ``pypi()``. ``None`` — нет пакета."""
        status, data = self.http.get(npm_url(name))
        if status != 200 or not isinstance(data, dict):
            return None
        parsed = npm_raw(data)
        if parsed is None:
            return None
        return {**parsed, "raw": data}

    def adoption(self, name: str) -> tuple[int | None, int | None]:
        """(за месяц, за неделю): сначала pypistats, при отсутствии — npm last-week."""
        status, data = self.http.get(pypistats_url(name))
        if status == 200 and isinstance(data, dict) and data:
            return adoption_raw(data, None)

        status, data = self.http.get(npm_downloads_url(name))
        if status == 200 and isinstance(data, dict) and data:
            downloads = data.get("downloads")
            return adoption_raw(None, downloads if isinstance(downloads, int) else None)
        return None, None
