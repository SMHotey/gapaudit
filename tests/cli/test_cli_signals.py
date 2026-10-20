"""``cli.signals`` (шаг P5a, расширение P5b): обёртка ``python -m cli.signals collect``.

Проверяется протокол задачи (hello → progress → done), формат
``--json-out`` (``{"package", "signals": {"jobs"/"patents": ...}}``),
graceful degradation без ключа (``configured=False``, код 0), выбор вида
по ``--kind`` (P5b: ``jobs`` и ``patents``, без ``--kind`` — все виды) и
дедупликация ``--kind``.

Сеть — ноль: ``compute_jobs``/``compute_patents`` подменены фейками, а
реальные транспорты в тестах без ключа проверяются «не дёргаются вовсе»
(ключи читаются из ``<data_dir>/api_keys.json``, которого в tmp нет —
AGENTS gotcha 61).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from cli import signals
from core.api_keys import ApiKeysStore
from core.collectors.jobs import JobsByCountry, JobsSignal
from core.collectors.funding import FundingQuarter, FundingSignal
from core.collectors.patents import PatentsSignal, PatentsYearBucket
from core.collectors.search_interest import (
    SearchInterestPoint,
    SearchInterestSignal,
    SearchInterestSource,
)

@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Каталог данных и cwd — во временный каталог (сеть по умолчанию нет)."""
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def protocol(out: str) -> list[dict]:
    """Разобрать stdout как JSON-lines: каждая непустая строка — валидный JSON."""
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def fake_signal(**overrides: object) -> JobsSignal:
    """Собранный jobs-сигнал для фейка compute_jobs."""
    fields: dict = {
        "total": 42,
        "by_country": (JobsByCountry(country="de", count=42, label="Germany"),),
        "configured": True,
        "error": None,
    }
    fields.update(overrides)
    return JobsSignal(**fields)


def test_collect_job_json_emits_protocol_and_writes_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """hello → progress (по стране) → done; json-out — package + signals.jobs."""
    def fake_compute(query: str, *, app_id, app_key, transport, on_progress=None,
                     **kwargs: object) -> JobsSignal:
        assert query == "requests"
        if on_progress:
            on_progress(1.0, "jobs de")
        return fake_signal()

    monkeypatch.setattr(signals, "compute_jobs", fake_compute)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "jobs",
        "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    messages = protocol(captured.out)
    assert messages[0]["type"] == "hello"
    assert messages[-1] == {"type": "done"}
    progress = [message for message in messages if message["type"] == "progress"]
    assert progress and progress[-1]["message"] == "jobs de"

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["package"] == "requests"
    assert payload["signals"]["jobs"] == {
        "total": 42,
        "by_country": [{"country": "de", "count": 42, "label": "Germany"}],
        "configured": True,
        "error": None,
    }
    # таблица и итог ушли в stderr (лог задачи), stdout — только протокол
    assert "сигналы собраны" not in captured.out


def test_collect_without_key_is_configured_false_and_exit_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Пустой Store ключей — configured=False, код 0: «не настроен» не ошибка.

    Транспорт compute_jobs заменён на взрывающийся: без ключа он не должен
    дёргаться (контракт core/collectors/jobs.py).
    """
    def must_not_be_called(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        raise AssertionError("транспорт без ключа не должен дёргаться")

    monkeypatch.setattr(signals, "default_transport", must_not_be_called)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert protocol(captured.out)[-1] == {"type": "done"}
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["signals"]["jobs"] == {
        "total": 0, "by_country": [], "configured": False, "error": None,
    }


def test_unknown_kind_is_argparse_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--kind evil`` — argparse-ошибка, код 2 (та же конвенция, что у задач)."""
    with pytest.raises(SystemExit) as excinfo:
        signals.main(["collect", "--package", "x", "--kind", "evil"])

    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_duplicate_kind_is_deduplicated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--kind jobs --kind jobs`` — один jobs в результате, один вызов коллектора."""
    calls: list[str] = []

    def fake_compute(query: str, *, on_progress=None, **kwargs: object) -> JobsSignal:
        calls.append(query)
        return fake_signal()

    monkeypatch.setattr(signals, "compute_jobs", fake_compute)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "jobs", "--kind", "jobs",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    assert calls == ["requests"]
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload["signals"]) == ["jobs"]


def test_exception_in_collector_emits_error_and_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Исключение коллектора — строка error и код 2, а не трейсбек."""
    def boom(query: str, **kwargs: object) -> JobsSignal:
        raise RuntimeError("Adzuna взорвался")

    monkeypatch.setattr(signals, "compute_jobs", boom)

    code = signals.main(["collect", "--package", "x", "--job-json"])
    captured = capsys.readouterr()

    assert code == 2
    messages = protocol(captured.out)
    assert messages[0]["type"] == "hello"
    assert messages[-1]["type"] == "error"
    assert "Adzuna взорвался" in messages[-1]["error"]


def test_without_job_json_prints_summary_for_humans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Автономный запуск — таблица стран и итог в stdout, JSON-lines нет."""
    monkeypatch.setattr(signals, "compute_jobs",
                        lambda query, **kwargs: fake_signal())
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert "Germany" in captured.out
    assert "всего вакансий: 42" in captured.out
    assert '"type"' not in captured.out
    assert out_path.is_file()


def test_empty_package_is_exit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--package ""`` — ошибка тела: собирать сигналы «про что» неизвестно."""
    code = signals.main(["collect", "--package", "   ", "--job-json"])

    assert code == 2
    assert "package" in capsys.readouterr().err


def test_runner_argv_order_flags_before_command_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Порядок argv из ``_default_runner``: флаги ДО подкоманды.

    Живой прогон P5a поймал именно это: ``--job-json --json-out=X collect
    --package X`` падал «unrecognized arguments: collect», потому что
    подкоманда искалась только первым токеном. Пакет с именем «collect»
    при этом не должен съедать диспетчеризацию.
    """
    monkeypatch.setattr(signals, "compute_jobs",
                        lambda query, **kwargs: fake_signal())
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "--job-json", f"--json-out={out_path}",
        "collect", "--package", "requests", "--kind", "jobs",
    ])
    capsys.readouterr()

    assert code == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["package"] == "requests"

    # --package collect: значение флага, а не команда
    out_path2 = tmp_path / "signals2.json"
    code = signals.main([
        "--job-json", "--json-out", str(out_path2),
        "collect", "--package", "collect",
    ])
    capsys.readouterr()

    assert code == 0
    assert json.loads(out_path2.read_text(encoding="utf-8"))["package"] == "collect"


# --- patents (шаг P5b) ---------------------------------------------------------


def fake_patents_signal(**overrides: object) -> PatentsSignal:
    """Собранный patents-сигнал для фейка compute_patents."""
    fields: dict = {
        "total": 42,
        "by_year": (PatentsYearBucket(year=2025, count=42),),
        "impulse_pct": None,
        "configured": True,
        "error": None,
    }
    fields.update(overrides)
    return PatentsSignal(**fields)


def test_collect_patents_without_key_is_configured_false_and_exit_0(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--kind patents`` без ключа — configured=False, код 0, запросов нет.

    Транспорт ``compute_patents`` заменён на взрывающийся: без ключа он не
    должен дёргаться (контракт core/collectors/patents.py), и вида jobs
    в результате тоже нет — собран ровно запрошенный.
    """
    def must_not_be_called(url, headers, timeout, body=None):  # noqa: ANN001
        raise AssertionError("транспорт без ключа не должен дёргаться")

    monkeypatch.setattr(signals, "default_post_transport", must_not_be_called)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "patents",
        "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert protocol(captured.out)[-1] == {"type": "done"}
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload["signals"]) == ["patents"]
    assert payload["signals"]["patents"] == {
        "total": 0, "by_year": [], "impulse_pct": None,
        "configured": False, "error": None,
    }


def test_collect_patents_with_key_writes_patents_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ключ из api_keys.json (провайдер patentsview) уходит в compute_patents."""
    ApiKeysStore(tmp_path / "data").save("patentsview", {"api_key": "test-key"})
    seen: dict[str, object] = {}

    def fake_compute(query: str, *, api_key, transport, on_progress=None,
                     **kwargs: object) -> PatentsSignal:
        seen.update(query=query, api_key=api_key, transport=transport)
        if on_progress:
            on_progress(1.0, "patents 2025")
        return fake_patents_signal()

    monkeypatch.setattr(signals, "compute_patents", fake_compute)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "patents",
        "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["signals"]["patents"] == {
        "total": 42,
        "by_year": [{"year": 2025, "count": 42}],
        "impulse_pct": None,
        "configured": True,
        "error": None,
    }
    assert seen["query"] == "requests"
    assert seen["api_key"] == "test-key"
    assert seen["transport"] is signals.default_post_transport
    progress = [m for m in protocol(captured.out) if m["type"] == "progress"]
    assert progress and progress[-1]["message"] == "patents 2025"


def test_collect_jobs_and_patents_writes_both_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--kind jobs --kind patents`` — оба блока в одном --json-out."""
    monkeypatch.setattr(signals, "compute_jobs",
                        lambda query, **kwargs: fake_signal())
    monkeypatch.setattr(signals, "compute_patents",
                        lambda query, **kwargs: fake_patents_signal())
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests",
        "--kind", "jobs", "--kind", "patents",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload["signals"]) == ["jobs", "patents"]
    assert payload["signals"]["jobs"]["total"] == 42
    assert payload["signals"]["patents"]["total"] == 42


def test_without_kind_collects_every_signal_kind(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Без ``--kind`` собираются все виды SIGNAL_KINDS (ключей нет — все False).

    P5d: видов четыре (jobs, patents, funding, search_interest) — новый вид
    обязан попасть и в набор по умолчанию, иначе «собери всё» тихо
    перестало бы собирать search_interest.
    """
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload["signals"]) == ["jobs", "patents", "funding", "search_interest"]
    assert payload["signals"]["jobs"]["configured"] is False
    assert payload["signals"]["patents"]["configured"] is False
    assert payload["signals"]["funding"]["configured"] is False
    assert payload["signals"]["search_interest"]["configured"] is False


def test_summary_for_humans_prints_patents_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Автономный запуск: итог по patents — таблица лет, импульс и источник."""
    monkeypatch.setattr(signals, "compute_patents",
                        lambda query, **kwargs: fake_patents_signal(
                            impulse_pct=200.0,
                            by_year=(
                                PatentsYearBucket(year=2023, count=14),
                                PatentsYearBucket(year=2025, count=42),
                            ),
                        ))
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "patents",
        "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert "всего патентов: 42" in captured.out
    assert "импульс +200.0%" in captured.out
    assert '"type"' not in captured.out


# --- funding (шаг P5c) ---------------------------------------------------------


def fake_funding_signal(**overrides: object) -> FundingSignal:
    """Собранный funding-сигнал для фейка compute_funding."""
    fields: dict = {
        "total": 42,
        "by_quarter": (FundingQuarter(year=2026, quarter=2, count=42),),
        "trend_pct": None,
        "configured": True,
        "error": None,
    }
    fields.update(overrides)
    return FundingSignal(**fields)


def test_collect_funding_without_user_agent_is_configured_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--kind funding`` без User-Agent — configured=False, код 0, запросов нет.

    Транспорт заменён на взрывающийся: без User-Agent EDGAR не дёргается
    (контракт core/collectors/funding.py), и вида jobs в результате нет.
    """
    def must_not_be_called(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        raise AssertionError("транспорт без User-Agent не должен дёргаться")

    monkeypatch.setattr(signals, "funding_transport", must_not_be_called)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "funding",
        "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert protocol(captured.out)[-1] == {"type": "done"}
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload["signals"]) == ["funding"]
    assert payload["signals"]["funding"] == {
        "total": 0, "by_quarter": [], "trend_pct": None,
        "configured": False, "error": None,
    }


def test_collect_funding_uses_public_meta_not_get(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """User-Agent EDGAR читается ``public_meta`` — каналом только ``meta``.

    §0.7 шага утверждал, что ``get("edgar")`` вернёт ``{}`` (у провайдера
    ``encrypted_value`` пуст). Фактически ``get`` домешивает ``public_meta``,
    поэтому User-Agent в нём тоже есть — оба пути дают значение, и «молчаливо
    пустой» funding получился бы только у того, кто читает поле не из того
    места (например, ``encrypted_value``). CLI идёт через ``public_meta``:
    это точный канал (только ``meta``, без расшифровки), и тест фиксирует,
    что значение доходит до ``compute_funding``.
    """
    keys = ApiKeysStore(tmp_path / "data")
    keys.save("edgar", {"user_agent": "GapAudit test@example.com"})

    assert keys.public_meta("edgar") == {"user_agent": "GapAudit test@example.com"}
    assert keys.status("edgar").configured is True
    seen: dict[str, object] = {}

    def fake_compute(query: str, *, user_agent, transport, on_progress=None,
                     **kwargs: object) -> FundingSignal:
        seen.update(query=query, user_agent=user_agent, transport=transport)
        return fake_funding_signal()

    monkeypatch.setattr(signals, "compute_funding", fake_compute)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "funding",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    assert seen["user_agent"] == "GapAudit test@example.com"
    assert seen["transport"] is signals.funding_transport
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["signals"]["funding"]["configured"] is True


def test_collect_all_three_kinds_writes_three_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--kind jobs --kind patents --kind funding`` — три блока в одном файле."""
    monkeypatch.setattr(signals, "compute_jobs",
                        lambda query, **kwargs: fake_signal())
    monkeypatch.setattr(signals, "compute_patents",
                        lambda query, **kwargs: fake_patents_signal())
    monkeypatch.setattr(signals, "compute_funding",
                        lambda query, **kwargs: fake_funding_signal())
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests",
        "--kind", "jobs", "--kind", "patents", "--kind", "funding",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload["signals"]) == ["jobs", "patents", "funding"]


def test_without_kind_collects_funding_too(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Без ``--kind`` собираются все четыре вида SIGNAL_KINDS (ключей нет — все False)."""
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload["signals"]) == ["jobs", "patents", "funding", "search_interest"]
    assert payload["signals"]["funding"]["configured"] is False


def test_summary_for_humans_prints_funding_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Автономный запуск: таблица кварталов и тренд funding в stdout."""
    monkeypatch.setattr(signals, "compute_funding",
                        lambda query, **kwargs: fake_funding_signal(
                            total=300,
                            trend_pct=300.0,
                            by_quarter=(
                                FundingQuarter(year=2025, quarter=4, count=50),
                                FundingQuarter(year=2026, quarter=3, count=200),
                            ),
                        ))
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "funding",
        "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert "Q4 2025" in captured.out
    assert "всего filing'ов формы D: 300" in captured.out
    assert "тренд +300.0% за 4 квартала" in captured.out
    assert '"type"' not in captured.out


def test_summary_without_user_agent_points_to_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Нет User-Agent — итог говорит «не настроен», а не «0 раундов».

    ``configured=False`` — это не ошибка (код 0) и не пустой результат сбора:
    пользователю нужно действие «ввести User-Agent», а не «собрать заново».
    """
    monkeypatch.setattr(signals, "compute_funding",
                        lambda query, **kwargs: fake_funding_signal(
                            total=0, by_quarter=(), configured=False))
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "funding",
        "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert "User-Agent SEC EDGAR не настроен" in captured.out
    assert "всего filing'ов" not in captured.out


# --- search_interest (шаг P5d): два источника ---------------------------------


def fake_si_source(
    source: str, *, configured: bool, error: str | None = None,
    points: tuple[SearchInterestPoint, ...] = (),
) -> SearchInterestSource:
    """Один источник search_interest для фейка compute_search_interest."""
    return SearchInterestSource(
        source=source,
        points=points,
        total=sum(point.value or 0 for point in points) if points else None,
        trend_pct=None,
        configured=configured,
        error=error,
    )


def fake_si_signal(
    *, wordstat: bool = False, google: bool = False
) -> SearchInterestSignal:
    """Сигнал с двумя источниками в заданных состояниях (для фейка)."""
    return SearchInterestSignal(
        query="requests",
        wordstat=fake_si_source(
            "wordstat",
            configured=wordstat,
            points=(
                (SearchInterestPoint(region="Россия", value=42, trend_pct=None),)
                if wordstat else ()
            ),
        ),
        google_trends=fake_si_source(
            "google_trends",
            configured=google,
            points=(
                (SearchInterestPoint(region="RU", value=100, trend_pct=None),)
                if google else ()
            ),
        ),
    )


def test_collect_search_interest_without_keys_is_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--kind search_interest`` без ключей — оба источника False, код 0.

    Транспорт заменён на взрывающийся: без ключей ни Wordstat, ни Apify не
    дёргаются (контракт core/collectors/search_interest.py), и в файле
    ровно один блок — составной.
    """
    def must_not_be_called(url: str, headers: dict, timeout: float,
                           body: bytes | None = None) -> tuple[int, bytes]:
        raise AssertionError("транспорт без ключей не должен дёргаться")

    monkeypatch.setattr(signals, "search_interest_transport", must_not_be_called)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "search_interest",
        "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert protocol(captured.out)[-1] == {"type": "done"}
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload["signals"]) == ["search_interest"]
    block = payload["signals"]["search_interest"]
    assert block["wordstat"]["configured"] is False
    assert block["google_trends"]["configured"] is False
    assert block["configured"] is False


def test_collect_search_interest_with_wordstat_key_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ключ Wordstat — собран Wordstat, Google Trends «не настроен».

    ``get("yandex_wordstat")`` домешивает ``meta.client_id`` (P5c, §0.4),
    поэтому CLI передаёт в коллектор обе части учётных данных — тест
    фиксирует, что они дошли.
    """
    keys = ApiKeysStore(tmp_path / "data")
    keys.save("yandex_wordstat", {"token": "ws-token", "client_id": "ws-client"})
    assert keys.get("yandex_wordstat") == {
        "token": "ws-token", "client_id": "ws-client",
    }
    seen: dict[str, object] = {}

    def fake_compute(query: str, *, wordstat_token, wordstat_client_id,
                     apify_token, apify_actor_id, transport,
                     on_progress=None, **kwargs: object) -> SearchInterestSignal:
        seen.update(
            query=query, wordstat_token=wordstat_token,
            wordstat_client_id=wordstat_client_id,
            apify_token=apify_token, apify_actor_id=apify_actor_id,
            transport=transport,
        )
        return fake_si_signal(wordstat=True)

    monkeypatch.setattr(signals, "compute_search_interest", fake_compute)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "search_interest",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    assert seen["wordstat_token"] == "ws-token"
    assert seen["wordstat_client_id"] == "ws-client"
    assert seen["apify_token"] is None
    assert seen["apify_actor_id"] is None
    assert seen["transport"] is signals.search_interest_transport
    block = json.loads(out_path.read_text(encoding="utf-8"))["signals"]["search_interest"]
    assert block["wordstat"]["configured"] is True
    assert block["google_trends"]["configured"] is False


def test_collect_search_interest_with_apify_key_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ключ Apify — собран Google Trends, Wordstat «не настроен» (обратный случай)."""
    keys = ApiKeysStore(tmp_path / "data")
    keys.save("apify", {"api_token": "ap-token", "actor_id": "apify/google-trends"})
    seen: dict[str, object] = {}

    def fake_compute(query: str, *, wordstat_token, wordstat_client_id,
                     apify_token, apify_actor_id, transport,
                     on_progress=None, **kwargs: object) -> SearchInterestSignal:
        seen.update(wordstat_token=wordstat_token, apify_token=apify_token,
                    apify_actor_id=apify_actor_id)
        return fake_si_signal(google=True)

    monkeypatch.setattr(signals, "compute_search_interest", fake_compute)
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "search_interest",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    assert seen["apify_token"] == "ap-token"
    assert seen["apify_actor_id"] == "apify/google-trends"
    assert seen["wordstat_token"] is None
    block = json.loads(out_path.read_text(encoding="utf-8"))["signals"]["search_interest"]
    assert block["wordstat"]["configured"] is False
    assert block["google_trends"]["configured"] is True


def test_collect_search_interest_with_both_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Оба ключа — собраны оба источника (не «или», а «и») и оба видны в JSON."""
    keys = ApiKeysStore(tmp_path / "data")
    keys.save("yandex_wordstat", {"token": "ws-token", "client_id": "ws-client"})
    keys.save("apify", {"api_token": "ap-token", "actor_id": "apify/google-trends"})
    monkeypatch.setattr(signals, "compute_search_interest",
                        lambda query, **kwargs: fake_si_signal(wordstat=True, google=True))
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "search_interest",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    block = json.loads(out_path.read_text(encoding="utf-8"))["signals"]["search_interest"]
    assert block["wordstat"]["configured"] is True
    assert block["google_trends"]["configured"] is True
    assert block["configured"] is True
    assert block["wordstat"]["points"] == [
        {"region": "Россия", "value": 42, "trend_pct": None}
    ]
    assert block["google_trends"]["points"] == [
        {"region": "RU", "value": 100, "trend_pct": None}
    ]


def test_collect_jobs_and_search_interest_writes_both_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--kind jobs --kind search_interest`` — два блока в одном --json-out."""
    monkeypatch.setattr(signals, "compute_jobs",
                        lambda query, **kwargs: fake_signal())
    monkeypatch.setattr(signals, "compute_search_interest",
                        lambda query, **kwargs: fake_si_signal(wordstat=True))
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests",
        "--kind", "jobs", "--kind", "search_interest",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert list(payload["signals"]) == ["jobs", "search_interest"]
    assert payload["signals"]["jobs"]["total"] == 42
    assert payload["signals"]["search_interest"]["wordstat"]["configured"] is True


def test_official_google_trends_key_without_apify_is_explained_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ключ официального alpha без Apify — ``configured=False`` + причина в ``error``.

    Коллектора официального Google Trends в P5d нет (§14, вариант A — по
    заявке), и молчаливое «не настроен» на введённый ключ было бы ложью:
    CLI кладёт в источник human-readable причину, а ``app/web.py`` считает
    такой источник «есть что показать» и отдаёт статус ``ok`` (карточка
    рисует строку «Ошибка: …»).
    """
    keys = ApiKeysStore(tmp_path / "data")
    keys.save("google_trends", {"api_key": "official-key"})
    monkeypatch.setattr(signals, "compute_search_interest",
                        lambda query, **kwargs: fake_si_signal())
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "search_interest",
        "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    block = json.loads(out_path.read_text(encoding="utf-8"))["signals"]["search_interest"]
    google = block["google_trends"]
    assert google["configured"] is False
    assert google["error"] == signals.GOOGLE_TRENDS_OFFICIAL_NOT_IMPLEMENTED
    assert "Apify" in google["error"]
    assert "не реализовано" in captured.err


def test_summary_for_humans_prints_search_interest_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Автономный запуск: таблица регионов Wordstat и «не настроен» для Apify."""
    monkeypatch.setattr(
        signals, "compute_search_interest",
        lambda query, **kwargs: SearchInterestSignal(
            query="requests",
            wordstat=SearchInterestSource(
                source="wordstat",
                points=(SearchInterestPoint(region="Россия", value=42, trend_pct=None),),
                total=42, trend_pct=None, configured=True, error=None,
            ),
            google_trends=SearchInterestSource(
                source="google_trends", points=(), total=None, trend_pct=None,
                configured=False, error=None,
            ),
        ),
    )
    out_path = tmp_path / "signals.json"

    code = signals.main([
        "collect", "--package", "requests", "--kind", "search_interest",
        "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert "Россия" in captured.out
    assert "суммарный интерес: 42" in captured.out
    assert "google_trends: ключ Google Trends не настроен" in captured.out
    assert '"type"' not in captured.out
