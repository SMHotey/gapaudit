"""Jobs — количество вакансий по ключевому слову, по странам (P5a, §18.3).

Первый сигнал GapAudit, которому нужен API-ключ: Adzuna (``app_id`` +
``app_key``, инструкция — ``docs/API-KEYS.md``). Прямой гео-сигнал спроса
на труд: «в Германии 200 вакансий с Rust, в Италии — 12».

Контракт модуля (AGENTS gotcha 50):

* **чистая функция** :func:`compute_jobs` с инжектируемым транспортом:
  ключ приходит параметром (его читает вызывающий — ``cli/signals.py``),
  ни файловой системы, ни сети на импорте нет;
* **три состояния graceful degradation, которые нельзя смешивать**:

  * нет ключа — ``configured=False``, пустой результат, ``error=None``:
    это «сигнал не настроен», а не ошибка: приложение работает, UI
    показывает ссылку «Настройки → API-ключи»;
  * ключ есть, часть стран упала (429/5xx/сеть) — ``error=None``,
    ``total`` по успешным: падать из-за одной страны нельзя, Adzuna и так
    отвечает локальными ошибками;
  * ключ есть, упали все страны — ``error='all requests failed'``,
    ``configured=True``, ``total=0`: сигнал «ошибка», а не «не настроен»:
    ключ введён, но данных нет — это видно по строке ошибки в карточке.

ToS (§14, docs/API-KEYS.md): Adzuna запрещает агрегацию счётчиков вакансий
для постоянных исследований без письменного согласия; лимиты free tier —
25 hits/мин, 250/сутки, 1000/нед, 2500/мес. Один прогон — ровно
``len(countries)`` запросов (по одному на страну, ``results_per_page=1``:
нужен только счётчик ``count``, а не страница вакансий). Троттлинг и кэш —
забота вызывающего: модуль их не содержит, чтобы тесты оставались
детерминированными.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

__all__ = [
    "ADZUNA_COUNTRIES",
    "ADZUNA_URL_TEMPLATE",
    "COUNTRY_LABELS",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TOP_N",
    "JobsByCountry",
    "JobsSignal",
    "Transport",
    "compute_jobs",
    "default_transport",
]

#: Двузначные коды стран, которые поддерживает Adzuna (docs/API-KEYS.md).
ADZUNA_COUNTRIES: tuple[str, ...] = (
    "gb", "us", "de", "fr", "nl", "au", "br", "in", "it", "es", "ca",
)

#: URL поиска Adzuna: одна страна, одна страница; общее число вакансий —
#: поле ``count`` в ответе, поэтому ``results_per_page=1`` (минимальный
#: запрос в рамках лимита 25 hits/мин).
ADZUNA_URL_TEMPLATE = (
    "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    "?app_id={app_id}&app_key={app_key}&what={what}"
    "&results_per_page=1"
)

#: Транспорт: ``(url, headers, timeout) -> (status, bytes)`` — та же форма,
#: что у ``core.http.Transport``, но без кэша/троттлинга: у адзунинского
#: клиента свой жизненный цикл (один прогон = 11 запросов).
Transport = Callable[[str, dict, float], tuple[int, bytes]]

#: Подписи стран для UI: код -> название («Germany», а не «de»).
COUNTRY_LABELS: dict[str, str] = {
    "gb": "United Kingdom",
    "us": "United States",
    "de": "Germany",
    "fr": "France",
    "nl": "Netherlands",
    "au": "Australia",
    "br": "Brazil",
    "in": "India",
    "it": "Italy",
    "es": "Spain",
    "ca": "Canada",
}

#: Сколько стран показывать в карточке (§18.3: top-5 по числу вакансий).
DEFAULT_TOP_N = 5

#: Таймаут одного запроса, сек.
DEFAULT_TIMEOUT = 20.0

#: Ошибка, когда ключ есть, но ни одна страна не ответила валидным count.
ALL_FAILED = "all requests failed"


@dataclass(frozen=True)
class JobsByCountry:
    """Число вакансий в одной стране — строка таблицы в карточке."""

    country: str
    count: int
    label: str

    def as_dict(self) -> dict[str, Any]:
        """Плоский словарь для JSON/``signal_observations.detail``."""
        return {"country": self.country, "count": self.count, "label": self.label}


@dataclass(frozen=True)
class JobsSignal:
    """Сигнал jobs для одного пакета (§18.3).

    ``total`` — сумма по ВСЕМ успешным странам, ``by_country`` — top-N по
    ``count`` DESC (только страны с ``count > 0``: нулевая строка в
    «топ-5» — не сигнал, а отсутствие данных). ``configured`` — был ли
    ключ на момент запроса; ``error`` — есть ли ошибка при наличии ключа
    (``None`` и ``configured=False`` — «не настроен», не ошибка).
    """

    total: int
    by_country: tuple[JobsByCountry, ...]
    configured: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON для ``--json-out``/``/api/signals`` и карточки пакета."""
        return {
            "total": self.total,
            "by_country": [row.as_dict() for row in self.by_country],
            "configured": self.configured,
            "error": self.error,
        }


def compute_jobs(
    query: str,
    *,
    app_id: str | None,
    app_key: str | None,
    transport: Transport,
    countries: tuple[str, ...] = ADZUNA_COUNTRIES,
    top_n: int = DEFAULT_TOP_N,
    timeout: float = DEFAULT_TIMEOUT,
    on_progress: Callable[[float, str], None] | None = None,
) -> JobsSignal:
    """Спрос на труд по ключевому слову: GET Adzuna на каждую страну, разбор ``count``.

    ``query`` — ключевое слово (обычно имя пакета). ``app_id``/``app_key`` —
    учётные данные Adzuna; отсутствие любого — «не настроен» (см. докстринг
    модуля), транспорт при этом НЕ дёргается ни разу: нет ключа — нет и
    запроса, а не «упал по пути».

    ``transport`` инжектируется (тесты передают фейк, читающий фикстуру);
    реальный — :func:`default_transport`. Ошибка одной страны (статус,
    битое тело, исключение транспорта) — пропуск, а не падение всего
    сигнала. ``on_progress((i+1)/len(countries), "jobs <country>")`` после
    каждой страны — так прогресс задачи доезжает до UI через SSE.
    """
    if not (app_id and app_key):
        # «Не настроен» — не ошибка: ни одного запроса, пустой результат.
        return JobsSignal(total=0, by_country=(), configured=False, error=None)

    rows: list[JobsByCountry] = []
    total = 0
    ok = 0
    for index, country in enumerate(countries):
        url = ADZUNA_URL_TEMPLATE.format(
            country=quote(country, safe=""),
            app_id=quote(str(app_id), safe=""),
            app_key=quote(str(app_key), safe=""),
            what=quote(str(query), safe=""),
        )
        try:
            status, body = transport(url, {"Accept": "application/json"}, timeout)
        except Exception:  # noqa: BLE001 - сбой транспорта на одной стране пропускаем
            status, body = 0, b""
        count = _parse_count(status, body)
        if count is not None:
            ok += 1
            total += count
            if count > 0:
                rows.append(
                    JobsByCountry(
                        country=country,
                        count=count,
                        label=COUNTRY_LABELS.get(country, country),
                    )
                )
        if on_progress is not None:
            on_progress((index + 1) / len(countries), f"jobs {country}")

    if countries and ok == 0:
        # Ключ есть, а данных нет ни по одной стране — это «ошибка», и
        # строка error отличает её от «не настроен» в UI и в detail.
        return JobsSignal(total=0, by_country=(), configured=True, error=ALL_FAILED)

    # Топ-N по убыванию; tie-break по коду страны — ответ детерминирован.
    rows.sort(key=lambda row: (-row.count, row.country))
    return JobsSignal(
        total=total,
        by_country=tuple(rows[: max(0, int(top_n))]),
        configured=True,
        error=None,
    )


def _parse_count(status: int, body: bytes) -> int | None:
    """``count`` из ответа Adzuna; ``None`` — ответ непригоден.

    ``200`` с JSON-объектом и целым ``count`` — успех. Всё остальное —
    ``None``: ``429`` (лимит 25/мин), ``5xx`` (сервис упал), прочие ``4xx``
    (нет такого ключа/слова), пустое или битое тело. Считать вакансии по
    неполному ответу нельзя; ``bool`` (JSON ``true``) числом не считается —
    как в ``app/web.py`` и ``core/collectors/momentum.py``.
    """
    if status != 200:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    count = payload.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def default_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    """Настоящий HTTP через ``urllib``; ошибка сети — ``(код, b'')``, не исключение.

    В тестах не используется — там инжектируется фейк (модуль чистый).
    ``HTTPError`` несёт код ответа (429/404) и уходит в
    :func:`_parse_count` как обычно; ``URLError`` (сеть недоступна,
    DNS) -> ``(0, b'')`` — тоже «пропустить страну», а не уронить сигнал.
    Значения ключей никуда не печатаются: URL с ``app_id``/``app_key`` не
    попадает ни в лог, ни в сообщения об ошибках.
    """
    request = Request(url, headers=dict(headers or {}), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL из шаблона
            return int(response.status or 200), response.read()
    except HTTPError as error:
        return int(error.code), b""
    except (URLError, OSError):
        return 0, b""
