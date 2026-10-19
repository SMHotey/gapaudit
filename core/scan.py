"""Потоковый обход каталога реестра MCP: размер, классы задач, тип поставки (шаг 1e).

Порт ``legacy/catalog_scan.py``. Логика та же: один проход по курсору реестра
до конца, потому что замер размера каталога и плотности классов задач порознь
означал бы два обхода по десятки минут, а ``search``-параметр реестра для
плотности не годится (возвращает 0–1 почти на всё — проверено в legacy).

Что считает проход: число записей, уникальные имена, концентрацию неймспейсов,
попадания подстрок-классов в имя/заголовок/описание и тип поставки (``remotes`` /
``packages`` — то есть «сервер по HTTP», «пакет в npm/PyPI», «и то и другое»).

Отличия от legacy (правила переезда в ``core``, DEVPLAN §4):

* ничего не печатает и не пишет: прогресс и чекпоинт идут колбэками, файлы
  (чекпоинт и итоговый JSON) пишет вызывающий — CLI-обёртка 1f, очередь P1;
* вместо Ctrl+C — ``threading.Event``: обход идёт в подпроцессе задачи, сигнал
  ему не приходит;
* HTTP-клиент и ``sleep`` инжектируются (тесты обходятся без сети и без пауз);
* время считает :func:`time.monotonic`, а не ``time.time``: настенные часы могут
  прыгнуть (перевод часов, синхронизация), монотонные — нет.

Классы задач (:data:`DEFAULT_CLASSES`) — дефолт для CLI; приложение передаёт свой
список из настроек, поэтому таблица классов здесь параметр, а не константа в коде
обхода.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from core.http import HttpClient
from core.mcp_registry import DEFAULT_LIMIT, MCP_LIST_URL, servers_page

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CLASSES",
    "SCAN_KINDS",
    "TOP_NAMESPACES",
    "ScanConfig",
    "ScanState",
    "scan",
    "update_state",
]

#: URL каталога реестра с пагинацией — как в legacy ``catalog_scan.BASE``.
#: Это ШАБЛОН с ``{limit}`` (шаг 1g): так ``--limit``/``config.limit`` продолжает
#: управлять страницей, а конкретный URL складывается в ``servers_page``.
DEFAULT_BASE_URL = MCP_LIST_URL

#: Классы задач: подстрока -> человекочитаемый ярлык (порядок как в legacy).
DEFAULT_CLASSES: tuple[tuple[str, str], ...] = (
    ("kubernetes", "kubernetes"),
    ("code search", "поиск/индексация кода"),
    ("sql", "SQL/аналитика"),
    ("memory", "память агента"),
    ("payment", "платежи/финансы"),
    ("prompt", "промпты/скиллы"),
    ("eval", "eval агентов"),
    ("pdf", "PDF/office"),
    ("schema", "схемы БД"),
    ("observability", "observability"),
    ("trace", "трассировка"),
    ("sbom", "SBOM/supply chain"),
    ("accessibility", "доступность"),
    ("i18n", "i18n"),
    ("flaky", "флаки-тесты"),
    ("incident", "инциденты"),
    ("postmortem", "постмортемы"),
    ("terraform", "Terraform/IaC"),
    ("cloud cost", "стоимость облака"),
    ("finops", "FinOps"),
    ("monorepo", "монорепа"),
    ("dead code", "мёртвый код"),
    ("codemod", "codemod"),
    ("flamegraph", "фламеграфы"),
    ("profiler", "профилировщик"),
    ("heap", "heap/утечки"),
    ("stack trace", "стектрейсы"),
    ("code review", "код-ревью"),
    ("pull request review", "ревью PR"),
    ("design token", "дизайн-токены"),
    ("impact analysis", "влияние зависимостей"),
    ("context budget", "контекстный бюджет"),
    ("token budget", "бюджет токенов"),
    ("documentation", "документация"),
    ("e2e", "e2e-тесты"),
    ("test generation", "генерация тестов"),
    ("migration", "миграции"),
    ("query plan", "план запроса"),
    ("secret", "секреты"),
    ("dbt", "dbt/ETL"),
    ("browser", "браузер/скрейпинг"),
)

#: Тип поставки сервера каталога — счётчики всегда есть все четыре (как в legacy).
SCAN_KINDS: tuple[str, ...] = ("remote", "package", "both", "none")

#: Сколько неймспейсов попадает в итоговую сводку (как в legacy-финале).
TOP_NAMESPACES = 20

#: Запас на «нет данных» / чужой тип в чекпоинте.
_EMPTY = ""


@dataclass
class ScanState:
    """Состояние обхода: прогресс, счётчики и курсор продолжения.

    Чекпоинт — это ровно этот объект (:meth:`as_checkpoint`): обход длится десятки
    минут, и задача, убитая вместе с окном, должна продолжиться с той же страницы.
    """

    base_url: str
    cursor: str | None = None
    pages: int = 0
    entries: int = 0
    names: set[str] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, int] = field(
        default_factory=lambda: {kind: 0 for kind in SCAN_KINDS}
    )
    namespaces: dict[str, int] = field(default_factory=dict)
    finished: bool = False
    started_at: float = 0.0
    elapsed: float = 0.0

    def as_checkpoint(self) -> dict:
        """Состояние словарём для JSON-файла чекпоинта.

        ``names`` пишется отсортированным списком: так файл чекпоинта детерминирован
        и его можно сравнивать в тестах и коммитить в отчёт без шума от порядка set.
        """
        return {
            "base_url": self.base_url,
            "cursor": self.cursor,
            "pages": self.pages,
            "entries": self.entries,
            "names": sorted(self.names),
            "counts": dict(self.counts),
            "kinds": dict(self.kinds),
            "namespaces": dict(self.namespaces),
            "finished": self.finished,
            "started_at": self.started_at,
            "elapsed": self.elapsed,
        }

    @classmethod
    def from_checkpoint(cls, checkpoint: dict | None) -> ScanState:
        """Восстановить состояние из чекпоинта; мусор и пропуски — дефолты.

        Понимает и старый формат legacy (``ns`` вместо ``namespaces``): чекпоинты
        писались в ``raw/registry_scan_ckpt.json`` и после переезда на ``core``
        должны читаться, иначе обход на 40 минут начнётся заново.
        """
        data = checkpoint if isinstance(checkpoint, dict) else {}
        state = cls(base_url=str(data.get("base_url") or DEFAULT_BASE_URL))
        state.cursor = _opt_str(data.get("cursor"))
        state.pages = _as_int(data.get("pages"))
        state.entries = _as_int(data.get("entries"))
        state.names = {str(name) for name in _as_list(data.get("names"))}
        state.counts = _as_counts(data.get("counts"))
        state.kinds = _as_kinds(data.get("kinds"))
        state.namespaces = _as_counts(data.get("namespaces") if "namespaces" in data else data.get("ns"))
        state.finished = bool(data.get("finished"))
        state.started_at = _as_float(data.get("started_at"))
        state.elapsed = _as_float(data.get("elapsed"))
        return state

    def summary(self) -> dict:
        """Итоговая сводка — форма legacy-файла ``registry_full_scan.json``.

        Ключей девять, как в legacy-финале (``entries``, ``pages``, ``unique_names``,
        ``namespaces``, ``top_namespaces``, ``class_counts``, ``delivery_kinds``,
        ``seconds``, ``finished``). ``seconds`` округлён до целых, как там же; кому
        нужна точность — берёт ``state.elapsed``.

        ``top_namespaces`` — отсортированный список пар, порядок при равных счётчиках
        зависит от порядка встречи (стабильная сортировка, как в legacy).
        """
        return {
            "entries": self.entries,
            "pages": self.pages,
            "unique_names": len(self.names),
            "namespaces": len(self.namespaces),
            "top_namespaces": sorted(self.namespaces.items(), key=lambda item: -item[1])[:TOP_NAMESPACES],
            "class_counts": dict(self.counts),
            "delivery_kinds": dict(self.kinds),
            "seconds": round(self.elapsed),
            "finished": self.finished,
        }


@dataclass
class ScanConfig:
    """Параметры обхода: откуда, чем считать, как часто чекпоинтить."""

    base_url: str = DEFAULT_BASE_URL
    classes: tuple[tuple[str, str], ...] = DEFAULT_CLASSES
    limit: int = DEFAULT_LIMIT
    checkpoint_every: int = 25
    max_pages: int = 400
    sleep_between_pages: float = 0.05


def update_state(
    state: ScanState,
    items: Iterable[dict] | None,
    classes: tuple[tuple[str, str], ...],
) -> None:
    """Обновить счётчики по странице серверов — чистая функция, порт тело цикла legacy.

    ``items`` — объекты серверов реестра: ``core.mcp_registry.servers_page`` отдаёт их
    уже развёрнутыми из ``servers[].server``. Записи в форме ответа
    (``{"server": {...}}``) тоже принимаются — так функция годится и для сырого
    ответа из чекпоинта/тестов.

    Поведение legacy сохранено дословно, включая причуду: сервер без имени попадает
    в ``names`` пустой строкой и добавляет единицу к ``unique_names``. Это осознанный
    паритет (см. ``tests/test_core_scan_legacy_contract.py``), а не опечатка.
    """
    for item in items or []:
        server = _server_of(item)
        state.entries += 1
        name = server.get("name") or _EMPTY
        state.names.add(name)
        if "/" in name:
            namespace = name.split("/")[0]
            state.namespaces[namespace] = state.namespaces.get(namespace, 0) + 1
        haystack = " ".join(
            [name, server.get("title") or _EMPTY, server.get("description") or _EMPTY]
        ).lower()
        for needle, label in classes:
            if needle in haystack:
                state.counts[label] = state.counts.get(label, 0) + 1
        state.kinds[_kind_of(server)] += 1


def scan(
    http: HttpClient,
    *,
    config: ScanConfig = ScanConfig(),
    state: ScanState | None = None,
    on_progress: Callable[[ScanState], None] | None = None,
    on_checkpoint: Callable[[ScanState], None] | None = None,
    cancel: threading.Event | None = None,
    sleep: Callable[[float], None] | None = time.sleep,
) -> ScanState:
    """Пройти каталог реестра по курсору; вернуть состояние (и, если дошли, ``finished``).

    Порядок действий на странице — как в legacy: получить страницу, обновить счётчики,
    записать прогресс, снять чекпоинт, и только потом решать, продолжать ли. Чекпоинт
    поэтому снимается и на последней странице (там он уже с ``cursor=None``), а
    ``on_checkpoint`` вызывается каждые ``config.checkpoint_every`` страниц.

    Остановки:

    * курсор кончился (``nextCursor`` нет) — ``finished=True``, каталог пройден;
    * страница не пришла (сеть/статус) — цикл встаёт, ``finished`` остаётся ``False``,
      а ``state.cursor`` держит курсор последней удачной страницы: следующий вызов
      :func:`scan` с этим же состоянием продолжит обход;
    * ``cancel.is_set()`` — остановка по требованию (задачу отменяют), ``finished=False``;
    * ``config.max_pages`` — предохранитель от бесконечного обхода.

    Ничего не печатает и не пишет: чекпоинт и итоговый JSON — дело вызывающего.
    """
    if state is None:
        state = ScanState(base_url=config.base_url)
    elif not state.base_url:
        state.base_url = config.base_url

    state.started_at = state.started_at or time.monotonic()
    # Нули по всем классам — как в legacy-финале: class_counts перечисляет все ярлыки,
    # иначе «сколько было kubernetes» нельзя отличить от «класса нет в списке».
    for _, label in config.classes:
        state.counts.setdefault(label, 0)

    while state.pages < config.max_pages and not state.finished:
        if cancel is not None and cancel.is_set():
            break

        servers, next_cursor = servers_page(
            http,
            base_url=config.base_url,
            cursor=state.cursor,
            limit=config.limit,
        )
        if servers is None:
            break

        update_state(state, servers, config.classes)
        state.pages += 1
        state.cursor = next_cursor

        if on_progress is not None:
            on_progress(state)
        if (
            on_checkpoint is not None
            and config.checkpoint_every > 0
            and state.pages % config.checkpoint_every == 0
        ):
            on_checkpoint(state)

        if next_cursor is None:
            state.finished = True
            break

        if config.sleep_between_pages > 0 and sleep is not None:
            sleep(config.sleep_between_pages)

    state.elapsed = time.monotonic() - state.started_at
    return state


# --- внутреннее ---------------------------------------------------------------


def _server_of(item: Any) -> dict:
    """Объект сервера из элемента страницы: развёрнутый или в форме ответа реестра."""
    if not isinstance(item, dict):
        return {}
    inner = item.get("server")
    if isinstance(inner, dict):
        return inner
    return item


def _kind_of(server: dict) -> str:
    """Тип поставки: есть remotes и/или packages (как в legacy)."""
    has_remote = bool(server.get("remotes"))
    has_package = bool(server.get("packages"))
    if has_remote and has_package:
        return "both"
    if has_remote:
        return "remote"
    if has_package:
        return "package"
    return "none"


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return 0


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _as_counts(value: Any) -> dict[str, int]:
    """Счётчики из чекпоинта: словарь строк/чисел; мусор пропускаем."""
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, count in value.items():
        if isinstance(key, str) and isinstance(count, int) and not isinstance(count, bool):
            counts[key] = count
    return counts


def _as_kinds(value: Any) -> dict[str, int]:
    """Типы поставки из чекпоинта; отсутствующие — нулями (форма всегда полная)."""
    counts = _as_counts(value)
    return {kind: counts.get(kind, 0) for kind in SCAN_KINDS}
