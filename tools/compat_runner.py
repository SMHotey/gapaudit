"""Фейковый рантайм и раннер L2-уровня compat_check (шаг 1g).

Здесь живёт всё, чем L2 подменяет сеть в процессе new CLI: транспорт по карте
правил и фейковый ``gh`` по спецификации. ``tools.compat_check`` импортирует те же
классы для стороны legacy — подмена одинаковая для обеих сторон (DEVPLAN §11:
сеть выключена до шага 1h, фейковый транспорт инжектируется одинаково).

Запуск new CLI (так делает compat_check)::

    python -m tools.compat_runner <cli-модуль> [аргументы CLI...]

Ожидаемое окружение:

* ``GAPAUDIT_COMPAT_HTTP`` — JSON-файл со списком правил транспорта:
  ``[{"match": <подстрока URL>, "status": 200, "body": {...}, "side": "legacy"|"new"}]``;
  первое правило, чья ``match``-подстрока есть в URL, выигрывает, неизвестный URL — 404;
* ``GAPAUDIT_COMPAT_GH`` — JSON-файл спецификации gh:
  ``{"search": [{"match": <подстрока запроса>, "total": N, "items": [...], "side": ...}],
  "orgs": {"owner": [репозитории]}, "auth_ok": true}``; неизвестный вызов gh — код 1;
* ``GAPAUDIT_DATA_DIR`` — каталог данных (задаёт compat_check, чтобы не трогать
  каталог пользователя).

``side`` — асимметрия только для тестов гейта (симуляция расхождения): правило
с ``side`` отдаётся исключительно названной стороне.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any

__all__ = [
    "FakeTime",
    "MapTransport",
    "for_side",
    "gh_spec_for_side",
    "http_rules_for_side",
    "make_gh",
    "main",
]


class MapTransport:
    """Транспорт ``core.http`` по карте правил: первая подходящая подстрока выигрывает."""

    def __init__(self, rules: list[dict] | None) -> None:
        self.rules = list(rules or [])
        self.calls: list[str] = []

    def __call__(
        self, url: str, headers: dict[str, str] | None = None, timeout: float = 0.0
    ) -> tuple[int, bytes]:
        self.calls.append(url)
        for rule in self.rules:
            if str(rule.get("match") or "") in url:
                body = rule.get("body")
                return int(rule.get("status", 200)), json.dumps(body).encode("utf-8")
        return 404, b'{"message": "Not Found"}'


class FakeTime:
    """``time`` с заглушкой ``sleep``: legacy спит между запросами, здесь пауза 0.

    Подставляется модулю legacy как ``module.time`` — глобальное ``time`` в
    процессе не трогается.
    """

    def __init__(self, real: Any) -> None:
        self._real = real

    def sleep(self, seconds: float) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def for_side(entries: list[dict] | None, side: str) -> list[dict]:
    """Правила, отдаваемые стороне ``side``: без ``side`` — обеим, с ``side`` — названной."""
    return [
        entry for entry in (entries or []) if entry.get("side") in (None, side)
    ]


def http_rules_for_side(rules: list[dict] | None, side: str) -> list[dict]:
    """HTTP-правила для стороны ``side`` (см. :func:`for_side`)."""
    return for_side(rules, side)


def gh_spec_for_side(spec: dict | None, side: str) -> dict:
    """Спецификация gh для стороны ``side``: асимметрия — только в правилах поиска."""
    source = dict(spec or {})
    return {
        **{key: value for key, value in source.items() if key != "search"},
        "search": for_side(source.get("search"), side),
    }


def make_gh(spec: dict | None) -> tuple[Any, Any, tuple[bool, str]]:
    """Фейковый gh: ``(runner для GhClient, run для legacy subprocess, (авторизован, текст))``.

    Диспетчер понимает обе формы вызова: ``["gh", "api", ...]`` (legacy
    ``subprocess.run``) и ``["api", ...]`` (``core.github.GhClient.runner``).
    Поиск отвечает по подстроке запроса (``match``), репозитории организации —
    по владельцу; так обе стороны получают одинаковые данные, даже когда
    декорируют запрос по-разному (``stars:>N``).
    """
    source = dict(spec or {})
    search_rules = list(source.get("search") or [])
    orgs = dict(source.get("orgs") or {})
    auth_ok = bool(source.get("auth_ok", True))

    def dispatch(argv: list[str]) -> tuple[int, str, str]:
        if argv[:1] == ["auth"]:
            return (0, "compat_runner: gh авторизован", "") if auth_ok else (1, "", "compat_runner: gh не авторизован")
        endpoint = argv[3] if len(argv) > 3 else ""
        params: dict[str, str] = {}
        for index, arg in enumerate(argv):
            if arg == "-f" and index + 1 < len(argv):
                key, _, value = argv[index + 1].partition("=")
                params[key] = value
        if endpoint == "search/repositories":
            query = params.get("q", "")
            for rule in search_rules:
                if str(rule.get("match") or "") in query:
                    items = [dict(item) for item in rule.get("items") or []]
                    payload = {"total_count": rule.get("total", len(items)), "items": items}
                    return 0, json.dumps(payload), ""
            return 0, json.dumps({"total_count": 0, "items": []}), ""
        if endpoint.startswith("orgs/") or endpoint.startswith("users/"):
            owner = endpoint.split("/")[1]
            return 0, json.dumps([dict(repo) for repo in orgs.get(owner, [])]), ""
        return 1, "", f"compat_runner: неожиданный вызов gh: {' '.join(argv[:4])}"

    def runner(argv: list[str]) -> tuple[int, str, str]:
        return dispatch(list(argv))

    def legacy_run(cmd: list[str], **kwargs: Any) -> Any:
        if cmd[:1] == ["gh"]:
            code, out, err = dispatch(list(cmd[1:]))
        else:
            code, out, err = 1, "", f"compat_runner: не gh в команде: {cmd[:2]!r}"
        return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)

    auth = (auth_ok, "" if auth_ok else "compat_runner: gh не авторизован")
    return runner, legacy_run, auth


def main(argv: list[str] | None = None) -> int:
    """``<cli-модуль> [args...]``: подменить транспорт/gh и позвать ``cli.<модуль>.main``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m tools.compat_runner <cli-модуль> [args...]", file=sys.stderr)
        return 2
    module_name, cli_args = args[0], args[1:]

    import importlib

    import core.http as http_module
    from core.cache import DictCache
    from core.github import GhClient
    from core.http import HttpClient, Throttle

    transport = MapTransport(http_rules_for_side(_env_json("GAPAUDIT_COMPAT_HTTP"), "new"))

    def build(cache_path: str = "raw/.pcache.json") -> HttpClient:
        return HttpClient(
            cache=DictCache(),
            throttle=Throttle(default_per_minute=0),
            transport=transport,
            sleep=lambda _seconds: None,
        )

    # Вызов через модуль — та же точка подмены, что в tests/cli/*.
    http_module.default_client = build

    cli_module = importlib.import_module("cli." + module_name)

    gh_spec_raw = _env_json("GAPAUDIT_COMPAT_GH")
    if gh_spec_raw:
        spec = gh_spec_for_side(gh_spec_raw, "new")
        runner, _legacy_run, auth = make_gh(spec)

        class FakeGh(GhClient):
            """GhClient на фейковом runner: без сети и без троттлинга."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(runner=runner)

            def _wait(self, per_minute: int) -> float:
                return 0.0

        def fake_setup() -> tuple[bool, str]:
            if not auth[0]:
                print(auth[1], file=sys.stderr)
            return auth

        cli_module.GhClient = FakeGh  # type: ignore[attr-defined]
        cli_module.setup_gh_or_warn = fake_setup  # type: ignore[attr-defined]

    return int(cli_module.main(cli_args) or 0)


# --- внутреннее ---------------------------------------------------------------


def _env_json(name: str) -> Any:
    """JSON из переменной окружения; пусто/нет — пустой словарь."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return {}
    return json.loads(Path(raw).read_text(encoding="utf-8"))


if __name__ == "__main__":
    sys.exit(main())
