"""Patents — патентный импульс: сколько заявок по теме за год (P5b, §18.3).

Второй сигнал GapAudit с API-ключом и первый, который ходит POST-запросом:
PatentsView Search API (``search.patentsview.org``, NIH/NSF) принимает JSON
тело ``{q, f, o}`` и требует заголовок ``X-Api-Key``. НЕ путать с
``api.uspto.gov`` — это другой API, отдельный провайдер, в текущей версии
не подключён (§14, ``docs/API-KEYS.md``).

Контракт модуля — как у ``core/collectors/jobs.py`` (AGENTS gotcha 50):

* **чистая функция** :func:`compute_patents` с инжектируемым транспортом:
  ключ приходит параметром (его читает ``cli/signals.py``), ни файловой
  системы, ни сети на импорте нет;
* **три состояния graceful degradation не смешиваются**:

  * нет ключа — ``configured=False``, пустой результат, ``error=None``:
    это «не настроен», не ошибка, транспорт не дёргается ни разу;
  * ключ есть, часть годов упала (429/5xx/сеть) — ``error=None``,
    ``total`` по успешным годам, ``by_year`` — только успешные;
  * ключ есть, упали все годы — ``error='all requests failed'``,
    ``configured=True``, ``total=0``: сигнал «ошибка», не «не настроен».

Формула (§18.3): импульс = (count(последний год) / count(год за ``years_back``
лет до него) − 1) · 100 %. Годы — завершённые календарные
``[current − years_back, current − 1]``: раскатывающееся «последние 12 мес»
включало бы неполный текущий год и занижало импульс. Нет базы (0 патентов в
базовом году), базовый год не собрался или успешен меньше одного года сверх
него — ``impulse_pct = None`` (делить не на что).

Контракт ответа проверен по исходникам API (PatentsView/PatentSearch-API):
успех — ``200`` с ``{"error": false, "count", "total_hits", "patents": [...]}``;
``total_hits`` — сколько записей совпало с запросом (то, что нам нужно),
``count`` — сколько вернулось в этом ответе (при ``size=1`` почти всегда 1).
Поле ``total_patent_count`` из первоначальной спецификации шага принимается
наравне с ``total_hits`` — в парсере оба. Опции ``o`` сервер читает как
``size`` (потолок 1000) и ``after``: ``per_page`` старого API здесь молча
игнорируется, поэтому в теле только то, что сервер реально понимает.

Лимит: 45 запросов/мин на ключ (§14); один прогон — ровно ``years_back``
запросов (по одному на год, нужен только счётчик). Троттлинг и кэш — забота
вызывающего, чтобы тесты оставались детерминированными.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__all__ = [
    "ALL_FAILED",
    "DEFAULT_TIMEOUT",
    "PATENTSVIEW_URL",
    "PostTransport",
    "PatentsSignal",
    "PatentsYearBucket",
    "compute_patents",
    "default_post_transport",
]

#: Endpoint поиска PatentsView: и GET, и POST принимают одни и те же
#: параметры, но POST-тело не роняет длинный запрос в URL (и ключ в URL
#: не попадает — ключ идёт заголовком, см. :func:`compute_patents`).
PATENTSVIEW_URL = "https://search.patentsview.org/api/v1/patent/"

#: Транспорт POST: ``(url, headers, timeout, body | None) -> (status, bytes)``.
#: Отдельный тип, а не ``Transport`` из ``jobs.py``: тот — GET без тела,
#: расширять его под POST значило бы менять контракт P5a (решение шага P5b:
#: новый PostTransport, чтобы не ломать jobs.py).
PostTransport = Callable[[str, dict, float, bytes | None], tuple[int, bytes]]

#: Таймаут одного запроса, сек.
DEFAULT_TIMEOUT = 20.0

#: Ошибка, когда ключ есть, но ни один год не ответил валидным счётчиком.
ALL_FAILED = "all requests failed"


@dataclass(frozen=True)
class PatentsYearBucket:
    """Число патентов за один год — строка таблицы в карточке."""

    year: int
    count: int

    def as_dict(self) -> dict[str, Any]:
        """Плоский словарь для JSON / ``signal_observations.detail``."""
        return {"year": self.year, "count": self.count}


@dataclass(frozen=True)
class PatentsSignal:
    """Сигнал patents для одного пакета (§18.3).

    ``total`` — сумма по УСПЕШНЫМ годам, ``by_year`` — по возрастанию
    (базовый год -> последний). ``impulse_pct`` — импульс в процентах или
    ``None`` (нет базы / нечего сравнивать). ``configured`` — был ли ключ
    на момент запроса; ``error`` — есть ли ошибка при наличии ключа
    (``None`` + ``configured=False`` — «не настроен», не ошибка).
    """

    total: int
    by_year: tuple[PatentsYearBucket, ...]
    impulse_pct: float | None
    configured: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON для ``--json-out`` / ``/api/signals`` и карточки пакета."""
        return {
            "total": self.total,
            "by_year": [row.as_dict() for row in self.by_year],
            "impulse_pct": self.impulse_pct,
            "configured": self.configured,
            "error": self.error,
        }


def compute_patents(
    query: str,
    *,
    api_key: str | None,
    transport: PostTransport,
    years_back: int = 3,
    timeout: float = DEFAULT_TIMEOUT,
    on_progress: Callable[[float, str], None] | None = None,
) -> PatentsSignal:
    """Патентный импульс по ключевому слову: POST PatentsView на каждый год окна.

    ``query`` — ключевое слово (обычно имя пакета), ищется по заголовку
    патента (``patent_title``, ``_text_any``). ``api_key`` — ключ PatentsView;
    пусто — «не настроен» (см. докстринг модуля), транспорт при этом НЕ
    дёргается ни разу: нет ключа — нет запроса.

    ``years_back`` — глубина окна завершённых лет (по умолчанию 3: текущий
    год не считаем — он неполный). ``transport`` инжектируется (тесты
    передают фейк с фикстурой); реальный — :func:`default_post_transport`.
    Ошибка одного года — пропуск, а не падение всего сигнала.
    ``on_progress((i+1)/years_back, "patents <year>")`` после каждого года —
    прогресс задачи доезжает до UI через SSE.
    """
    if not api_key:
        # «Не настроен» — не ошибка: ни одного запроса, пустой результат.
        return PatentsSignal(
            total=0, by_year=(), impulse_pct=None, configured=False, error=None
        )

    # Текущий год замеряем один раз: на границе Нового года два вызова
    # дали бы разные значения и окно «съехало» бы на год.
    current = datetime.now(timezone.utc).year
    years = list(range(current - years_back, current))
    headers = {
        # Ключ — только заголовком (документация Search API: X-Api-Key);
        # в URL его не кладём — URL попадает в логи и историю.
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    counts: dict[int, int] = {}
    ok = 0
    for index, year in enumerate(years):
        body = json.dumps(_build_query(query, year), ensure_ascii=False).encode("utf-8")
        try:
            status, raw = transport(PATENTSVIEW_URL, headers, timeout, body)
        except Exception:  # noqa: BLE001 - сбой транспорта на одном годе пропускаем
            status, raw = 0, b""
        count = _parse_patents_count(status, raw)
        if count is not None:
            counts[year] = count
            ok += 1
        if on_progress is not None and years:
            on_progress((index + 1) / len(years), f"patents {year}")

    if years and ok == 0:
        # Ключ есть, а данных нет ни за один год — «ошибка», а не «не настроен».
        return PatentsSignal(
            total=0, by_year=(), impulse_pct=None, configured=True, error=ALL_FAILED
        )

    by_year = tuple(
        PatentsYearBucket(year=year, count=counts[year]) for year in sorted(counts)
    )
    # Импульс — только когда собраны оба конца окна и у базы есть что делить:
    # базовый год = current − years_back, последний = current − 1. Правило
    # «< 2 успешных лет» (спецификация шага) включено явно: при years_back = 1
    # окно из одного года не может дать осмысленное сравнение.
    first = current - years_back
    last = current - 1
    impulse: float | None = None
    if ok >= 2 and first in counts and last in counts and counts[first] > 0:
        impulse = (counts[last] / counts[first] - 1) * 100.0
    return PatentsSignal(
        total=sum(counts.values()),
        by_year=by_year,
        impulse_pct=impulse,
        configured=True,
        error=None,
    )


def _parse_patents_count(status: int, body: bytes) -> int | None:
    """Сколько патентов совпало с запросом; ``None`` — ответ непригоден.

    ``200`` с JSON-объектом и целым счётчиком — успех. Всё остальное —
    ``None``: ``429`` (лимит 45/мин), ``5xx``, прочие ``4xx``, пустое или
    битое тело. Счётчики: ``total_hits`` — фактическое поле ответа
    (APISerializer PatentsView: ``error``/``count``/``total_hits``),
    ``total_patent_count`` — имя из спецификации шага P5b; принимаем оба.
    ``count`` НЕ берём: это размер страницы ответа (при ``size=1`` почти
    всегда 1), а не итог по запросу. ``bool`` (JSON ``true``) числом не
    считается — как в jobs.py и momentum.py.
    """
    if status != 200:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("total_hits", "total_patent_count"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        return value
    return None


def _build_query(query: str, year: int) -> dict[str, Any]:
    """JSON-тело POST для одного года: текстовый поиск + диапазон дат.

    ``q`` — ровно один ключ (сервер принимает только одноключевой объект
    верхнего уровня, остальное — внутри ``_and``). ``patent_date`` — дата
    выдачи патента, поэтому год закрыт границами ``01-01``..``12-31``.
    ``size=1``: нужен только счётчик ``total_hits``, одна запись в ответе —
    минимум трафика в рамках лимита 45/мин.
    """
    return {
        "q": {
            "_and": [
                {"_text_any": {"patent_title": str(query)}},
                {"_gte": {"patent_date": f"{year}-01-01"}},
                {"_lte": {"patent_date": f"{year}-12-31"}},
            ]
        },
        "f": ["patent_id"],
        "o": {"size": 1},
    }


def default_post_transport(
    url: str,
    headers: dict,
    timeout: float,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    """Настоящий POST через ``urllib``; ошибка сети — ``(код, b'')``, не исключение.

    В тестах не используется — там инжектируется фейк (модуль чистый).
    ``HTTPError`` несёт код ответа (403 неверный ключ, 429 лимит, 400 бад
    запрос) и уходит в :func:`_parse_patents_count` как обычно; ``URLError``
    (сеть недоступна, DNS) -> ``(0, b'')`` — «пропустить год», а не уронить
    сигнал. Значение ключа никуда не печатается: заголовок с ``X-Api-Key``
    не попадает ни в лог, ни в сообщения об ошибках.
    """
    request = Request(url, data=body, headers=dict(headers or {}), method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL из константы
            return int(response.status or 200), response.read()
    except HTTPError as error:
        return int(error.code), b""
    except (URLError, OSError):
        return 0, b""
