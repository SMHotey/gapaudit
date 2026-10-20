"""Funding — раунды финансирования компаний, упоминающих технологию (P5c, §18.3).

Третий сигнал GapAudit с внешним API и первый, которому нужен НЕ ключ, а
**User-Agent с email**: SEC EDGAR full-text search (``efts.sec.gov``) требует
идентификации клиента и отвечает ``403`` без неё (AGENTS gotcha 52,
``docs/API-KEYS.md``). Источник — форма D (:data:`EDGAR_FORMS`): уведомление о
привлечении капитала, выходит ДО пресс-релизов.

Контракт модуля — как у ``core/collectors/jobs.py`` и ``patents.py``
(AGENTS gotcha 50):

* **чистая функция** :func:`compute_funding` с инжектируемыми транспортом и
  функцией сна: User-Agent приходит параметром (его читает
  ``cli/signals.py`` через ``ApiKeysStore.public_meta("edgar")`` — не
  ``get``, у ``edgar`` он лежит в ``meta``), ни файловой системы, ни сети на
  импорте нет;
* **три состояния graceful degradation не смешиваются**:

  * нет User-Agent — ``configured=False``, пустой результат, ``error=None``:
    это «не настроен», не ошибка; транспорт не дёргается ни разу;
  * User-Agent есть, часть кварталов упала (403/429/5xx/сеть) —
    ``error=None``, ``total`` по успешным, ``by_quarter`` — только успешные;
  * User-Agent есть, упали все кварталы — ``error='all requests failed'``,
    ``configured=True``, ``total=0``: сигнал «ошибка», не «не настроен».

Окно — последние :data:`DEFAULT_QUARTERS_BACK` **завершённых** календарных
кварталов (см. :func:`_quarter_window`): текущий квартал неполон и дал бы
ложный спад на тренде — то же правило, что у окна лет в ``patents.py``
(DEVPLAN §18.3).

Формат ответа проверен живым запросом к ``efts.sec.gov`` (20.10.2026):
``200`` с ``{"took":..., "hits":{"total":{"value":3,"relation":"eq"},
"hits":[...]}}``; без ``User-Agent`` — ``403``. Парсер принимает и
``hits.total.value`` (фактическая форма Elasticsearch-ответа), и
``hits.total`` целым числом (форма из спецификации шага) — оба варианта
покрыты тестами.

Лимит SEC — 10 запросов/сек (§14, ``docs/API-KEYS.md``); один прогон — ровно
``quarters_back`` запросов, между ними :data:`EDGAR_RATE_LIMIT_SEC` (0.11 с с
запасом). Сон инжектируется: тесты не ждут.
"""

from __future__ import annotations

import calendar
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

__all__ = [
    "ALL_FAILED",
    "DEFAULT_QUARTERS_BACK",
    "DEFAULT_TIMEOUT",
    "EDGAR_FORMS",
    "EDGAR_RATE_LIMIT_SEC",
    "EDGAR_URL",
    "FundingQuarter",
    "FundingSignal",
    "Transport",
    "compute_funding",
    "default_transport",
]

#: Endpoint полнотекстового поиска SEC EDGAR. Проверено живым запросом:
#: ``?q=...&forms=D&dateRange=custom&startdt=...&enddt=...`` отдаёт JSON
#: (``hits.total.value``), без ``User-Agent`` — ``403``.
EDGAR_URL = "https://efts.sec.gov/LATEST/search-index"

#: Форма D — «Notice of Exempt Offering of Securities», то есть привлечение
#: капитала. Сервер отдаёт вместе с ней и ``D/A`` (поправки к предложению):
#: ``forms`` фильтрует по ``root_forms``, поэтому поправки попадают в счётчик.
#: Это осознанно: поправка — тоже событие раунда, а не отдельный сигнал.
EDGAR_FORMS = "D"

#: Лимит SEC — 10 запросов/сек, значит 0.1 с между запросами; 0.11 — с запасом.
EDGAR_RATE_LIMIT_SEC = 0.11

#: Сколько завершённых кварталов собирать (окно тренда, §18.3).
DEFAULT_QUARTERS_BACK = 4

#: Таймаут одного запроса, сек.
DEFAULT_TIMEOUT = 20.0

#: Ошибка, когда User-Agent есть, но ни один квартал не ответил счётчиком.
ALL_FAILED = "all requests failed"

#: Транспорт GET: ``(url, headers, timeout) -> (status, bytes)`` — та же форма,
#: что у ``core/collectors/jobs.py`` (у EDGAR поиск — GET, тело не нужно).
Transport = Callable[[str, dict, float], tuple[int, bytes]]


@dataclass(frozen=True)
class FundingQuarter:
    """Число filing'ов формы D за один квартал — строка таблицы в карточке."""

    year: int
    quarter: int  # 1..4
    count: int

    def as_dict(self) -> dict[str, Any]:
        """Плоский словарь для JSON / ``signal_observations.detail``."""
        return {"year": self.year, "quarter": self.quarter, "count": self.count}


@dataclass(frozen=True)
class FundingSignal:
    """Сигнал funding для одного пакета (§18.3).

    ``total`` — сумма по УСПЕШНЫМ кварталам, ``by_quarter`` — по возрастанию
    (первый квартал окна -> последний). ``trend_pct`` — тренд в процентах или
    ``None`` (нет базы / нечего сравнивать). ``configured`` — был ли
    User-Agent на момент запроса; ``error`` — есть ли ошибка при наличии
    User-Agent (``None`` + ``configured=False`` — «не настроен», не ошибка).
    """

    total: int
    by_quarter: tuple[FundingQuarter, ...]
    trend_pct: float | None
    configured: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON для ``--json-out`` / ``/api/signals`` и карточки пакета."""
        return {
            "total": self.total,
            "by_quarter": [row.as_dict() for row in self.by_quarter],
            "trend_pct": self.trend_pct,
            "configured": self.configured,
            "error": self.error,
        }


def compute_funding(
    query: str,
    *,
    user_agent: str | None,
    transport: Transport,
    quarters_back: int = DEFAULT_QUARTERS_BACK,
    timeout: float = DEFAULT_TIMEOUT,
    sleep_between: float = EDGAR_RATE_LIMIT_SEC,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[float, str], None] | None = None,
) -> FundingSignal:
    """Раунды финансирования по ключевому слову: GET EDGAR на каждый квартал окна.

    ``query`` — ключевое слово (обычно имя пакета), ищется полнотекстово
    (``q=``). ``user_agent`` — строка ``"GapAudit email@example.com"``
    (требование SEC); пусто — «не настроен» (см. докстринг модуля),
    транспорт при этом НЕ дёргается ни разу: нет User-Agent — нет запроса.

    ``quarters_back`` — глубина окна завершённых кварталов. ``transport`` и
    ``sleep`` инжектируются (тесты передают фейки); реальный транспорт —
    :func:`default_transport`. Ошибка одного квартала (статус, битое тело,
    исключение транспорта) — пропуск, а не падение всего сигнала.

    ``sleep_between`` вызывается МЕЖДУ запросами (``quarters_back - 1`` раз) —
    уважение к лимиту 10 запросов/сек. ``on_progress((i+1)/len, "funding
    Q<квартал>/<год>")`` после каждого квартала — так прогресс задачи доезжает
    до UI через SSE.
    """
    if not user_agent or not str(user_agent).strip():
        # «Не настроен» — не ошибка: ни одного запроса, пустой результат.
        return FundingSignal(
            total=0, by_quarter=(), trend_pct=None, configured=False, error=None
        )

    quarters = _quarter_window(quarters_back)
    headers = {
        # Значение User-Agent задаёт пользователь в Настройках → API-ключи;
        # без него SEC отвечает 403 — тогда сигнал «не настроен» (выше).
        "User-Agent": str(user_agent).strip(),
        "Accept": "application/json",
    }
    counts: dict[tuple[int, int], int] = {}
    ok = 0
    for index, (year, quarter) in enumerate(quarters):
        if index and sleep_between > 0:
            # Между запросами, не после последнего: лишняя пауза в конце —
            # это секунда ожидания задачи «на пустом месте».
            sleep(sleep_between)
        url = _build_url(query, year, quarter)
        try:
            status, body = transport(url, headers, timeout)
        except Exception:  # noqa: BLE001 - сбой транспорта на квартале пропускаем
            status, body = 0, b""
        count = _parse_hits_total(status, body)
        if count is not None:
            counts[(year, quarter)] = count
            ok += 1
        if on_progress is not None and quarters:
            on_progress((index + 1) / len(quarters), f"funding Q{quarter}/{year}")

    if quarters and ok == 0:
        # User-Agent есть, а данных нет ни за один квартал — «ошибка», а не
        # «не настроен»: в карточке это разные строки.
        return FundingSignal(
            total=0, by_quarter=(), trend_pct=None, configured=True, error=ALL_FAILED
        )

    by_quarter = tuple(
        FundingQuarter(year=year, quarter=quarter, count=counts[(year, quarter)])
        for year, quarter in sorted(counts)
    )
    # Тренд — только когда собраны оба конца окна и у базы есть что делить:
    # первый квартал окна и последний. Меньше двух успешных кварталов —
    # сравнивать нечего (как «< 2 успешных лет» у patents).
    trend: float | None = None
    if ok >= 2 and quarters:
        first, last = quarters[0], quarters[-1]
        if first in counts and last in counts and counts[first] > 0:
            trend = (counts[last] / counts[first] - 1) * 100.0
    return FundingSignal(
        total=sum(counts.values()),
        by_quarter=by_quarter,
        trend_pct=trend,
        configured=True,
        error=None,
    )


def _quarter_window(
    quarters_back: int, today: date | None = None
) -> list[tuple[int, int]]:
    """Последние ``quarters_back`` ЗАВЕРШЁННЫХ кварталов, по возрастанию.

    Текущий квартал не входит: он неполон, и счётчик filing'ов в нём заведомо
    ниже — тренд «последний квартал к первому» показывал бы ложный спад.
    То же правило, что у окна лет в ``patents.py`` (DEVPLAN §18.3).

    Квартал индексируется целым числом ``year * 4 + (month - 1) // 3`` (с
    января 0), поэтому арифметика не ломается на границе года.
    """
    if quarters_back <= 0:
        return []
    moment = today or datetime.now(timezone.utc).date()
    current_index = moment.year * 4 + (moment.month - 1) // 3
    return [
        (index // 4, index % 4 + 1)
        for index in range(current_index - quarters_back, current_index)
    ]


def _quarter_dates(year: int, quarter: int) -> tuple[str, str]:
    """Границы квартала ISO-датами: первое и последнее число (``YYYY-MM-DD``)."""
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    last_day = calendar.monthrange(year, end_month)[1]
    return (
        f"{year:04d}-{start_month:02d}-01",
        f"{year:04d}-{end_month:02d}-{last_day:02d}",
    )


def _build_url(query: str, year: int, quarter: int) -> str:
    """URL запроса одного квартала: ``q``, ``forms``, ``dateRange=custom`` + даты.

    ``dateRange=custom`` обязателен: без него ``startdt``/``enddt`` сервер
    игнорирует и отдаёт весь архив (проверено на живом запросе). Даты —
    границы квартала включительно.
    """
    start, end = _quarter_dates(year, quarter)
    params = urlencode(
        {
            "q": str(query),
            "forms": EDGAR_FORMS,
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
        }
    )
    return f"{EDGAR_URL}?{params}"


def _parse_hits_total(status: int, body: bytes) -> int | None:
    """Сколько filing'ов совпало с запросом; ``None`` — ответ непригоден.

    ``200`` с JSON-объектом и целым счётчиком — успех. Всё остальное —
    ``None``: ``403`` (нет User-Agent), ``429`` (лимит 10/сек), ``5xx``,
    прочие ``4xx``, пустое или битое тело.

    Счётчик EDGAR — ``hits.total.value`` (фактическая форма Elasticsearch,
    проверена живым запросом); ``hits.total`` целым числом — форма из
    спецификации шага P5c, принимаем обе. ``bool`` (JSON ``true``) числом не
    считается — как в jobs.py, patents.py и momentum.py.
    """
    if status != 200:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    hits = payload.get("hits")
    if not isinstance(hits, dict):
        return None
    total = hits.get("total")
    if isinstance(total, dict):
        total = total.get("value")
    if isinstance(total, bool) or not isinstance(total, int):
        return None
    return total


def default_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    """Настоящий GET через ``urllib``; ошибка сети — ``(код, b'')``, не исключение.

    В тестах не используется — там инжектируется фейк (модуль чистый).
    ``HTTPError`` несёт код ответа (403 без User-Agent, 429 лимит) и уходит в
    :func:`_parse_hits_total` как обычно; ``URLError`` (сеть недоступна, DNS)
    -> ``(0, b'')`` — «пропустить квартал», а не уронить сигнал.
    """
    request = Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL из константы
            return int(response.status or 200), response.read()
    except HTTPError as error:
        return int(error.code), b""
    except (URLError, OSError):
        return 0, b""
