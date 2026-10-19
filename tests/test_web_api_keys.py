"""``/api/settings/api-keys`` (шаг P4a): контракт экрана «Настройки → API-ключи».

Проверяется именно контракт, а не внутренности ``core.api_keys`` (у него свои
тесты):

* GET отдаёт всех провайдеров со статусом, полями и ``meta_fields`` — и **не
  отдаёт значения ключей** (ни в одном поле ответа);
* PUT валидирует до записи (поля, провайдер, пустые значения, чужие ключи в
  ``meta``) и отвечает 400 со всеми причинами;
* запись ложится в ``<data_dir>/api_keys.json`` в base64 — открытым текстом
  ключа нет ни в файле, ни в логе;
* POST ``/check`` — честная заглушка P4a: 200 и ``ok=False``, статус проверки не
  подделывается; неизвестный провайдер — 404;
* DELETE удаляет один раз (повтор — 404);
* UI-контракт: вкладки «Пороги»/«API-ключи» на месте, ``settings.js`` ходит в
  эти эндпоинты, а ссылка «Инструкции по получению» ведёт на существующий
  ``docs/API-KEYS.md`` (а не в 404).

Сеть — ноль, подпроцессы — ноль (клиент из conftest: фейковый runner, каталог
данных в ``tmp_path``, воркеры не поднимаются).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import web as web_module
from core.api_keys import META_FIELDS, PROVIDERS, ApiKeysStore
from tests.conftest import app_state, auth

ADZUNA = {"app_id": "adzuna-id", "app_key": "adzuna-key"}
SECRET = "SECRET-KEY-VALUE"


def keys_path(client: TestClient) -> Path:
    return Path(app_state(client).data_dir) / "api_keys.json"


def list_keys(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/settings/api-keys", headers=auth())
    assert response.status_code == 200
    return response.json()


def by_id(payload: dict[str, Any], provider: str) -> dict[str, Any]:
    return next(item for item in payload["providers"] if item["id"] == provider)


def put_key(client: TestClient, payload: dict[str, Any]) -> Any:
    return client.put("/api/settings/api-keys", headers=auth(), json=payload)


# --- чтение ------------------------------------------------------------------


def test_list_requires_token(client: TestClient) -> None:
    """Без токена — 403: эндпоинт под той же миддлварью, что и остальные /api/*."""
    response = client.get("/api/settings/api-keys")

    assert response.status_code == 403


def test_lifespan_creates_store_in_data_dir(client: TestClient) -> None:
    """Хранилище живёт в app.state и смотрит в каталог данных приложения.

    Ровно то, что не проверить «по коду»: если lifespan положит туда другой
    каталог, ключи будут писаться не туда, где их читает UI.
    """
    store = app_state(client).api_keys

    assert isinstance(store, ApiKeysStore)
    assert store.path == keys_path(client)


def test_list_on_empty_store_returns_all_providers(client: TestClient) -> None:
    """Шесть провайдеров, все «не настроен», у каждого свои поля и якорь docs."""
    payload = list_keys(client)

    assert [item["id"] for item in payload["providers"]] == list(PROVIDERS)
    assert all(item["configured"] is False for item in payload["providers"])
    assert all(item["last_check"] is None for item in payload["providers"])
    assert all(item["last_check_ok"] is None for item in payload["providers"])

    adzuna = by_id(payload, "adzuna")
    assert adzuna["label"] == "Adzuna"
    assert adzuna["kind"] == "api_key"
    assert adzuna["fields"] == ["app_id", "app_key"]
    assert adzuna["meta_fields"] == []
    assert adzuna["docs_anchor"] == "adzuna"
    assert adzuna["meta_public"] == {}


def test_list_marks_public_fields_for_every_provider(client: TestClient) -> None:
    """``meta_fields`` совпадают с объявленными в core — по ним UI выбирает тип поля."""
    payload = list_keys(client)

    for provider in PROVIDERS:
        assert by_id(payload, provider)["meta_fields"] == list(META_FIELDS[provider])


# --- запись ------------------------------------------------------------------


def test_put_saves_key_and_status_becomes_configured(client: TestClient) -> None:
    """PUT -> 200, GET -> «настроен», а на диске — base64 (не открытый текст)."""
    response = put_key(client, {"provider": "adzuna", "values": ADZUNA})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert by_id(list_keys(client), "adzuna")["configured"] is True

    text = keys_path(client).read_text(encoding="utf-8")
    assert "adzuna-id" not in text
    assert "adzuna-key" not in text
    assert json.loads(text)["providers"]["adzuna"]["meta"] == {}


def test_put_rejects_missing_field(client: TestClient) -> None:
    """Половина учётных данных — 400 и никакой записи."""
    response = put_key(client, {"provider": "adzuna", "values": {"app_id": "x"}})

    assert response.status_code == 400
    assert "app_key" in response.json()["detail"]
    assert not keys_path(client).exists()


def test_put_rejects_unknown_provider(client: TestClient) -> None:
    """Имя вне ``PROVIDERS`` — 400, а не новая запись в файле."""
    response = put_key(client, {"provider": "uspto", "values": {"api_key": "x"}})

    assert response.status_code == 400
    assert not keys_path(client).exists()


def test_put_rejects_empty_values(client: TestClient) -> None:
    """Пустые значения (и пустой словарь) — 400: «сохранить нечего»."""
    assert put_key(client, {"provider": "adzuna", "values": {}}).status_code == 400
    assert put_key(
        client, {"provider": "adzuna", "values": {"app_id": "x", "app_key": "  "}}
    ).status_code == 400
    assert not keys_path(client).exists()


def test_put_rejects_foreign_meta_key(client: TestClient) -> None:
    """``meta`` видна в API: чужой ключ туда не пускается (§18.5)."""
    response = put_key(
        client,
        {
            "provider": "edgar",
            "values": {"user_agent": "GapAudit me@example.com"},
            "meta": {"app_key": SECRET},
        },
    )

    assert response.status_code == 400
    assert not keys_path(client).exists()


def test_put_rejects_unknown_body_field(client: TestClient) -> None:
    """Лишний ключ в теле — 400: тело эндпоинта стабильно, как и у настроек."""
    response = put_key(
        client, {"provider": "adzuna", "values": ADZUNA, "лишнее": 1}
    )

    assert response.status_code == 400
    assert not keys_path(client).exists()


def test_put_without_provider_is_400(client: TestClient) -> None:
    """Тело без имени провайдера не должно превратиться в запись."""
    assert put_key(client, {"values": ADZUNA}).status_code == 400
    assert not keys_path(client).exists()


def test_edgar_public_meta_is_returned_and_secret_is_not(client: TestClient) -> None:
    """EDGAR: ``user_agent`` — не секрет и виден в ``meta_public`` (§7)."""
    put_key(client, {"provider": "edgar", "values": {"user_agent": "GapAudit me@example.com"}})

    edgar = by_id(list_keys(client), "edgar")
    assert edgar["configured"] is True
    assert edgar["meta_public"] == {"user_agent": "GapAudit me@example.com"}


# --- ключ не отдаётся и не логируется ---------------------------------------


def test_get_never_returns_key_values(client: TestClient) -> None:
    """В ответе GET нет значений ключей — ни в каком поле, ни куском строки."""
    put_key(client, {"provider": "adzuna", "values": {"app_id": "VALUE-ID", "app_key": SECRET}})
    put_key(client, {"provider": "yandex_wordstat", "values": {"token": "TOKEN-STR", "client_id": "12345"}})

    response = client.get("/api/settings/api-keys", headers=auth())
    body = response.text

    assert response.status_code == 200
    for secret in ("VALUE-ID", SECRET, "TOKEN-STR"):
        assert secret not in body, secret
    # несекретные идентификаторы при этом возвращаются — иначе UI слепнет
    assert "12345" in body


def test_put_does_not_log_key_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§18.5: значения ключей не попадают в лог (проверяются все записи логгера)."""
    calls: list[tuple[Any, ...]] = []

    class Recorder:
        def debug(self, *args: Any, **kwargs: Any) -> None:
            calls.append(args)

        def info(self, *args: Any, **kwargs: Any) -> None:
            calls.append(args)

        def warning(self, *args: Any, **kwargs: Any) -> None:
            calls.append(args)

        def error(self, *args: Any, **kwargs: Any) -> None:
            calls.append(args)

    monkeypatch.setattr(web_module, "logger", Recorder())

    assert put_key(
        client, {"provider": "adzuna", "values": {"app_id": "VALUE-ID", "app_key": SECRET}}
    ).status_code == 200

    logged = "; ".join(str(call) for call in calls)
    assert SECRET not in logged
    assert "VALUE-ID" not in logged
    assert "adzuna" in logged, "о сохранении ключа узнать из лога всё-таки можно"


# --- проверка ключа (заглушка P4a) ------------------------------------------


def test_check_endpoint_is_honest_stub(client: TestClient) -> None:
    """POST /check — 200 ``ok=False``; статус проверки при этом НЕ подделывается."""
    put_key(client, {"provider": "adzuna", "values": ADZUNA})

    response = client.post("/api/settings/api-keys/adzuna/check", headers=auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "P5" in payload["reason"]
    # mark_checked не вызывается: в статусе по-прежнему «не проверялся»
    adzuna = by_id(list_keys(client), "adzuna")
    assert adzuna["last_check"] is None
    assert adzuna["last_check_ok"] is None


def test_check_endpoint_on_unconfigured_provider_hints_to_save(client: TestClient) -> None:
    """Пустой провайдер: та же заглушка, но причина полезная."""
    response = client.post("/api/settings/api-keys/apify/check", headers=auth())

    assert response.status_code == 200
    assert "сохраните ключ" in response.json()["reason"]


def test_check_endpoint_unknown_provider_is_404(client: TestClient) -> None:
    """Опечатка в имени провайдера — 404, а не «проверка в разработке»."""
    response = client.post("/api/settings/api-keys/uspto/check", headers=auth())

    assert response.status_code == 404


def test_check_requires_token(client: TestClient) -> None:
    """Проверка — мутация состояния (в P5), значит токен обязателен и сейчас."""
    assert client.post("/api/settings/api-keys/adzuna/check").status_code == 403


# --- удаление ----------------------------------------------------------------


def test_delete_removes_key_once(client: TestClient) -> None:
    """DELETE -> 200 и статус «не настроен»; повторный DELETE -> 404."""
    put_key(client, {"provider": "adzuna", "values": ADZUNA})

    first = client.delete("/api/settings/api-keys/adzuna", headers=auth())
    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert by_id(list_keys(client), "adzuna")["configured"] is False

    assert client.delete("/api/settings/api-keys/adzuna", headers=auth()).status_code == 404


def test_delete_unknown_provider_is_404(client: TestClient) -> None:
    """Удалять несуществующего провайдера — тот же 404, что и «ключа нет»."""
    assert client.delete("/api/settings/api-keys/uspto", headers=auth()).status_code == 404


def test_delete_requires_token(client: TestClient) -> None:
    """Мутация без токена не проходит (миддлварь, как у остальных /api/*)."""
    put_key(client, {"provider": "adzuna", "values": ADZUNA})

    assert client.delete("/api/settings/api-keys/adzuna").status_code == 403
    assert by_id(list_keys(client), "adzuna")["configured"] is True


def test_saved_key_survives_app_restart(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключ — в файле, а не в памяти: новый lifespan видит его без пересохранения.

    Это и есть смысл отдельного файла (§18.2): перезапуск окна не должен
    требовать повторного ввода, а база при этом ключей не содержит вовсе.
    """
    put_key(client, {"provider": "adzuna", "values": ADZUNA})
    data_dir = app_state(client).data_dir

    from app.web import create_app
    from tests.conftest import PORT, TOKEN, fake_runner

    monkeypatch.setenv("GAPAUDIT_DATA_DIR", data_dir)
    again = create_app(token=TOKEN, port=PORT, runner=fake_runner, gh_check=lambda: True)
    with TestClient(again, base_url=f"http://127.0.0.1:{PORT}") as second:
        assert by_id(list_keys(second), "adzuna")["configured"] is True
    assert (Path(data_dir) / "gapaudit.db").exists(), "каталог данных тот же"


# --- UI-контракт (вкладка на экране «Настройки») -----------------------------


def test_settings_screen_has_api_keys_tab(client: TestClient) -> None:
    """Вкладки «Пороги»/«API-ключи» и контейнер списка провайдеров — в разметке."""
    response = client.get("/settings")

    assert response.status_code == 200
    for element_id in ("tab-thresholds", "tab-api-keys", "providers-list", "api-keys-msg"):
        assert f'id="{element_id}"' in response.text, element_id
    for tab in ("thresholds", "api-keys"):
        assert f'data-tab="{tab}"' in response.text, tab
    # вторая вкладка скрыта, пока пользователь её не открыл
    assert 'id="tab-api-keys" class="tab-panel" data-tab-panel="api-keys" hidden' in response.text
    # ссылка на инструкции есть и она ведёт в существующий файл, а не в 404
    assert 'href="/docs/API-KEYS.md"' in response.text
    assert client.get("/docs/API-KEYS.md").status_code == 200


def test_settings_js_uses_api_keys_endpoints_without_innerhtml(client: TestClient) -> None:
    """Скрипт ходит в свои эндпоинты и строит карточки DOM-методами (gotcha 37)."""
    script = client.get("/static/settings.js").text

    assert "/api/settings/api-keys" in script
    assert "/check" in script
    assert "'DELETE'" in script
    assert "createElement" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    # пароль не показывается открытым полем, а несекретные — показываются
    assert "input.type = isPublic ? 'text' : 'password'" in script
    # подтверждение перед удалением ключа
    assert "window.confirm" in script
    # F5 перечитывает и ключи, и пороги
    assert "window.__gapauditRefresh = load" in script


def test_list_reports_load_error_for_broken_file(client: TestClient) -> None:
    """Битый ``api_keys.json`` виден в API как ``load_error`` (§0.7, шаг P4b).

    До P4b ответ был неотличим от «ключей не вводили»: все провайдеры
    «не настроены», и пользователь вводил бы ключи заново поверх
    повреждённого файла. Причина уезжает в UI, который рисует баннер.
    """
    path = keys_path(client)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ это не JSON", encoding="utf-8", newline="\n")

    payload = list_keys(client)

    assert payload["load_error"] is not None
    assert all(item["configured"] is False for item in payload["providers"])
    # Ни одно значение файла не уехало в ответ: это по-прежнему только статус.
    assert "это не JSON" not in json.dumps(payload, ensure_ascii=False)


def test_list_load_error_is_null_for_healthy_store(client: TestClient) -> None:
    """Здоровый файл: ``load_error = None`` — UI баннер не рисует."""
    put_key(client, {"provider": "edgar", "values": {"user_agent": "GapAudit me@example.com"}})

    payload = list_keys(client)

    assert payload["load_error"] is None
    assert by_id(payload, "edgar")["configured"] is True
