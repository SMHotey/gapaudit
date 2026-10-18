"""GitHub через CLI ``gh`` — подпроцессами (шаг 1b).

Порт legacy ``verify.search()``/``org_candidates()``/``relevant()``/``age_days()``
и ``collect.gh_search()``. Отличия от legacy: никаких глобалов и ``print``, лимиты
соблюдает общий :class:`core.http.Throttle`, а сам запуск ``gh`` инжектируется
(``GhRunner``), поэтому тесты не ходят в сеть.

``gh`` авторизован у пользователя: лимит 5000 запросов/час вместо 60 у анонимного API,
поэтому ``search_repositories`` не спит между страницами — паузу держит Throttle
(Search API — 30 запросов/мин, остальные эндпоинты — 60).

Ничего не печатает: диагностику (текст ошибки gh) отдаёт вызывающему в виде
``(None, [])``/``[]``, а печатает её CLI-обёртка на шаге 1e.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
from typing import Any, Callable

from core.http import DEFAULT_PER_MINUTE, Throttle, default_throttle

__all__ = [
    "GITHUB_HOST",
    "GITHUB_SEARCH_PER_MINUTE",
    "GhClient",
    "GhRunner",
    "age_days",
    "default_runner",
    "org_candidates",
    "relevant_items",
]

#: (argv) -> (returncode, stdout, stderr). argv — аргументы ПОСЛЕ ``gh``.
GhRunner = Callable[[list[str]], tuple[int, str, str]]

GITHUB_HOST = "api.github.com"
GITHUB_SEARCH_PER_MINUTE = 30
NO_DATA_AGE_DAYS = 9999
DEFAULT_GH_TIMEOUT = 45.0
GH_MISSING_CODE = 127
GH_TIMEOUT_CODE = 124


def default_runner(argv: list[str], timeout: float = DEFAULT_GH_TIMEOUT) -> tuple[int, str, str]:
    """Запустить ``gh`` с аргументами ``argv`` и вернуть (код, stdout, stderr).

    Отсутствие ``gh`` и таймаут — не исключения, а коды 127 и 124: вызывающему коду
    нужно отличать «нет gh» от «gh сказал нет». Остальные исключения пробрасываются.
    """
    try:
        proc = subprocess.run(
            ["gh", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except FileNotFoundError:
        return GH_MISSING_CODE, "", "gh не найден в PATH — установите и авторизуйте gh CLI"
    except subprocess.TimeoutExpired:
        return GH_TIMEOUT_CODE, "", f"gh не ответил за {timeout:.0f} с: {' '.join(argv[:3])}…"
    return int(proc.returncode), proc.stdout or "", proc.stderr or ""


def age_days(pushed_iso: str | None) -> int:
    """Сколько дней с последнего push. Пусто или мусор — ``9999`` (как в legacy)."""
    if not pushed_iso:
        return NO_DATA_AGE_DAYS
    try:
        moment = datetime.datetime.fromisoformat(str(pushed_iso).replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - moment).days
    except Exception:
        return NO_DATA_AGE_DAYS


def relevant_items(
    items: list[dict],
    needle: str,
    keyword: str,
    exclude_full_name: str | None = None,
) -> list[dict]:
    """Оставить находки, где ``needle`` (по границе слова) И ``keyword`` — порт legacy.

    ``needle`` ищется по границе слова ``[a-z0-9]``: без этого «uv» находится внутри
    «kubernetes» и вердикт «ниша закрыта» становится ложным. Порядок: сначала находки
    с ``keyword`` в имени репозитория (свой сервер важнее чужого), затем по звёздам.
    """
    pattern = r"(?<![a-z0-9])" + re.escape(str(needle).lower()) + r"(?![a-z0-9])"
    keyword_lower = str(keyword).lower()
    excluded = (exclude_full_name or "").lower()

    found: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        full_name = str(item.get("full_name") or "")
        # сам инструмент не является сервером для себя — иначе ложный «ЖИВОЙ»
        if excluded and full_name.lower() == excluded:
            continue
        haystack = (full_name + " " + str(item.get("description") or "")).lower()
        if re.search(pattern, haystack) and keyword_lower in haystack:
            found.append(item)

    found.sort(
        key=lambda item: (
            keyword_lower in str(item.get("full_name") or "").lower(),
            item.get("stargazers_count") or 0,
        ),
        reverse=True,
    )
    return found


def _slim_repo(repo: dict) -> dict:
    """Минимальная форма репозитория — остальное из ответа gh не нужно."""
    return {
        "full_name": repo.get("full_name"),
        "description": repo.get("description"),
        "stargazers_count": repo.get("stargazers_count") or 0,
        "pushed_at": repo.get("pushed_at"),
        "html_url": repo.get("html_url"),
    }


class GhClient:
    """Обёртки над ``gh api``: поиск репозиториев, репозитории организации, auth."""

    def __init__(
        self,
        runner: GhRunner = default_runner,
        search_per_minute: int = GITHUB_SEARCH_PER_MINUTE,
        throttle: Throttle | None = None,
    ) -> None:
        self.runner = runner
        self.search_per_minute = int(search_per_minute)
        #: Свой throttle — если вызывающий не передал общий (например, с gh-очередью P1).
        self.throttle = throttle if throttle is not None else default_throttle()

    # --- внутреннее ---------------------------------------------------------

    def _wait(self, per_minute: int) -> float:
        """Дождаться лимита для api.github.com, выставив нужную скорость.

        Search и остальные эндпоинты живут на одном хосте, поэтому троттл не может
        различить их по URL: скорость переключает вызывающий код перед каждым вызовом.
        """
        self.throttle.set_rate(GITHUB_HOST, per_minute)
        return self.throttle.wait(GITHUB_HOST)

    def _api_json(self, endpoint: str, params: list[tuple[str, Any]], per_minute: int) -> Any | None:
        """``gh api -X GET <endpoint> -f k=v`` -> разобранный JSON или None при ошибке."""
        argv = ["api", "-X", "GET", endpoint]
        for key, value in params:
            argv += ["-f", f"{key}={value}"]
        self._wait(per_minute)
        code, stdout, _stderr = self.runner(argv)
        if code != 0:
            return None
        try:
            return json.loads(stdout)
        except ValueError:
            return None

    # --- контракт -----------------------------------------------------------

    def search_repositories(
        self,
        q: str,
        *,
        per_page: int = 100,
        sort: str = "stars",
        order: str = "desc",
        page: int = 1,
    ) -> tuple[int | None, list[dict]]:
        """Search API: ``(total_count, items)``. Ошибка gh — ``(None, [])``."""
        params = [
            ("q", q),
            ("sort", sort),
            ("order", order),
            ("per_page", per_page),
            ("page", page),
        ]
        data = self._api_json("search/repositories", params, self.search_per_minute)
        if not isinstance(data, dict):
            return None, []
        items = data.get("items") or []
        if not isinstance(items, list):
            return None, []
        return data.get("total_count", 0), items

    def org_repos(
        self,
        owner: str,
        *,
        per_page: int = 100,
        sort: str = "updated",
    ) -> list[dict]:
        """Репозитории организации (fallback — пользователя). Ошибка — ``[]``.

        У крупных проектов репозиторий часто назван по назначению, а не по продукту
        (``grafana/helm-charts``, ``Dokploy/mcp``) — поиск по имени продукта их теряет,
        поэтому список организации нужен отдельно.
        """
        params: list[tuple[str, Any]] = [("per_page", per_page), ("sort", sort)]
        for endpoint in (f"orgs/{owner}/repos", f"users/{owner}/repos"):
            data = self._api_json(endpoint, params, DEFAULT_PER_MINUTE)
            if not isinstance(data, list):
                continue
            return [_slim_repo(repo) for repo in data if isinstance(repo, dict)]
        return []

    def auth_status(self) -> tuple[bool, str]:
        """``gh auth status``: (авторизован, текст). Без gh — ``(False, "...")``."""
        code, stdout, stderr = self.runner(["auth", "status"])
        text = ((stdout or "") + (stderr or "")).strip()
        return code == 0, text


def org_candidates(
    org: str,
    keyword: str,
    runner: GhRunner = default_runner,
    *,
    exclude_full_name: str | None = None,
) -> list[dict]:
    """Репозитории организации, где ``keyword`` есть в имени или описании.

    Порт legacy ``verify.org_candidates()``: там же отбрасывался сам инструмент
    (``full_name`` начинался с ``<target>/``) — здесь это ``exclude_full_name``.
    """
    repos = GhClient(runner=runner).org_repos(org)
    keyword_lower = str(keyword).lower()
    excluded = (exclude_full_name or "").lower()

    found: list[dict] = []
    for repo in repos:
        full_name = str(repo.get("full_name") or "")
        if excluded and full_name.lower() == excluded:
            continue
        haystack = (full_name + " " + str(repo.get("description") or "")).lower()
        if keyword_lower in haystack:
            found.append(repo)

    found.sort(
        key=lambda repo: (
            keyword_lower in str(repo.get("full_name") or "").lower(),
            repo.get("stargazers_count") or 0,
        ),
        reverse=True,
    )
    return found
