"""``GET``/``PUT /api/settings`` (шаг 2c): настройки экрана «Настройки».

Проверяется контракт экрана, а не внутренности ``core.settings``:

* GET отдаёт изменяемые поля, readonly-поля (каталог данных, база, версия) и
  список ключей, которые разрешено менять;
* PUT валидирует до записи (тип, диапазон, инвариант ``alive_weekly >=
  dead_weekly``) и отвечает 400 со всеми проблемами сразу;
* успешный PUT пишет ``settings.json`` в каталог данных, меняет
  ``app.state.settings`` (это видит ``/api/matrix``) и переживает перезапуск
  приложения — файл накладывается в lifespan, потому что ``desktop.py`` его не
  читает (deviation 2c, см. отчёт).

Сеть — ноль, подпроцессы — ноль: клиент из conftest (фейковый runner, каталог
данных в ``tmp_path``, воркеры не поднимаются).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.jobs import JobQueue
from app.web import EDITABLE_SETTING_KEYS, VERSION, create_app
from tests.conftest import HOST, PORT, TOKEN, app_state, auth, fake_runner


def get_settings(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/settings", headers=auth())
    assert response.status_code == 200
    return response.json()


def put_settings(client: TestClient, payload: dict[str, Any]) -> Any:
    return client.put("/api/settings", headers=auth(), json=payload)


# --- GET ---------------------------------------------------------------------


def test_settings_requires_token(client: TestClient) -> None:
    """Без токена — 403 (миддлварь, как у остальных ``/api/*``)."""
    response = client.get("/api/settings")

    assert response.status_code == 403


def test_get_returns_settings_readonly_and_editable_keys(client: TestClient) -> None:
    """Все изменяемые поля на месте, их ровно семнадцать, readonly — отдельно.

    Семнадцать = десять прежних (пороги, ключевые слова, кэш, задачи, реестр) +
    семь весов Gap Score (шаг P5e). Число держится явно: список — контракт
    экрана, и «пропало поле» должно краснить тест, а не молча прятать форму.
    """
    payload = get_settings(client)

    settings = payload["settings"]
    for key in EDITABLE_SETTING_KEYS:
        assert key in settings, key
    assert len(payload["editable_keys"]) == 17
    assert set(payload["editable_keys"]) == set(EDITABLE_SETTING_KEYS)

    # data_dir и language в настройках есть, но менять их через API нельзя
    assert "data_dir" in settings
    assert "language" in settings
    assert "data_dir" not in payload["editable_keys"]
    assert "language" not in payload["editable_keys"]
    assert "load_error" not in settings, "состояние загрузки — не настройка"

    # дефолты legacy: экран показывает то же, чем живёт CLI
    assert settings["alive_weekly"] == 100
    assert settings["dead_weekly"] == 10
    assert settings["keywords"] == ["mcp", "model context protocol"]

    assert set(payload["readonly"]) == {
        "data_dir", "db_path", "version", "api_token_required",
    }
    assert payload["readonly"]["api_token_required"] is True


def test_readonly_data_dir_is_absolute_test_directory(
    client: TestClient, tmp_path: Path
) -> None:
    """``readonly.data_dir`` — тот же каталог, куда lifespan открыл Store."""
    readonly = get_settings(client)["readonly"]

    data_dir = Path(readonly["data_dir"])
    assert data_dir.is_absolute()
    assert data_dir == tmp_path / "data"
    assert data_dir == Path(app_state(client).data_dir)


def test_readonly_db_path_points_to_sqlite_file(client: TestClient) -> None:
    """``readonly.db_path`` — файл SQLite внутри каталога данных."""
    readonly = get_settings(client)["readonly"]

    assert readonly["db_path"] == str(Path(readonly["data_dir"]) / "gapaudit.db")
    assert readonly["db_path"].endswith("gapaudit.db")


def test_readonly_version_matches_app_version(client: TestClient) -> None:
    """Версия — одна на всё приложение (не литерал в шаблоне)."""
    assert get_settings(client)["readonly"]["version"] == VERSION == "0.1.0"


# --- PUT: успешный путь ------------------------------------------------------


def test_put_valid_threshold_returns_new_value_and_warning(
    client: TestClient,
) -> None:
    """``alive_weekly=50`` при ``dead_weekly=10``: принято и предупреждает о пересчёте.

    Предупреждение обязательно: порог изменился, а матрица и вердикты считаются
    по нему только после перечитывания экранов. (Список тестов в задании шага
    ждал здесь пустых warnings — это противоречит его же разделу про warnings;
    принят вариант с предупреждением, см. отчёт, отклонение 2c-1.)
    """
    response = put_settings(client, {"alive_weekly": 50})

    assert response.status_code == 200
    payload = response.json()
    assert payload["settings"]["alive_weekly"] == 50
    assert payload["warnings"], "изменение порога обязано предупредить о пересчёте"
    assert "/api/matrix" in payload["warnings"][0]


def test_put_unchanged_value_gives_empty_warnings(client: TestClient) -> None:
    """Повторная запись того же значения — 200 без предупреждений.

    Так экран отличает «сохранено, но ничего не поменялось» от «сохранено,
    матрица считается по-старому»: warnings — про фактическое изменение.
    """
    payload = put_settings(client, {"alive_weekly": 100}).json()

    assert payload["settings"]["alive_weekly"] == 100
    assert payload["warnings"] == []


def test_put_max_parallel_jobs_warns_about_restart(client: TestClient) -> None:
    """``max_parallel_jobs`` меняется только после перезапуска — и говорит об этом."""
    payload = put_settings(client, {"max_parallel_jobs": 3}).json()

    assert payload["settings"]["max_parallel_jobs"] == 3
    assert len(payload["warnings"]) == 1
    assert "перезапуск" in payload["warnings"][0]


def test_get_after_put_returns_new_values(client: TestClient) -> None:
    """Порядок GET → PUT → GET: второй GET видит сохранённое, а не дефолт."""
    assert get_settings(client)["settings"]["stale_days"] == 540

    assert put_settings(client, {"stale_days": 400}).status_code == 200

    assert get_settings(client)["settings"]["stale_days"] == 400


def test_put_saves_settings_json_in_data_dir(client: TestClient, tmp_path: Path) -> None:
    """Файл настроек лежит в каталоге данных и содержит новое значение."""
    assert put_settings(client, {"alive_monthly": 777}).status_code == 200

    path = tmp_path / "data" / "settings.json"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert '"alive_monthly": 777' in text
    # data_dir в файл попадает как есть (пустая строка — «каталог по умолчанию»),
    # но через API его не меняли: PUT его не принимает
    assert '"language": "ru"' in text


def test_put_keywords_normalizes_and_persists(client: TestClient) -> None:
    """Keywords: пробелы вокруг слов снимаются, значение переживает в GET."""
    response = put_settings(client, {"keywords": ["  mcp ", "helm"]})

    assert response.status_code == 200
    assert response.json()["settings"]["keywords"] == ["mcp", "helm"]
    assert get_settings(client)["settings"]["keywords"] == ["mcp", "helm"]


def test_put_threshold_is_used_by_matrix(client: TestClient) -> None:
    """Порог из PUT виден ``/api/matrix`` без перезапуска (2b читает на запросе)."""
    assert put_settings(client, {"alive_weekly": 500}).status_code == 200

    matrix = client.get("/api/matrix", headers=auth()).json()

    assert matrix["thresholds"]["alive_weekly"] == 500


# --- PUT: отказы -------------------------------------------------------------


def test_put_unknown_key_is_rejected(client: TestClient) -> None:
    """Незнакомый ключ — 400: экран не должен «сохранять» то, чего нет."""
    response = put_settings(client, {"foo": 1})

    assert response.status_code == 400
    assert "foo" in response.json()["detail"]


def test_put_wrong_type_is_rejected(client: TestClient) -> None:
    """Строка вместо числа — 400 (никакого молчаливого приведения)."""
    response = put_settings(client, {"alive_weekly": "50"})

    assert response.status_code == 400
    assert "alive_weekly" in response.json()["detail"]
    assert get_settings(client)["settings"]["alive_weekly"] == 100


def test_put_negative_and_zero_bounds_are_rejected(client: TestClient) -> None:
    """Границы: ``stale_days < 1`` и ``cache_ttl_hours <= 0`` — 400."""
    assert put_settings(client, {"stale_days": -1}).status_code == 400
    assert put_settings(client, {"stale_days": 0}).status_code == 400
    assert put_settings(client, {"cache_ttl_hours": 0}).status_code == 400
    assert put_settings(client, {"cache_ttl_hours": -2}).status_code == 400
    assert put_settings(client, {"alive_weekly": -5}).status_code == 400
    # дефолты не тронуты ни одним отказом
    settings = get_settings(client)["settings"]
    assert settings["stale_days"] == 540
    assert settings["cache_ttl_hours"] == 12.0


def test_put_rejects_alive_below_dead(client: TestClient) -> None:
    """Инвариант: «живой» не может быть меньше «мёртвого»."""
    response = put_settings(client, {"alive_weekly": 5, "dead_weekly": 10})

    assert response.status_code == 400
    assert "alive_weekly" in response.json()["detail"]
    assert get_settings(client)["settings"]["alive_weekly"] == 100


def test_put_invariant_checks_merged_values(client: TestClient) -> None:
    """Инвариант проверяется на сведённых значениях: ``dead_weekly=200`` при 100.

    Один ключ в запросе — второй берётся из текущих настроек; проверка только
    присланной пары пропустила бы противоречие с состоянием.
    """
    response = put_settings(client, {"dead_weekly": 200})

    assert response.status_code == 400
    assert get_settings(client)["settings"]["dead_weekly"] == 10


def test_put_rejects_empty_or_too_long_keywords(client: TestClient) -> None:
    """Keywords: 1..50 непустых строк; пустой список и 51 слово — 400."""
    assert put_settings(client, {"keywords": []}).status_code == 400
    assert put_settings(client, {"keywords": ["mcp", "  "]}).status_code == 400
    assert put_settings(client, {"keywords": ["x"] * 51}).status_code == 400
    assert put_settings(client, {"keywords": "mcp"}).status_code == 400

    assert get_settings(client)["settings"]["keywords"] == [
        "mcp", "model context protocol",
    ]


def test_failed_put_does_not_touch_settings_file(
    client: TestClient, tmp_path: Path
) -> None:
    """Отказ ничего не пишет: ни ``app.state``, ни файла."""
    assert put_settings(client, {"alive_weekly": 5, "dead_weekly": 10}).status_code == 400

    assert not (tmp_path / "data" / "settings.json").exists()


# --- перезапуск --------------------------------------------------------------


def test_saved_settings_are_loaded_on_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сохранённое переживает перезапуск: lifespan накладывает settings.json.

    ``desktop.py`` читает только окружение, поэтому файл применяется в
    ``app/web.py`` (см. :func:`app.web._merge_saved_settings`). Проверка идёт
    двумя приложениями на одном каталоге данных — это и есть «перезапуск».
    """
    monkeypatch.setenv("GAPAUDIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(JobQueue, "start", lambda self: None)

    def build() -> Any:
        return create_app(
            token=TOKEN, port=PORT, runner=fake_runner, gh_check=lambda: True
        )

    first = build()
    with TestClient(first, base_url=f"http://{HOST}") as client:
        assert put_settings(client, {"registry_limit": 42}).status_code == 200

    second = build()
    with TestClient(second, base_url=f"http://{HOST}") as client:
        payload = get_settings(client)

    assert payload["settings"]["registry_limit"] == 42
    # data_dir по-прежнему из окружения, а не из файла
    assert payload["readonly"]["data_dir"] == str(tmp_path / "data")
    assert cast(Any, second).state.settings.registry_limit == 42


# --- веса Gap Score (шаг P5e) ------------------------------------------------


def test_get_reports_gap_weights_with_devplan_defaults(client: TestClient) -> None:
    """``GET /api/settings`` отдаёт все семь весов с дефолтами §18.3.

    Экран заполняет форму из этого ответа: пропавший ключ дал бы пустое поле
    и `required`-ошибку при сохранении.
    """
    settings = get_settings(client)["settings"]

    assert settings["gap_weight_adoption"] == 1.0
    assert settings["gap_weight_momentum"] == 1.0
    assert settings["gap_weight_no_flag"] == 2.0
    assert settings["gap_weight_funding"] == -0.5
    assert settings["gap_weight_patents"] == -0.3
    assert settings["gap_weight_jobs"] == 0.5
    assert settings["gap_weight_search"] == 0.3


def test_put_gap_weight_changes_matrix_and_persists(client: TestClient) -> None:
    """``PUT`` веса -> 200, значение видно в ``GET`` и в ``app.state.settings``.

    Вес уезжает в ``core.signals.GapWeights`` через ``default_weights_from_settings``:
    проверяем и ответ, и состояние приложения (по нему считаются score), и
    предупреждение «обновите экраны» — размер точек поменялся.
    """
    from app.web import GAP_WEIGHT_KEYS

    assert "gap_weight_adoption" in GAP_WEIGHT_KEYS

    response = put_settings(client, {"gap_weight_adoption": 0.5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["settings"]["gap_weight_adoption"] == 0.5
    assert any("Gap Score" in warning for warning in payload["warnings"])
    assert get_settings(client)["settings"]["gap_weight_adoption"] == 0.5
    assert app_state(client).settings.gap_weight_adoption == 0.5


def test_put_gap_weight_rejects_nan_and_infinity(client: TestClient) -> None:
    """NaN/inf — 400: такой вес делает score бессмысленным (P5e).

    Проверяются оба пути: строка ``"NaN"`` из формы и настоящие ``NaN``/
    ``Infinity`` в теле JSON. Литералы отправляются сырым телом: ``httpx``
    отказывается сериализовать ``float("nan")`` («Out of range float values are
    not JSON compliant»), а Python-сервер такие литералы штатно принимает
    (``json.loads`` — расширение стандарта), то есть это реальный вход.
    """
    for value in ("NaN", "abc", ""):
        response = put_settings(client, {"gap_weight_adoption": value})
        assert response.status_code == 400, value
        assert "gap_weight_adoption" in response.json()["detail"]

    for literal in ("NaN", "Infinity", "-Infinity"):
        response = client.put(
            "/api/settings",
            headers={**auth(), "Content-Type": "application/json"},
            content=('{"gap_weight_adoption": ' + literal + "}").encode(),
        )
        assert response.status_code == 400, literal
        assert "gap_weight_adoption" in response.json()["detail"]

    # ни одно из значений не записалось
    assert get_settings(client)["settings"]["gap_weight_adoption"] == 1.0
