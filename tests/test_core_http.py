"""Тесты HTTP-слоя: кэш, повторы, троттлинг, разбор тела (шаг 1a).

Никакой сети: транспорт подменяется фейком, ``sleep`` — заглушкой, кэш — DictCache.
Реальные ожидания сведены к одному тесту на 0.1 с (`test_throttle_wait_returns_positive_actual_wait`);
всё остальное время в тестах троттлинга идёт по виртуальным часам (:class:`VirtualClock`).
"""

from __future__ import annotations

import pytest

from core.cache import DictCache
from core.http import (
    CACHE_KEY_PREFIX,
    GITHUB_SEARCH_PER_MINUTE,
    HttpClient,
    Throttle,
    default_throttle,
)

URL = "https://example.test/api/thing"


class FakeTransport:
    """Транспорт из списка ответов: элемент — ``(status, body)`` или исключение."""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, headers, timeout))
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[return-value]

    @property
    def count(self) -> int:
        return len(self.calls)


class RecordingSleep:
    """Подмена time.sleep: запоминает запрошенные паузы и не спит."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class VirtualClock:
    """Виртуальные часы: ``sleep`` двигает время, реального ожидания нет.

    Тесты троттлинга не должны платить секундами за проверку интервала, поэтому
    и часы, и пауза инжектируются в :class:`Throttle` (``clock``/``sleep``).
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def fast_throttle() -> Throttle:
    """Троттл почти без паузы: 60000/мин = 1 мс на запрос."""
    return Throttle(per_minute={"example.test": 60000}, default_per_minute=60000)


def make_client(*responses: object, cache: DictCache | None = None) -> tuple[HttpClient, FakeTransport, RecordingSleep]:
    transport = FakeTransport(*responses)
    sleep = RecordingSleep()
    client = HttpClient(
        cache=DictCache() if cache is None else cache,
        throttle=fast_throttle(),
        transport=transport,
        sleep=sleep,
    )
    return client, transport, sleep


# 1. Успех и попадание в кэш ----------------------------------------------------------------


def test_200_json_is_parsed_and_cached() -> None:
    client, transport, _ = make_client((200, b'{"ok": true, "n": 1}'))

    first = client.get(URL)
    second = client.get(URL)

    assert first == (200, {"ok": True, "n": 1})
    assert second == first
    assert transport.count == 1
    assert transport.calls[0][1]["User-Agent"] == "ecosystem-gap-audit"


def test_cache_key_matches_legacy() -> None:
    cache = DictCache()
    client, _, _ = make_client((200, b"{}"), cache=cache)

    client.get(URL)

    assert cache.get(CACHE_KEY_PREFIX + URL) is not None


# 2. 404 — валидный отрицательный ответ -----------------------------------------------------


def test_404_is_negative_answer_and_is_cached() -> None:
    client, transport, _ = make_client((404, b"Not Found"))

    assert client.get(URL) == (404, None)
    assert client.get(URL) == (404, None)
    assert transport.count == 1


def test_410_is_cached_too() -> None:
    client, transport, _ = make_client((410, b""))
    assert client.get(URL) == (410, None)
    assert client.get(URL) == (410, None)
    assert transport.count == 1


def test_403_is_final_without_retry_and_not_cached() -> None:
    client, transport, sleep = make_client((403, b"forbidden"))

    assert client.get(URL) == (403, None)
    assert transport.count == 1
    assert sleep.delays == []


# 3-4. Повторы на 5xx -----------------------------------------------------------------------


def test_500_500_200_returns_success_after_three_calls() -> None:
    client, transport, sleep = make_client(
        (500, b"boom"), (500, b"boom"), (200, b'{"ok": true}')
    )

    assert client.get(URL) == (200, {"ok": True})
    assert transport.count == 3
    assert sleep.delays == [2.0, 4.0]


def test_500_three_times_returns_none_none() -> None:
    client, transport, sleep = make_client((500, b"boom"), (500, b"boom"), (500, b"boom"))

    assert client.get(URL) == (None, None)
    assert transport.count == 3
    assert sleep.delays == [2.0, 4.0]


def test_429_is_retried_with_backoff() -> None:
    client, transport, sleep = make_client((429, b""), (429, b""), (200, b'{"ok": true}'))

    assert client.get(URL) == (200, {"ok": True})
    assert transport.count == 3
    assert sleep.delays == [2.0, 4.0]


def test_backoff_schedule_reaches_20_and_stays_there() -> None:
    client, transport, sleep = make_client(*[(500, b"")] * 7)

    assert client.get(URL, tries=7) == (None, None)
    assert transport.count == 7
    assert sleep.delays == [2.0, 4.0, 8.0, 16.0, 20.0, 20.0]


# 5. Сетевые сбои ---------------------------------------------------------------------------


def test_timeout_three_times_returns_none_none() -> None:
    client, transport, sleep = make_client(TimeoutError("timed out"))

    assert client.get(URL) == (None, None)
    assert transport.count == 3
    assert sleep.delays == [2.0, 4.0]


def test_connection_error_is_retried() -> None:
    client, transport, _ = make_client(ConnectionResetError("reset"), (200, b'{"ok": 1}'))

    assert client.get(URL) == (200, {"ok": 1})
    assert transport.count == 2


# 6-7. Троттлинг ----------------------------------------------------------------------------


def test_throttle_600_per_minute_interval_is_100ms_on_virtual_clock() -> None:
    clock = VirtualClock()
    throttle = Throttle(per_minute={"example.test": 600}, clock=clock, sleep=clock.sleep)

    assert throttle.wait("example.test") == 0.0
    waited = throttle.wait("example.test")

    # Интервал при 600/мин — 0.1 с; ждём его целиком, но реально не спим.
    assert waited == pytest.approx(0.1)
    assert waited < 0.2
    assert clock.slept == [pytest.approx(0.1)]


def test_throttle_60_per_minute_waits_full_second_on_virtual_clock() -> None:
    clock = VirtualClock()
    throttle = Throttle(per_minute={"example.test": 60}, clock=clock, sleep=clock.sleep)

    assert throttle.wait("example.test") == 0.0
    waited = throttle.wait("example.test")

    # Обе границы интервала 60/мин: не меньше 0.9 с и меньше 1.5 с — ровно 1.0 с.
    assert waited >= 0.9
    assert waited < 1.5
    assert waited == pytest.approx(1.0)
    assert clock.slept == [pytest.approx(1.0)]


def test_throttle_wait_returns_positive_actual_wait() -> None:
    """Единственное реальное ожидание сюиты — 0.1 с; проверяются обе границы."""
    throttle = Throttle(per_minute={"example.test": 600})

    first = throttle.wait("example.test")
    second = throttle.wait("example.test")

    assert first == 0.0
    # Интервал при 600/мин — 0.1 с; возвращается именно выдержанная пауза.
    assert second > 0.05
    assert second < 0.2


def test_throttle_is_per_host() -> None:
    throttle = Throttle(per_minute={"a.test": 60, "b.test": 60})

    throttle.wait("a.test")
    assert throttle.wait("b.test") == 0.0


def test_throttle_uses_virtual_clock() -> None:
    now = [1000.0]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = Throttle(per_minute={"h.test": 60}, clock=clock, sleep=sleep)
    assert throttle.wait("h.test") == 0.0
    assert throttle.wait("h.test") == pytest.approx(1.0)
    assert slept == [pytest.approx(1.0)]


def test_client_calls_throttle_before_each_attempt() -> None:
    hosts: list[str] = []
    throttle = Throttle(per_minute={"example.test": 6000})

    original_wait = throttle.wait

    def wait(host: str) -> float:
        hosts.append(host)
        return original_wait(host)

    throttle.wait = wait  # type: ignore[method-assign]
    transport = FakeTransport((500, b""), (200, b"{}"))
    client = HttpClient(cache=DictCache(), throttle=throttle, transport=transport, sleep=lambda _s: None)

    assert client.get(URL) == (200, {})
    assert hosts == ["example.test", "example.test"]


def test_get_calls_throttle_wait() -> None:
    """§0.3 шага 1d: ``HttpClient.get`` ждёт троттл перед транспортом.

    Часы и пауза виртуальные, поэтому проверяется ровно выдержанный интервал
    60/мин = 1 с, а не реальное время; кэш выключен, чтобы дошло до транспорта.
    """
    clock = VirtualClock()
    throttle = Throttle(per_minute={"example.test": 60}, clock=clock, sleep=clock.sleep)
    transport = FakeTransport((200, b"{}"))
    client = HttpClient(
        cache=DictCache(), throttle=throttle, transport=transport, sleep=lambda _s: None
    )

    assert client.get(URL) == (200, {})
    assert clock.now == 1000.0  # первый запрос лимит не ждёт
    assert client.get(URL, use_cache=False) == (200, {})

    assert transport.count == 2
    assert clock.now - 1000.0 >= 1.0
    assert clock.now - 1000.0 == pytest.approx(1.0)
    assert clock.slept == [pytest.approx(1.0)]


def test_default_throttle_rates() -> None:
    throttle = default_throttle()

    assert throttle.rate_for("api.github.com") == 60
    assert throttle.rate_for("pypistats.org") == 30
    assert throttle.rate_for("pypi.org") == 60
    # GitHub Search — тот же хост, лимит выставляет вызывающий (шаг 1b).
    throttle.set_rate("api.github.com", GITHUB_SEARCH_PER_MINUTE)
    assert throttle.rate_for("api.github.com") == 30


# 8. use_cache=False ------------------------------------------------------------------------


def test_use_cache_false_forces_network() -> None:
    client, transport, _ = make_client((200, b'{"n": 1}'), (200, b'{"n": 2}'))

    assert client.get(URL) == (200, {"n": 1})
    assert client.get(URL, use_cache=False) == (200, {"n": 2})
    assert transport.count == 2
    # Принудительный запрос всё равно обновляет кэш — им пользуется UI «обновить».
    assert client.get(URL) == (200, {"n": 2})


# 9. raw=True -------------------------------------------------------------------------------


def test_raw_json_is_parsed_and_cached() -> None:
    cache = DictCache()
    client, transport, _ = make_client((200, b'{"a": [1, 2]}'), cache=cache)

    assert client.get(URL, raw=True) == (200, {"a": [1, 2]})
    assert client.get(URL) == (200, {"a": [1, 2]})
    assert transport.count == 1


def test_raw_non_json_returns_bytes_and_is_not_cached() -> None:
    cache = DictCache()
    client, transport, _ = make_client((200, b"<html>not json</html>"), cache=cache)

    status, body = client.get(URL, raw=True)
    assert status == 200
    assert body == b"<html>not json</html>"
    assert cache.get(CACHE_KEY_PREFIX + URL) is None

    client.get(URL, raw=True)
    assert transport.count == 2


def test_non_json_without_raw_returns_none_and_is_not_cached() -> None:
    cache = DictCache()
    client, transport, _ = make_client((200, b"<html>oops</html>"), cache=cache)

    assert client.get(URL) == (200, None)
    assert cache.get(CACHE_KEY_PREFIX + URL) is None
    client.get(URL)
    assert transport.count == 2


# 10. Импорт без побочных эффектов ----------------------------------------------------------


def test_import_and_construction_touch_no_files(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    import core.cache as cache_module
    import core.http as http_module

    assert cache_module.FileCache("raw/.pcache.json").get("GET x") is None
    client = http_module.default_client()
    assert client.cache is not None

    assert list(tmp_path.iterdir()) == []


def test_empty_tries_is_one_attempt() -> None:
    client, transport, _ = make_client((200, b"{}"))
    assert client.get(URL, tries=0) == (200, {})
    assert transport.count == 1
