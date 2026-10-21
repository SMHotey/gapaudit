"""``cli.niche`` (шаг P5f-1): конвейер Niche Finder.

Проверяется то, что нельзя увидеть в браузере: протокол задачи
(hello → progress → done), форма ``--json-out`` (``{query, items, stats}``),
ранжирование по Gap Score, работа ``--limit``, поведение при ненастроенных
ключах (вид пропускается, код 0), argparse на неизвестном виде сигнала и
порядок argv, который реально собирает ``_default_runner`` (gotcha 62).

Сеть — ноль: ``build_catalog``, ``check_many`` и коллекторы подменены
фейками (gotcha 61); реальные транспорты в тестах без ключей проверяются
«не дёргаются вовсе».
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
from typing import Any

import pytest

from cli import niche
from core.api_keys import ApiKeysStore


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Каталог данных и cwd — во временный каталог (сеть по умолчанию нет)."""
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)


def protocol(out: str) -> list[dict]:
    """Разобрать stdout как JSON-lines: каждая непустая строка — валидный JSON."""
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def fake_catalog(*ids: str):
    """Фейк ``build_catalog``: каталог из заданных id (по одному источнику)."""

    def build(phrases: list[str], *, on_progress: Any = None, **kwargs: Any) -> dict:
        if on_progress is not None:
            on_progress(0.5, f"фраза 1/1: «{phrases[0]}» → github: {len(ids)}")
            on_progress(1.0, f"фраза 1/1: «{phrases[0]}» → npm: 0")
        return {
            "queries": phrases,
            "stats": {"total": len(ids)},
            "items": [
                {"id": name, "sources": ["github", "npm"], "stars": 100}
                for name in ids
            ],
        }

    return build


def fake_checks(rows: dict[str, dict]):
    """Фейк ``check_many``: наблюдения по заданным именам (week/flag/verdict)."""

    def checks(names: list[str], client: Any, **kwargs: Any) -> list[dict]:
        return [
            {
                "name": name,
                "week": rows[name]["week"],
                "month": rows[name].get("month", 4000),
                "flag": rows[name].get("flag", False),
                "verdict": rows[name].get("verdict", "ЖИВО"),
            }
            for name in names
        ]

    return checks


# --- протокол и результат ------------------------------------------------------


def test_run_job_json_emits_protocol_and_writes_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """hello → progress (шаги конвейера) → done; json-out — query/items/stats."""
    monkeypatch.setattr(niche, "build_catalog", fake_catalog("alpha", "beta"))
    monkeypatch.setattr(
        niche,
        "check_many",
        fake_checks({
            "alpha": {"week": 5000, "flag": False},
            "beta": {"week": 1000, "flag": True},
        }),
    )
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "run", "--query", "test impact analysis", "--min-stars", "20",
        "--pages", "1", "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    messages = protocol(captured.out)
    assert messages[0]["type"] == "hello"
    assert messages[-1] == {"type": "done"}
    steps = [m["message"] for m in messages if m["type"] == "progress"]
    assert "catalog.build" in steps
    assert "packages.check" in steps
    assert "gap_score" in steps
    assert steps[-1] == "done"

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["query"] == "test impact analysis"
    assert [item["name"] for item in payload["items"]] == ["alpha", "beta"]
    assert payload["stats"]["found"] == 2
    assert payload["stats"]["total"] == 2
    # без ключей сигналов: score по adoption + признаку ниши
    assert payload["stats"]["signals_used"] == []
    assert payload["items"][0]["sources"] == ["github", "npm"]
    assert payload["items"][0]["momentum_30d"] is None


def test_items_sorted_by_gap_score_desc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ранжирование по Gap Score: меньше adoption+признак — ниже в списке.

    ``alpha``: adoption 5000 без признака; ``beta``: 1000 с признаком;
    ``gamma``: 100 с признаком. Порядок в каталоге обратный ожидаемому —
    проверяется именно сортировка, а не порядок входа.
    """
    monkeypatch.setattr(niche, "build_catalog", fake_catalog("gamma", "beta", "alpha"))
    monkeypatch.setattr(
        niche,
        "check_many",
        fake_checks({
            "alpha": {"week": 5000, "flag": False},
            "beta": {"week": 1000, "flag": True},
            "gamma": {"week": 100, "flag": True},
        }),
    )
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "run", "--query", "test impact analysis", "--job-json",
        "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    items = json.loads(out_path.read_text(encoding="utf-8"))["items"]
    assert [item["name"] for item in items] == ["alpha", "beta", "gamma"]
    scores = [item["gap_score"] for item in items]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] is not None and scores[0] > scores[-1]


def test_limit_truncates_after_ranking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--limit 1`` — одна строка в результате, но ``found`` считает всех."""
    monkeypatch.setattr(niche, "build_catalog", fake_catalog("a", "b", "c"))
    monkeypatch.setattr(
        niche,
        "check_many",
        fake_checks({
            "a": {"week": 3000},
            "b": {"week": 2000},
            "c": {"week": 1000},
        }),
    )
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "run", "--query", "test impact analysis", "--limit", "1",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert [item["name"] for item in payload["items"]] == ["a"]
    assert payload["stats"] == {
        "total": 1, "found": 3, "checked": 3, "with_gap": 1, "signals_used": [],
    }


def test_without_job_json_prints_table_for_humans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Автономный запуск — таблица top-N в stdout, JSON-lines нет."""
    monkeypatch.setattr(niche, "build_catalog", fake_catalog("alpha"))
    monkeypatch.setattr(niche, "check_many", fake_checks({"alpha": {"week": 5000}}))
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "run", "--query", "test impact analysis", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert "alpha" in captured.out
    assert "niche: найдено 1" in captured.out
    assert '\"type\"' not in captured.out
    assert out_path.is_file()


# --- сигналы ------------------------------------------------------------------


def test_signal_kinds_with_keys_are_collected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """jobs + patents с ключами — два вида в ``signals_used`` и в компонентах.

    Ключи читаются из ``<data_dir>/api_keys.json`` (как в ``cli.signals``):
    тест кладёт их туда через ``ApiKeysStore.save``, а коллекторы подменяет —
    ``jobs_total`` и ``patents_impulse_pct`` должны доехать до score.
    """
    data_dir = tmp_path / "data"
    keys = ApiKeysStore(data_dir)
    keys.save("adzuna", {"app_id": "id", "app_key": "key"})
    keys.save("patentsview", {"api_key": "pv-key"})
    seen: dict[str, Any] = {}

    def fake_jobs(query: str, *, app_id, app_key, transport, **kwargs: Any):
        from core.collectors.jobs import JobsSignal
        seen["jobs"] = (query, app_id, app_key)
        return JobsSignal(total=1200, by_country=(), configured=True, error=None)

    def fake_patents(query: str, *, api_key, transport, **kwargs: Any):
        from core.collectors.patents import PatentsSignal
        seen["patents"] = (query, api_key)
        return PatentsSignal(
            total=42, by_year=(), impulse_pct=5.0, configured=True, error=None
        )

    monkeypatch.setattr(niche, "compute_jobs", fake_jobs)
    monkeypatch.setattr(niche, "compute_patents", fake_patents)
    monkeypatch.setattr(niche, "build_catalog", fake_catalog("alpha"))
    monkeypatch.setattr(niche, "check_many", fake_checks({"alpha": {"week": 5000}}))
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "run", "--query", "test impact analysis",
        "--signal-kinds", "jobs", "patents",
        "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["stats"]["signals_used"] == ["jobs", "patents"]
    item = payload["items"][0]
    assert item["jobs_total"] == 1200
    assert item["patents_impulse_pct"] == 5.0
    assert seen["jobs"] == ("alpha", "id", "key")
    assert seen["patents"] == ("alpha", "pv-key")
    steps = [m["message"] for m in protocol(captured.out) if m["type"] == "progress"]
    assert "signals.collect jobs" in steps
    assert "signals.collect patents" in steps


def test_signal_kinds_without_keys_are_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ключей нет — виды пропущены целиком: коллекторы не дёргаются, код 0.

    «Не настроен» — не ошибка (graceful degradation §18.1): score считается
    по adoption и признаку ниши, а ``signals_used`` честно пуст.
    """

    def must_not_be_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("коллектор без ключа не должен дёргаться")

    monkeypatch.setattr(niche, "compute_jobs", must_not_be_called)
    monkeypatch.setattr(niche, "compute_funding", must_not_be_called)
    monkeypatch.setattr(niche, "build_catalog", fake_catalog("alpha"))
    monkeypatch.setattr(niche, "check_many", fake_checks({"alpha": {"week": 5000}}))
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "run", "--query", "test impact analysis",
        "--signal-kinds", "jobs", "funding",
        "--job-json", "--json-out", str(out_path),
    ])
    captured = capsys.readouterr()

    assert code == 0
    assert protocol(captured.out)[-1] == {"type": "done"}
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["stats"]["signals_used"] == []
    item = payload["items"][0]
    assert item["jobs_total"] is None
    assert item["funding_total"] is None


def test_search_interest_uses_first_configured_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Тренд search_interest — из первого источника без ошибки (gotcha 66).

    Wordstat не настроен, Google Trends настроен и даёт тренд: в компонент
    уходит его число, а не ``None`` от ненастроенного Wordstat.
    """
    keys = ApiKeysStore(tmp_path / "data")
    keys.save("apify", {"api_token": "token", "actor_id": "actor"})

    def fake_search_interest(query: str, **kwargs: Any):
        from core.collectors.search_interest import (
            SearchInterestSignal,
            SearchInterestSource,
        )
        return SearchInterestSignal(
            query=query,
            wordstat=SearchInterestSource(
                source="wordstat", points=(), total=None, trend_pct=None,
                configured=False, error=None,
            ),
            google_trends=SearchInterestSource(
                source="google_trends", points=(), total=100,
                trend_pct=12.5, configured=True, error=None,
            ),
        )

    monkeypatch.setattr(niche, "compute_search_interest", fake_search_interest)
    monkeypatch.setattr(niche, "build_catalog", fake_catalog("alpha"))
    monkeypatch.setattr(niche, "check_many", fake_checks({"alpha": {"week": 5000}}))
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "run", "--query", "test impact analysis",
        "--signal-kinds", "search_interest",
        "--job-json", "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["stats"]["signals_used"] == ["search_interest"]
    assert payload["items"][0]["search_interest_trend"] == 12.5


# --- ошибки и argv ------------------------------------------------------------


def test_unknown_signal_kind_is_argparse_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--signal-kinds evil`` — argparse-ошибка, код 2 (та же конвенция, что у задач)."""
    with pytest.raises(SystemExit) as excinfo:
        niche.main([
            "run", "--query", "x", "--signal-kinds", "evil",
        ])

    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--pages", "0"), ("--pages", "11"), ("--limit", "0"), ("--limit", "501")],
)
def test_out_of_range_flags_are_exit_2(
    flag: str, value: str, capsys: pytest.CaptureFixture[str],
) -> None:
    """Границы ``--pages`` 1..10 и ``--limit`` 1..500 — код 2 без запуска сети."""
    code = niche.main(["run", "--query", "x", flag, value, "--job-json"])

    assert code == 2
    assert flag.lstrip("-") in capsys.readouterr().err


def test_runner_argv_order_flags_before_subcommand(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Порядок argv из ``_default_runner``: флаги задачи ДО подкоманды ``run``.

    ``python -m cli.niche --job-json --json-out=X run --query ...`` — тот же
    урок gotcha 62, что у ``cli.signals``: подкоманда разбирается как
    необязательный позиционный аргумент, а ``--query run`` остаётся значением
    флага (пакет/фраза с именем «run» не должна съедать диспетчеризацию).
    """
    monkeypatch.setattr(niche, "build_catalog", fake_catalog())
    monkeypatch.setattr(niche, "check_many", fake_checks({}))
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "--job-json", f"--json-out={out_path}",
        "run", "--query", "run", "--min-stars", "20",
    ])
    capsys.readouterr()

    assert code == 0
    assert json.loads(out_path.read_text(encoding="utf-8"))["query"] == "run"


def test_import_does_not_touch_filesystem(tmp_path: pathlib.Path) -> None:
    """Импорт модуля ничего не создаёт и не печатает (правило слоя ``cli``)."""
    created = list(tmp_path.iterdir())

    importlib.reload(niche)

    assert list(tmp_path.iterdir()) == created


def test_check_limit_caps_checked_rows_by_stars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Проверяются первые ``CHECK_LIMIT`` строк по звёздам, а не весь каталог.

    Каждая строка — до трёх запросов к реестрам, и на 130 строках лимиты
    реестров отдают ошибки: adoption не приходит ни у одной, все score равны
    100 (живой прогон P5f-1). Отбор до проверки: дешевле не спрашивать, чем
    выбрасывать ответы. ``found`` в статистике при этом честно считает весь
    каталог, ``checked`` — сколько реально проверено.
    """
    monkeypatch.setattr(niche, "CHECK_LIMIT", 2)

    def catalog(phrases: list[str], *, on_progress: Any = None, **kwargs: Any) -> dict:
        return {
            "items": [
                {"id": "low", "stars": 10, "sources": ["github"]},
                {"id": "high", "stars": 900, "sources": ["github"]},
                {"id": "mid", "stars": 100, "sources": ["github"]},
            ]
        }

    monkeypatch.setattr(niche, "build_catalog", catalog)
    monkeypatch.setattr(
        niche,
        "check_many",
        fake_checks({"high": {"week": 9000}, "mid": {"week": 100}}),
    )
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "run", "--query", "test impact analysis", "--job-json",
        "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert [item["name"] for item in payload["items"]] == ["high", "mid"]
    assert payload["stats"]["found"] == 3
    assert payload["stats"]["checked"] == 2


def test_measured_items_rank_above_unmeasured_on_equal_score() -> None:
    """При равном score измеренная ниша выше: «100 по одному признаку» не прячет данные.

    Без этого правила верх списка занимали строки без adoption (score = 100
    только по компоненте «MCP нет»), отсортированные по алфавиту, а реальные
    измерения уезжали вниз (живой прогон P5f-1). Проверяется сам ранжировщик:
    через каталог такой случай не собрать — при разных adoption min-max даёт
    разные score.
    """
    from types import SimpleNamespace

    score = SimpleNamespace(score=100.0, components_available=1)
    rows = [
        {"name": "aaa-no-data", "adoption_week": None, "verdict": "—"},
        {"name": "zzz-measured", "adoption_week": 5000, "verdict": "ЖИВО"},
    ]
    scores = {row["name"]: score for row in rows}

    items = niche._ranked_items(rows, scores, limit=10)

    assert [item["name"] for item in items] == ["zzz-measured", "aaa-no-data"]
    assert items[0]["gap_score"] == items[1]["gap_score"] == 100.0


# --- имена реестров -----------------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("pytest-dev/pytest", "pytest"),
        ("requests", "requests"),
        ("@scope/pkg", "@scope/pkg"),
        ("", ""),
    ],
)
def test_registry_name_strips_github_path(identifier: str, expected: str) -> None:
    """GitHub-id ``owner/repo`` → имя репозитория; npm scoped — как есть.

    Запрос ``pypi/owner%2Frepo`` всегда 404, и adoption у GitHub-строк
    оставался бы ``None`` при живых данных (найдено живым прогоном P5f-1).
    ``@scope/pkg`` не трогаем: косая черта там — часть имени пакета.
    """
    assert niche._registry_name(identifier) == expected


def test_github_ids_are_checked_by_repo_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Проверка пакетов и сигналы идут по имени репозитория, а в результате — id.

    Экран показывает, что нашёл каталог (``owner/repo``), а реестры знают
    проект под именем репозитория: без этого разделения adoption не находится
    ни у одной GitHub-строки, и ранжирование по Gap Score теряет смысл.
    """
    seen: list[list[str]] = []

    def fake_checks(names: list[str], client: Any, **kwargs: Any) -> list[dict]:
        seen.append(list(names))
        return [
            {"name": name, "week": 5000, "flag": False, "verdict": "ЖИВО"}
            for name in names
        ]

    monkeypatch.setattr(niche, "build_catalog", fake_catalog("pytest-dev/pytest", "@scope/pkg"))
    monkeypatch.setattr(niche, "check_many", fake_checks)
    out_path = tmp_path / "niche.json"

    code = niche.main([
        "run", "--query", "test impact analysis", "--job-json",
        "--json-out", str(out_path),
    ])
    capsys.readouterr()

    assert code == 0
    assert seen == [["pytest", "@scope/pkg"]]
    items = json.loads(out_path.read_text(encoding="utf-8"))["items"]
    assert [item["name"] for item in items] == ["@scope/pkg", "pytest-dev/pytest"]
    assert all(item["adoption_week"] == 5000 for item in items)


# --- momentum из Store и прозрачность имён (P5f-2) -----------------------------


def seed_history(rows: dict[str, tuple[int, int]]) -> pathlib.Path:
    """Положить в ``<data_dir>/gapaudit.db`` два замера на имя: давний и сейчас.

    ``rows``: имя -> (старый adoption_week, текущий). Старый замер пишется
    25 дней назад, а не 30: окно momentum — ``[now-30д, now]``, и замер ровно
    на границе выпадает из него (разница в доли секунды между записью и
    расчётом), оставляя одну точку и ``None`` вместо дельты.
    Замеры пишутся тем же ``Store.record_observation``, что и хуки задач, —
    значит CLI читает ровно ту форму, которую оставляет приложение.
    """
    from datetime import datetime, timedelta, timezone

    from app.store import Store

    data_dir = pathlib.Path(os.environ["GAPAUDIT_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    with Store(data_dir / "gapaudit.db") as store:
        for name, (old_week, new_week) in rows.items():
            for shift, week in ((timedelta(days=25), old_week), (timedelta(), new_week)):
                store.record_observation(
                    {
                        "name": name,
                        "src": "pypi",
                        "ver": "1.0",
                        "date": "2026-10-01",
                        "age_days": 5,
                        "summary": "demo",
                        "home": "",
                        "flag": False,
                        "mcp": False,
                        "month": week * 4,
                        "week": week,
                        "verdict": "ЖИВОЙ",
                    },
                    run_id="seed",
                    measured_at=(now - shift).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
    return data_dir


def run_niche(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    ids: tuple[str, ...],
    rows: dict[str, dict],
    extra: tuple[str, ...] = (),
) -> dict:
    """Прогнать конвейер с фейковым каталогом и вернуть разобранный result.json."""
    monkeypatch.setattr(niche, "build_catalog", fake_catalog(*ids))
    monkeypatch.setattr(niche, "check_many", fake_checks(rows))
    out_path = tmp_path / "niche.json"
    code = niche.main([
        "run", "--query", "test impact analysis", "--json-out", str(out_path), *extra,
    ])
    assert code == 0
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_momentum_is_read_from_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """История наблюдений даёт momentum_30d: (1500-1000)/1000 = +50%."""
    seed_history({"alpha": (1000, 1500)})

    payload = run_niche(
        monkeypatch, tmp_path, ("alpha",), {"alpha": {"week": 1500, "flag": False}}
    )
    capsys.readouterr()

    item = payload["items"][0]
    assert item["momentum_30d"] == 50.0
    assert item["components_available"] >= 3  # adoption + momentum + «MCP нет»


def test_momentum_is_none_without_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Базы нет (CLI без приложения) — momentum ``None``, а не 0 и не падение."""
    payload = run_niche(
        monkeypatch, tmp_path, ("alpha",), {"alpha": {"week": 1500, "flag": False}}
    )
    capsys.readouterr()

    assert payload["items"][0]["momentum_30d"] is None


def test_cli_does_not_create_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Чтение Store не создаёт ``gapaudit.db``: momentum — сигнал, а не запись."""
    run_niche(monkeypatch, tmp_path, ("alpha",), {"alpha": {"week": 1500}})
    capsys.readouterr()

    data_dir = pathlib.Path(os.environ["GAPAUDIT_DATA_DIR"])
    assert not (data_dir / "gapaudit.db").exists()


def test_broken_database_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Битый файл базы — код 0, momentum ``None`` и объяснение в stderr."""
    data_dir = pathlib.Path(os.environ["GAPAUDIT_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "gapaudit.db").write_bytes(b"not a sqlite database")

    payload = run_niche(
        monkeypatch, tmp_path, ("alpha",), {"alpha": {"week": 1500, "flag": False}}
    )
    captured = capsys.readouterr()

    assert payload["items"][0]["momentum_30d"] is None
    assert "momentum" in captured.err


def test_items_report_registry_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``registry_name`` — под каким именем пакет искали в реестрах (не owner/repo)."""
    payload = run_niche(
        monkeypatch, tmp_path, ("acme/alpha",), {"alpha": {"week": 10, "flag": True}}
    )
    capsys.readouterr()

    item = payload["items"][0]
    assert item["name"] == "acme/alpha"
    assert item["registry_name"] == "alpha"
