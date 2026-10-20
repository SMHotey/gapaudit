"""``cli.signals`` (шаг P5a): обёртка ``python -m cli.signals collect``.

Проверяется протокол задачи (hello → progress → done), формат
``--json-out`` (``{"package", "signals": {"jobs": ...}}``), graceful
degradation без ключа (``configured=False``, код 0) и дедупликация
``--kind``.

Сеть — ноль: ``compute_jobs`` подменён фейком, а реальный транспорт в
тесте без ключа проверяется «не дёргается вовсе» (ключ читается из
``<data_dir>/api_keys.json``, которого в tmp нет — AGENTS gotcha 61).
"""

from __future__ import annotations

import json
import pathlib

import pytest

from cli import signals
from core.collectors.jobs import JobsByCountry, JobsSignal

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
