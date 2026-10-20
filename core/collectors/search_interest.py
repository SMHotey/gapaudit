"""Search interest — динамика и регионы поискового интереса (P5d, §18.3).

Последний сигнал P5 с ключами и единственный, у которого **два источника**:

* **Yandex Wordstat** — бесплатно, по заявке; OAuth-токен + ``client_id``.
  Даёт частотность и срез по регионам РФ (``POST /v1/topRequests``).
* **Google Trends через Apify** — платно, сразу; ``api_token`` + ``actor_id``.
  Даёт нормализованный индекс 0–100, разбивку по странам и ряд за 12 месяцев.

Официальный Google Trends API (alpha по заявке, §14) в P5d не реализован:
провайдер ``google_trends`` заведён в ``core/api_keys.py`` и виден в
Настройках, но коллектора у него нет — CLI отвечает на такой ключ
честным сообщением «official alpha not implemented yet», а не пустым
сигналом (см. ``cli/signals.py``).

Контракт модуля — как у ``core/collectors/jobs.py``, ``patents.py`` и
``funding.py`` (AGENTS gotcha 50):

* **чистые функции** :func:`compute_wordstat`, :func:`compute_google_trends`
  и обёртка :func:`compute_search_interest` с инжектируемым транспортом:
  ключи приходят параметрами (их читает ``cli/signals.py`` через
  ``ApiKeysStore.get``), ни файловой системы, ни сети на импорте нет;
* **источники независимы**: нет ключа источника — этот источник
  ``configured=False`` (пустой результат, ``error=None``, транспорт не
  дёргается ни разу), второй при этом собирается; оба настроены — оба и
  собираются (не «или», а «и»);
* **источник никогда не ``None``**: даже если он не настроен, это объект
  :class:`SearchInterestSource` с ``configured=False``. ``None`` в
  :class:`SearchInterestSignal` остаётся только для «источник вообще не
  запрашивали» (ручное конструирование), и потребитель обязан это терпеть;
* **три состояния graceful degradation не смешиваются** (как в P5a–P5c):

  * ключа нет — ``configured=False``, ``error=None``: «не настроен»;
  * ключ есть, часть регионов/запросов упала (429/5xx/сеть) —
    ``error=None``, ``total`` по успешным;
  * ключ есть, упало всё — ``error='all requests failed'``,
    ``configured=True``, ``total=0``: «ошибка», а не «не настроен».

Формат Wordstat проверен по документации Яндекса (``/v1/topRequests``,
20.10.2026): POST с ``Content-type: application/json`` и заголовком
``Authorization: Bearer <oauth>``; тело ``{"phrase", "regions": [id],
"devices": ["all"]}``; ответ ``{"topRequests": [{"phrase", "count"}],
"totalCount": N}``. Два отличия от псевдокода шага, оба в пользу
документации: параметр называется **``regions``** и это СПИСОК
идентификаторов (в шаге — ``"region": region_id`` строкой), а
``client_id`` в запросе не передаётся вовсе — он привязан к токену при
регистрации приложения у Яндекса. ``client_id`` остаётся обязательным
полем учётных данных (``PROVIDERS["yandex_wordstat"]``) и условием
«настроен»: без заявки на доступ токен бесполезен.

Формат Apify — ``run-sync-get-dataset-items`` возвращает **датасет
актора**, то есть список элементов ``[{"term", "interestOverTime",
"interestByRegion"}]``. Поле ``term`` и имена ``interest*`` — контракт
конкретного актора (Google Trends Scraper), поэтому парсер терпим к
вариантам: ``interestOverTime`` принимает и список чисел, и список
``{"date", "value"}``; ``interestByRegion`` — ``{"geoName", "value"}``
или ``{"geo", "count"}``. Фикстуры синтетические (реальный ответ —
follow-up с ключом Apify, §0.5 шага).

Тренд: у Google Trends — по ряду ``interestOverTime``
(:func:`_trend_from_series`). У Wordstat ``trend_pct = None``: ``topRequests``
отдаёт только последние 30 дней, а ряд за 12 месяцев живёт в ``/v1/dynamics``
— отдельный метод с собственной формой ответа и ещё одним расходом квоты
на регион; в P5d он не реализован (отклонение зафиксировано в отчёте шага
и в DEVPLAN §18.3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

__all__ = [
    "ALL_FAILED",
    "APIFY_URL_TEMPLATE",
    "DEFAULT_TIMEOUT",
    "GOOGLE_TRENDS_TIMEOUT",
    "WORDSTAT_REGIONS",
    "WORDSTAT_URL",
    "SearchInterestPoint",
    "SearchInterestSignal",
    "SearchInterestSource",
    "Transport",
    "compute_google_trends",
    "compute_search_interest",
    "compute_wordstat",
    "default_post_transport",
]

#: Endpoint Wordstat: популярные запросы за последние 30 дней по фразе.
#: POST, JSON-тело, OAuth-токен в заголовке ``Authorization``.
WORDSTAT_URL = "https://api.wordstat.yandex.net/v1/topRequests"

#: Регионы РФ, по которым собирается срез (идентификаторы Яндекс.Гео).
#: В P5d — фиксированный набор пяти крупнейших: один запрос на регион,
#: поэтому расширение набора прямо увеличивает расход квоты. Полный
#: список регионов даёт ``/v1/getRegionsTree``; выбирать регионы в UI —
#: P5f (Niche Finder), не P5d.
WORDSTAT_REGIONS: dict[str, str] = {
    "225": "Россия",
    "1": "Москва и область",
    "2": "Санкт-Петербург",
    "65": "Новосибирская область",
    "47": "Ленинградская область",
}

#: Endpoint Apify: синхронный запуск актора с возвратом элементов датасета.
#: Токен — в query (форма самого Apify: ``?token=...``); URL с токеном
#: нельзя ни логировать, ни печатать.
APIFY_URL_TEMPLATE = (
    "https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"
    "?token={api_token}"
)

#: Таймаут запроса Wordstat, сек (сервис отвечает быстро, но квота — 10/сек).
DEFAULT_TIMEOUT = 20.0

#: Таймаут запуска актора Apify, сек: актор ходит в Google Trends сам,
#: поэтому запас больше, чем у прямого API.
GOOGLE_TRENDS_TIMEOUT = 30.0

#: Ошибка, когда ключ источника есть, но ни один запрос не дал данных.
ALL_FAILED = "all requests failed"

#: Транспорт POST: ``(url, headers, timeout, body | None) -> (status, bytes)``
#: — та же форма, что ``PostTransport`` в ``core/collectors/patents.py``
#: (оба источника ходят POST с JSON-телом; P5b уже ввёл этот тип, чтобы
#: не расширять GET-транспорт P5a).
Transport = Callable[[str, dict, float, bytes | None], tuple[int, bytes]]


@dataclass(frozen=True)
class SearchInterestPoint:
    """Одна точка сигнала: регион и значение интереса.

    ``value`` — счётчик запросов (Wordstat) или нормализованный индекс
    0–100 (Google Trends); ``None`` — регион пришёл без числа (актор
    отдал пустое значение), и UI обязан показать «—», а не 0.
    ``trend_pct`` — тренд по региону, если источник его даёт (в P5d
    Google Trends даёт тренд только по фразе целиком, поэтому у точек
    он ``None``; поле оставлено, чтобы не менять контракт при появлении
    порегиональной динамики).
    """

    region: str
    value: int | None
    trend_pct: float | None

    def as_dict(self) -> dict[str, Any]:
        """Плоский словарь для JSON / ``signal_observations.detail``."""
        return {
            "region": self.region,
            "value": self.value,
            "trend_pct": self.trend_pct,
        }


@dataclass(frozen=True)
class SearchInterestSource:
    """Один источник сигнала: Wordstat или Google Trends (§18.3).

    ``total`` — суммарный интерес (сумма счётчиков по регионам у Wordstat;
    сумма ряда ``interestOverTime`` у Google Trends — «накопленный индекс
    за 12 месяцев»), ``None`` — источнику это поле не свойственно или
    настройки нет. При «упало всё» (``error=ALL_FAILED``) ``total=0``, как
    у ``jobs``/``patents``/``funding``: пустой сбор отличим от «ключа нет».
    ``trend_pct`` — изменение за 12 месяцев в процентах или
    ``None``. ``configured`` — был ли полный набор учётных данных на
    момент запроса; ``error`` — есть ли ошибка при наличии ключа
    (``None`` + ``configured=False`` — «не настроен», не ошибка).
    """

    source: str  # 'wordstat' | 'google_trends'
    points: tuple[SearchInterestPoint, ...]
    total: int | None
    trend_pct: float | None
    configured: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON для ``--json-out`` / ``/api/signals`` и карточки пакета."""
        return {
            "source": self.source,
            "points": [point.as_dict() for point in self.points],
            "total": self.total,
            "trend_pct": self.trend_pct,
            "configured": self.configured,
            "error": self.error,
        }


@dataclass(frozen=True)
class SearchInterestSignal:
    """Сигнал search_interest для одного пакета: оба источника сразу (§18.3).

    ``wordstat``/``google_trends`` — по объекту на источник, включая
    ненастроенный (``configured=False``). ``as_dict`` добавляет два
    агрегата, которых нет у одиночных сигналов P5a–P5c:

    * ``total`` — сумма ``total`` обоих источников: ``app/web.py`` пишет
      его в ``signal_observations.value`` (колонка числовая, и от неё
      зависят будущие сортировки/агрегации);
    * ``configured`` — настроен ли хотя бы один источник (без него
      общий помощник ``_signal_payload`` не смог бы отличить «не
      настроен» от «собран»);
    * ``error`` — первая непустая ошибка источника: карточке достаточно
      одной строки, а подробности лежат в блоках источников.
    """

    query: str
    wordstat: SearchInterestSource | None
    google_trends: SearchInterestSource | None

    def as_dict(self) -> dict[str, Any]:
        """JSON для ``--json-out`` / ``/api/signals`` и карточки пакета."""
        sources = [
            source
            for source in (self.wordstat, self.google_trends)
            if source is not None
        ]
        return {
            "query": self.query,
            "wordstat": self.wordstat.as_dict() if self.wordstat else None,
            "google_trends": (
                self.google_trends.as_dict() if self.google_trends else None
            ),
            "total": sum(source.total or 0 for source in sources),
            "configured": any(source.configured for source in sources),
            "error": next(
                (source.error for source in sources if source.error), None
            ),
        }


def compute_wordstat(
    query: str,
    *,
    token: str | None,
    client_id: str | None,
    transport: Transport,
    regions: tuple[str, ...] = tuple(WORDSTAT_REGIONS),
    top_n: int = 5,
    timeout: float = DEFAULT_TIMEOUT,
    on_progress: Callable[[float, str], None] | None = None,
) -> SearchInterestSource:
    """Интерес в Яндексе по регионам РФ: POST ``/v1/topRequests`` на каждый регион.

    ``query`` — ключевая фраза (обычно имя пакета). ``token`` + ``client_id``
    — учётные данные Wordstat (оба обязательны: без заявки на доступ токен
    бесполезен); пусто — «не настроен» (см. докстринг модуля), транспорт
    при этом НЕ дёргается ни разу.

    ``regions`` — идентификаторы Яндекс.Гео (ключи :data:`WORDSTAT_REGIONS`),
    ``top_n`` — сколько регионов оставить в ``points`` (по убыванию
    интереса; ``total`` считается по ВСЕМ успешным регионам, а не по
    top-N). ``transport`` инжектируется (тесты передают фейк с фикстурой);
    реальный — :func:`default_post_transport`. Ошибка одного региона —
    пропуск, а не падение всего источника.

    ``on_progress((i+1)/len(regions), "wordstat <регион>")`` после каждого
    региона — прогресс задачи доезжает до UI через SSE.
    ``trend_pct`` всегда ``None``: ``topRequests`` отдаёт срез за 30 дней,
    ряда за 12 месяцев у него нет (см. докстринг модуля).
    """
    if not _provided(token) or not _provided(client_id):
        # «Не настроен» — не ошибка: ни одного запроса, пустой результат.
        return SearchInterestSource(
            source="wordstat",
            points=(),
            total=None,
            trend_pct=None,
            configured=False,
            error=None,
        )

    headers = {
        # Токен — только заголовком: URL попадает в логи и историю.
        "Authorization": f"Bearer {str(token).strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    points: list[SearchInterestPoint] = []
    ok = 0
    for index, region in enumerate(regions):
        body = json.dumps(_wordstat_body(query, region), ensure_ascii=False).encode(
            "utf-8"
        )
        try:
            status, raw = transport(WORDSTAT_URL, headers, timeout, body)
        except Exception:  # noqa: BLE001 - сбой транспорта на регионе пропускаем
            status, raw = 0, b""
        parsed = _parse_wordstat(status, raw)
        if parsed is not None:
            ok += 1
            points.append(
                SearchInterestPoint(
                    region=WORDSTAT_REGIONS.get(region, region),
                    value=parsed["totalCount"],
                    trend_pct=None,
                )
            )
        if on_progress is not None and regions:
            on_progress(
                (index + 1) / len(regions),
                f"wordstat {WORDSTAT_REGIONS.get(region, region)}",
            )

    if regions and ok == 0:
        # Ключ есть, а данных нет ни по одному региону — «ошибка», а не
        # «не настроен»: в карточке это разные строки.
        return SearchInterestSource(
            source="wordstat",
            points=(),
            total=0,
            trend_pct=None,
            configured=True,
            error=ALL_FAILED,
        )

    ordered = tuple(
        sorted(points, key=lambda point: point.value or 0, reverse=True)[:top_n]
    )
    return SearchInterestSource(
        source="wordstat",
        points=ordered,
        total=sum(point.value or 0 for point in points),
        trend_pct=None,
        configured=True,
        error=None,
    )


def compute_google_trends(
    query: str,
    *,
    api_token: str | None,
    actor_id: str | None,
    transport: Transport,
    timeout: float = GOOGLE_TRENDS_TIMEOUT,
    on_progress: Callable[[float, str], None] | None = None,
) -> SearchInterestSource:
    """Глобальный интерес: один синхронный запуск актора Apify.

    ``api_token`` + ``actor_id`` (``owner/actor-name``) — учётные данные
    Apify; любого не хватает — «не настроен», транспорт не дёргается.
    Тело запроса — ``{"searchTerms": [query], "geo": "", "timeRange":
    "today 12-m"}``: пустой ``geo`` = весь мир, окно тренда — 12 месяцев.

    Ответ — датасет актора (список элементов). Берётся первый элемент:
    ``points`` из ``interestByRegion`` (по убыванию значения, без
    обрезки — top-5 режет UI), ``trend_pct`` из ``interestOverTime``,
    ``total`` — сумма ряда (накопленный индекс за окно). Пустой датасет,
    не-список и ошибка сети — ``error='all requests failed'`` с
    ``configured=True``: ключ есть, данных нет.

    ``on_progress(1.0, "google_trends")`` — один вызов: запуск актора
    неделим, промежуточного прогресса у него нет.
    """
    if not _provided(api_token) or not _provided(actor_id):
        return SearchInterestSource(
            source="google_trends",
            points=(),
            total=None,
            trend_pct=None,
            configured=False,
            error=None,
        )

    url = APIFY_URL_TEMPLATE.format(
        # Актор — «owner/actor-name», поэтому слэш остаётся частью пути.
        actor_id=quote(str(actor_id).strip(), safe="/"),
        api_token=quote(str(api_token).strip(), safe=""),
    )
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps(
        {"searchTerms": [str(query)], "geo": "", "timeRange": "today 12-m"},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        status, raw = transport(url, headers, timeout, body)
    except Exception:  # noqa: BLE001 - сбой транспорта = пустой датасет
        status, raw = 0, b""
    items = _parse_apify(status, raw)
    if on_progress is not None:
        on_progress(1.0, "google_trends")

    first = items[0] if items and isinstance(items[0], dict) else None
    if first is None:
        # Пустой датасет — это не «нет интереса», а неудачный запуск:
        # актор вернул 200 без элементов (пустой ввод, лимит актора).
        return SearchInterestSource(
            source="google_trends",
            points=(),
            total=0,
            trend_pct=None,
            configured=True,
            error=ALL_FAILED,
        )

    series = _series_of(first.get("interestOverTime"))
    return SearchInterestSource(
        source="google_trends",
        points=_region_points(first.get("interestByRegion")),
        total=int(round(sum(series))) if series else None,
        trend_pct=_trend_from_series(series),
        configured=True,
        error=None,
    )


def compute_search_interest(
    query: str,
    *,
    wordstat_token: str | None = None,
    wordstat_client_id: str | None = None,
    apify_token: str | None = None,
    apify_actor_id: str | None = None,
    transport: Transport,
    top_n: int = 5,
    timeout: float = DEFAULT_TIMEOUT,
    google_timeout: float = GOOGLE_TRENDS_TIMEOUT,
    on_progress: Callable[[float, str], None] | None = None,
) -> SearchInterestSignal:
    """Оба источника в одном сигнале: Wordstat и Google Trends (Apify).

    Обёртка над :func:`compute_wordstat` и :func:`compute_google_trends`
    для CLI: ключи обоих источников приходят четырьмя параметрами, каждый
    источник собирается независимо (оба настроены — собираются оба).
    Прогресс делится пополам: Wordstat занимает первую половину шкалы,
    Google Trends — вторую, поэтому одна задача не «дёргает» прогресс
    назад, когда настроен только второй источник.
    """
    wordstat_progress: Callable[[float, str], None] | None = None
    google_progress: Callable[[float, str], None] | None = None
    if on_progress is not None:
        def on_wordstat(fraction: float, message: str) -> None:
            on_progress(0.5 * fraction, message)

        def on_google(fraction: float, message: str) -> None:
            on_progress(0.5 + 0.5 * fraction, message)

        wordstat_progress = on_wordstat
        google_progress = on_google

    wordstat = compute_wordstat(
        query,
        token=wordstat_token,
        client_id=wordstat_client_id,
        transport=transport,
        top_n=top_n,
        timeout=timeout,
        on_progress=wordstat_progress,
    )
    google = compute_google_trends(
        query,
        api_token=apify_token,
        actor_id=apify_actor_id,
        transport=transport,
        timeout=google_timeout,
        on_progress=google_progress,
    )
    return SearchInterestSignal(query=str(query), wordstat=wordstat, google_trends=google)


def _provided(value: str | None) -> bool:
    """Заполнено ли поле учётных данных (``None``/пробелы — нет)."""
    return bool(value) and bool(str(value).strip())


def _wordstat_body(query: str, region: str) -> dict[str, Any]:
    """JSON-тело запроса одного региона.

    ``regions`` — СПИСОК идентификаторов (документация ``/v1/topRequests``;
    псевдокод шага P5d писал ``"region": region_id`` строкой — расхождение
    снято в пользу документации). ``devices: ["all"]`` — без срезов по
    устройствам; ``numPhrases`` не задаём: нужен только ``totalCount``.
    """
    try:
        region_id: Any = int(region)
    except (TypeError, ValueError):
        # Нестандартный идентификатор оставляем строкой — сервер сам решит.
        region_id = str(region)
    return {"phrase": str(query), "regions": [region_id], "devices": ["all"]}


def _parse_wordstat(status: int, body: bytes) -> dict | None:
    """``200`` → ``{"totalCount": N, "topRequests": [...]}``; иначе ``None``.

    Всё остальное — ``None``: ``401/403`` (неверный или просроченный токен),
    ``429`` (лимит: ``Quota limit exceeded … Time to refill: N seconds``,
    §14), ``5xx``, пустое или битое тело.

    ``totalCount`` — общее число запросов с фразой (документация Яндекса).
    Если сервер его не прислал (форма ответа без него встречается в
    примерах документации), берём сумму ``count`` из ``topRequests``.
    Пустой объект ``{}`` даёт ``None`` — «регион пропущен», а не «интерес
    равен нулю»: ноль приходит явным ``totalCount: 0``.
    """
    if status != 200:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("topRequests")
    if not isinstance(rows, list):
        rows = []
    total = payload.get("totalCount")
    if isinstance(total, bool) or not isinstance(total, int):
        total = None
    if total is None:
        counts = [
            row["count"]
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("count"), int)
            and not isinstance(row.get("count"), bool)
        ]
        if not counts:
            return None
        total = sum(counts)
    return {
        "totalCount": total,
        "topRequests": [row for row in rows if isinstance(row, dict)],
    }


def _parse_apify(status: int, body: bytes) -> list | None:
    """``200`` → список элементов датасета; иначе ``None``.

    ``run-sync-get-dataset-items`` отвечает массивом. Всё остальное —
    ``None``: ``401/403`` (токен), ``400`` (неверный ``actor_id`` или
    тело), ``429`` (лимит актора), ``5xx``, объект с описанием ошибки
    (Apify отдаёт ``{"error": {...}}``) и битое тело.
    """
    if status != 200:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    return payload


def _series_of(raw: Any) -> list[float]:
    """Ряд ``interestOverTime`` → список чисел.

    Акторы Google Trends отдают либо список чисел, либо список объектов
    ``{"date": ..., "value": N}`` — принимаем обе формы (контракт актора
    не наш, и версии его меняют). Нечисловые и булевы значения
    пропускаются: ось времени без значения бесполезна для тренда.
    """
    if not isinstance(raw, list):
        return []
    series: list[float] = []
    for item in raw:
        value: Any = item
        if isinstance(item, dict):
            value = item.get("value")
            if value is None:
                value = item.get("count")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        series.append(float(value))
    return series


def _region_points(raw: Any) -> tuple[SearchInterestPoint, ...]:
    """``interestByRegion`` → точки по убыванию интереса.

    Имена полей принимаются в двух формах (``geoName``/``geo`` и
    ``value``/``count``), потому что разные акторы Apify называют их
    по-разному. Регион без имени подписывается ``?`` — терять точку
    из-за подписи хуже, чем показать её без названия.
    """
    if not isinstance(raw, list):
        return ()
    points: list[SearchInterestPoint] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        region = (
            item.get("geoName")
            or item.get("geo")
            or item.get("region")
            or "?"
        )
        value: Any = item.get("value")
        if value is None:
            value = item.get("count")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = None
        points.append(
            SearchInterestPoint(
                region=str(region),
                value=int(value) if value is not None else None,
                trend_pct=None,
            )
        )
    return tuple(sorted(points, key=lambda point: point.value or 0, reverse=True))


def _trend_from_series(series: list[int | float]) -> float | None:
    """Тренд ряда в процентах: ``(последнее − первое) / первое · 100``.

    ``None``, если точек меньше двух (сравнивать нечего) или первая точка
    нулевая (делить не на что). Отрицательное значение — падение; ноль —
    «без изменений», а не «нет данных» (отличимо от ``None``).
    """
    if len(series) < 2:
        return None
    first = float(series[0])
    if first == 0:
        return None
    return (float(series[-1]) - first) / first * 100.0


def default_post_transport(
    url: str,
    headers: dict,
    timeout: float,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    """Настоящий POST через ``urllib``; ошибка сети — ``(код, b'')``, не исключение.

    В тестах не используется — там инжектируется фейк (модуль чистый).
    ``HTTPError`` несёт код ответа (401 токен, 429 лимит, 400 тело) и уходит
    в парсеры как обычно; ``URLError`` (сеть недоступна, DNS) -> ``(0, b'')``
    — «пропустить регион»/«пустой датасет», а не уронить сигнал. Значения
    токенов никуда не печатаются: ни заголовок ``Authorization``, ни URL с
    ``?token=`` в сообщения об ошибках не попадают.
    """
    request = Request(url, data=body, headers=dict(headers or {}), method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL из константы
            return int(response.status or 200), response.read()
    except HTTPError as error:
        return int(error.code), b""
    except (URLError, OSError):
        return 0, b""
