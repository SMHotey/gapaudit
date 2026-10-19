"""Тесты ``core/api_keys.py`` (шаг P4a): файл ``<data_dir>/api_keys.json``.

Что держат тесты:

* импорт модуля не делает ввода-вывода: файл появляется только после ``save``;
* формат файла — ровно §18.2 (``version``, ``providers``, ``encrypted_value``,
  ``added_at``, ``last_check``/``last_check_ok``, ``meta``);
* значения секретов лежат в base64 и **не встречаются в файле открытым
  текстом** (тест «ключ не в plaintext» — гейт против возврата к plaintext);
* ``meta`` — только объявленные несекретные поля (``user_agent``, ``client_id``,
  ``actor_id``), потому что ``meta`` видна в API;
* права: 0600 на POSIX, ``icacls`` без унаследованных записей на Windows;
* валидация ``save`` до записи: неизвестный провайдер, неполный набор полей,
  пустые значения, чужие ключи в ``meta``.

Сеть — ноль, подпроцессов — ноль, кроме ``icacls`` (он есть только на Windows и
только там, где тест не пропускается).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from core.api_keys import (
    FORMAT_VERSION,
    KEY_FILE_NAME,
    META_FIELDS,
    PROVIDERS,
    ApiKeysStore,
    ProviderStatus,
    _decode,
    _encode,
)

ADZUNA = {"app_id": "adzuna-id", "app_key": "adzuna-key"}


def store_for(tmp_path: Path) -> ApiKeysStore:
    return ApiKeysStore(tmp_path)


def raw(tmp_path: Path) -> str:
    """Сырой текст файла ключей (тесту нужны и байты, и структура)."""
    return (tmp_path / KEY_FILE_NAME).read_text(encoding="utf-8")


def document(tmp_path: Path) -> dict:
    return json.loads(raw(tmp_path))


# --- импорт и пустое хранилище -----------------------------------------------


def test_import_does_not_create_file(tmp_path: Path) -> None:
    """Импорт модуля и статус на пустом каталоге файла не создают.

    Иначе обычный запуск CLI оставлял бы в каталоге данных пустой
    ``api_keys.json`` — след, которого пользователь не заказывал.
    """
    store = store_for(tmp_path)

    assert not store.path.exists()
    assert len(store.status_all()) == len(PROVIDERS)
    assert store.get("adzuna") is None

    assert not store.path.exists()


def test_status_all_on_empty_dir_is_not_configured(tmp_path: Path) -> None:
    """Пустой каталог: все шесть провайдеров «не настроен», проверок нет."""
    statuses = store_for(tmp_path).status_all()

    assert [status.provider for status in statuses] == list(PROVIDERS)
    assert all(status.configured is False for status in statuses)
    assert all(status.last_check is None for status in statuses)
    assert all(status.last_check_ok is None for status in statuses)


def test_status_of_unknown_provider_raises(tmp_path: Path) -> None:
    """Опечатка в имени провайдера — ошибка вызывающего, а не «не настроен»."""
    with pytest.raises(ValueError):
        store_for(tmp_path).status("adzunа")  # noqa: RUF001 - последняя буква кириллицей


def test_providers_meta_fields_agree(tmp_path: Path) -> None:
    """Согласие слоёв: у каждого провайдера объявлены meta-поля из его ``fields``.

    ``META_FIELDS`` и ``PROVIDERS`` правятся в разных местах, а расхождение
    молчаливое: поле, объявленное несекретным, но отсутствующее в ``fields``,
    никогда не заполнится, а незадекларированное секретное поле попало бы в
    ``meta`` открытым текстом.
    """
    assert set(META_FIELDS) == set(PROVIDERS)
    for provider, spec in PROVIDERS.items():
        for name in META_FIELDS[provider]:
            assert name in spec["fields"], (provider, name)
        assert spec["docs_anchor"], provider


# --- запись и чтение ---------------------------------------------------------


def test_save_creates_file_with_expected_document(tmp_path: Path) -> None:
    """``save`` создаёт файл: версия, провайдер, base64, added_at, meta, проверки."""
    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)

    doc = document(tmp_path)
    assert doc["version"] == FORMAT_VERSION
    assert list(doc["providers"]) == ["adzuna"]
    entry = doc["providers"]["adzuna"]
    assert set(entry) == {
        "encrypted_value", "added_at", "last_check", "last_check_ok", "meta",
    }
    assert entry["meta"] == {}
    assert entry["last_check"] is None
    assert entry["last_check_ok"] is None
    assert entry["added_at"].endswith("Z")
    # encrypted_value — base64 от JSON с обоими полями
    decoded = json.loads(base64.b64decode(entry["encrypted_value"]).decode("utf-8"))
    assert decoded == ADZUNA


def test_save_then_get_returns_all_fields(tmp_path: Path) -> None:
    """``get`` возвращает расшифрованные поля — то, чем пользуется коллектор."""
    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)

    assert store.get("adzuna") == ADZUNA
    assert store.status("adzuna") == ProviderStatus(
        provider="adzuna", configured=True, last_check=None, last_check_ok=None
    )


def test_status_all_after_save_only_that_provider_configured(tmp_path: Path) -> None:
    """Настроен ровно один провайдер — остальные остаются пустыми."""
    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)

    configured = {
        status.provider for status in store.status_all() if status.configured
    }
    assert configured == {"adzuna"}
    assert store.get("patentsview") is None


def test_mode_is_owner_only_on_posix(tmp_path: Path) -> None:
    """POSIX: файл с ключами — 0600 (§18.5)."""
    if os.name == "nt":
        pytest.skip("Windows: права задаёт icacls, а не биты режима (AGENTS gotcha 26)")

    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)

    assert os.stat(store.path).st_mode & 0o777 == 0o600


def _icacls(path: Path) -> bytes:
    out = subprocess.run(["icacls", str(path)], capture_output=True, timeout=30)
    return out.stdout


def test_windows_acl_keeps_only_owner(tmp_path: Path) -> None:
    """Windows: наследование снято, чужих записей в ACL нет (icacls).

    Контроль рядом — файл, который мы не ограничивали: у него наследованные
    записи есть, и тест ловит их отсутствие по маркеру ``(I)`` (наследовано).
    Без контроля проверка была бы зелёной и при выключенном ``icacls``.
    """
    if os.name != "nt":
        pytest.skip("icacls есть только на Windows")

    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)
    control = tmp_path / "control.txt"
    control.write_text("x", encoding="utf-8")

    assert b"(I)" in _icacls(control), "контроль: у обычного файла наследование видно"
    assert b"(I)" not in _icacls(store.path)
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    assert f"{domain}\\{user}".encode("utf-8") in _icacls(store.path) or not user


# --- валидация ---------------------------------------------------------------


def test_save_rejects_empty_values_dict(tmp_path: Path) -> None:
    """Пустой словарь — не «сохранить без полей», а ошибка: файл не создаётся."""
    store = store_for(tmp_path)

    with pytest.raises(ValueError):
        store.save("adzuna", {})
    assert not store.path.exists()


def test_save_rejects_missing_field(tmp_path: Path) -> None:
    """Неполный набор учётных данных хуже отсутствия — ровно поэтому ошибка."""
    store = store_for(tmp_path)

    with pytest.raises(ValueError):
        store.save("adzuna", {"app_id": "x"})
    assert not store.path.exists()


def test_save_rejects_blank_value_and_extra_field(tmp_path: Path) -> None:
    """Пустое значение и лишнее поле — тоже отказ до записи."""
    store = store_for(tmp_path)

    with pytest.raises(ValueError):
        store.save("adzuna", {"app_id": "x", "app_key": "   "})
    with pytest.raises(ValueError):
        store.save("adzuna", {**ADZUNA, "лишнее": "y"})
    assert not store.path.exists()


def test_save_rejects_unknown_provider(tmp_path: Path) -> None:
    """Чужое имя провайдера не станет записью в файле."""
    store = store_for(tmp_path)

    with pytest.raises(ValueError):
        store.save("uspto", {"api_key": "x"})
    assert not store.path.exists()


def test_save_rejects_undeclared_meta_field(tmp_path: Path) -> None:
    """``meta`` видна в API — произвольное поле туда не пускается (§18.5)."""
    store = store_for(tmp_path)

    with pytest.raises(ValueError):
        store.save("edgar", {"user_agent": "GapAudit me@example.com"}, {"secret": "x"})
    assert not store.path.exists()


def test_save_trims_surrounding_spaces(tmp_path: Path) -> None:
    """Пробелы по краям снимаются: ключ копируют из письма, и хвостовой пробел ломал бы запрос."""
    store = store_for(tmp_path)
    store.save("adzuna", {"app_id": "  x  ", "app_key": "\ty\n"})

    assert store.get("adzuna") == {"app_id": "x", "app_key": "y"}


def test_save_overwrite_resets_check_and_added_at(tmp_path: Path) -> None:
    """Новый ключ сбрасывает результат прежней проверки: он про старое значение."""
    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)
    store.mark_checked("adzuna", True)
    assert store.status("adzuna").last_check_ok is True

    store.save("adzuna", {"app_id": "new-id", "app_key": "new-key"})

    status = store.status("adzuna")
    assert status.configured is True
    assert status.last_check is None
    assert status.last_check_ok is None
    assert store.get("adzuna") == {"app_id": "new-id", "app_key": "new-key"}


# --- meta: несекретные поля --------------------------------------------------


def test_edgar_user_agent_lives_in_meta_not_in_base64(tmp_path: Path) -> None:
    """EDGAR — не ключ: ``encrypted_value`` пуст, значение в ``meta`` (§18.2)."""
    store = store_for(tmp_path)
    store.save("edgar", {"user_agent": "GapAudit me@example.com"})

    entry = document(tmp_path)["providers"]["edgar"]
    assert entry["encrypted_value"] == ""
    assert entry["meta"]["user_agent"] == "GapAudit me@example.com"

    assert store.get("edgar") == {"user_agent": "GapAudit me@example.com"}
    assert store.public_meta("edgar") == {"user_agent": "GapAudit me@example.com"}
    assert store.status("edgar").configured is True


def test_wordstat_and_apify_split_secret_and_identifier(tmp_path: Path) -> None:
    """Wordstat: токен в base64, ``client_id`` в meta; Apify: токен + ``actor_id``.

    Так записано в §18.2 и §14: идентификатор нужен UI и API, а секрет — нет.
    """
    store = store_for(tmp_path)
    store.save("yandex_wordstat", {"token": "oauth-token", "client_id": "12345"})
    store.save("apify", {"api_token": "apify-token", "actor_id": "owner/actor"})

    doc = document(tmp_path)["providers"]
    assert doc["yandex_wordstat"]["meta"] == {"client_id": "12345"}
    assert doc["apify"]["meta"] == {"actor_id": "owner/actor"}
    assert "oauth-token" not in raw(tmp_path)
    assert "apify-token" not in raw(tmp_path)

    assert store.get("yandex_wordstat") == {"token": "oauth-token", "client_id": "12345"}
    assert store.get("apify") == {"api_token": "apify-token", "actor_id": "owner/actor"}
    assert store.public_meta("yandex_wordstat") == {"client_id": "12345"}


def test_public_meta_ignores_foreign_keys_from_hand_edited_file(tmp_path: Path) -> None:
    """Чужой ключ в ``meta`` (правка файла руками) наружу не отдаётся.

    ``meta`` уезжает в API без расшифровки, поэтому её состав — часть контракта
    безопасности: читатель фильтрует по :data:`META_FIELDS`, а не доверяет файлу.
    """
    store = store_for(tmp_path)
    store.save("edgar", {"user_agent": "GapAudit me@example.com"})
    doc = document(tmp_path)
    doc["providers"]["edgar"]["meta"]["app_key"] = "утекло"
    store.path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")

    assert store.public_meta("edgar") == {"user_agent": "GapAudit me@example.com"}


# --- секрет не в открытом виде -----------------------------------------------


def test_secret_is_not_plaintext_in_file(tmp_path: Path) -> None:
    """Гейт «ключ не в plaintext»: в файле только base64.

    Проверяются оба поля adzuna: ``app_id`` хранится вместе с ``app_key`` как
    часть учётных данных, а не в ``meta`` (см. docstring модуля).
    """
    store = store_for(tmp_path)
    store.save("adzuna", {"app_id": "SECRET-ID-STR", "app_key": "SECRET-KEY-STR"})

    text = raw(tmp_path)
    assert "SECRET-ID-STR" not in text
    assert "SECRET-KEY-STR" not in text
    # и при этом значение действительно читается обратно
    assert store.get("adzuna") == {"app_id": "SECRET-ID-STR", "app_key": "SECRET-KEY-STR"}


def test_encode_decode_round_trip_with_cyrillic() -> None:
    """base64-обёртка переживает несколько полей и кириллицу."""
    values = {"app_id": "идентификатор", "app_key": "ключ-ü+"}

    assert _decode(_encode(values)) == values


def test_decode_of_broken_base64_is_empty() -> None:
    """Битый ``encrypted_value`` — «ключ не читается», а не исключение."""
    assert _decode("не-base64") == {}
    assert _decode(base64.b64encode(b"[1, 2]").decode("ascii")) == {}


def test_broken_file_reads_as_not_configured_and_is_not_rewritten(tmp_path: Path) -> None:
    """Битый файл не роняет чтение и не переписывается сам по себе."""
    store = store_for(tmp_path)
    store.path.write_text("{ это не JSON", encoding="utf-8", newline="\n")

    assert all(not status.configured for status in store.status_all())
    assert store.get("adzuna") is None
    assert raw(tmp_path) == "{ это не JSON"


def test_foreign_provider_entry_survives_write(tmp_path: Path) -> None:
    """Запись чужого (неизвестного модулю) провайдера не теряется при save.

    Файл может быть создан более новой версией приложения: сохранение adzuna не
    должно стирать её данные.
    """
    store = store_for(tmp_path)
    store.path.write_text(
        json.dumps({"version": 1, "providers": {"future_provider": {"encrypted_value": ""}}}),
        encoding="utf-8",
        newline="\n",
    )
    store.save("adzuna", ADZUNA)

    assert "future_provider" in document(tmp_path)["providers"]


# --- удаление и отметка проверки --------------------------------------------


def test_delete_reports_whether_entry_existed(tmp_path: Path) -> None:
    """``delete`` -> True один раз: второй вызов уже нечего удалять."""
    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)

    assert store.delete("adzuna") is True
    assert store.status("adzuna").configured is False
    assert store.delete("adzuna") is False
    assert store.delete("unknown-provider") is False


def test_mark_checked_updates_status(tmp_path: Path) -> None:
    """Результат проверки и её время видны в статусе (проверка появится в P5)."""
    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)

    store.mark_checked("adzuna", True)
    ok_status = store.status("adzuna")
    assert ok_status.last_check_ok is True
    assert ok_status.last_check is not None and ok_status.last_check.endswith("Z")

    store.mark_checked("adzuna", False)
    assert store.status("adzuna").last_check_ok is False


def test_mark_checked_on_unconfigured_provider_is_noop(tmp_path: Path) -> None:
    """Отмечать нечего — не ошибка и не появление пустой записи в файле."""
    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)

    store.mark_checked("patentsview", True)

    assert "patentsview" not in document(tmp_path)["providers"]
    assert store.status("patentsview").configured is False


def test_mark_checked_unknown_provider_raises(tmp_path: Path) -> None:
    """Незнакомое имя — ошибка вызывающего (в API такого вызова нет вообще)."""
    with pytest.raises(ValueError):
        store_for(tmp_path).mark_checked("uspto", True)


def test_clear_all_removes_file_and_status(tmp_path: Path) -> None:
    """«Сбросить всё»: файла нет, все провайдеры «не настроен», повтор — не ошибка."""
    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)
    store.save("edgar", {"user_agent": "GapAudit me@example.com"})

    store.clear_all()
    store.clear_all()

    assert not store.path.exists()
    assert all(not status.configured for status in store.status_all())


def test_status_is_configured_only_when_all_fields_present(tmp_path: Path) -> None:
    """Настроен = все поля объявлены и непусты: половинчатая запись — не ключ."""
    store = store_for(tmp_path)
    store.save("adzuna", ADZUNA)
    doc = document(tmp_path)
    # стираем одно из двух полей ровно так, как это сделала бы чужая версия
    doc["providers"]["adzuna"]["encrypted_value"] = _encode({"app_id": "x"})
    store.path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")

    assert store.status("adzuna").configured is False
    assert store.get("adzuna") is None


# --- load_error (шаг P4b, §0.7) ----------------------------------------------


def test_broken_json_sets_load_error_and_reports_not_configured(tmp_path: Path) -> None:
    """Битый файл: ``load_error`` с причиной, статусы — «не настроен».

    Разница принципиальна: «файла нет» — это норма (ключей ещё не вводили),
    «файл есть, но не читается» — авария, о которой UI обязан сказать. Без
    этого поля обе ситуации выглядели одинаково — пустым списком карточек.
    """
    store = store_for(tmp_path)
    store.path.write_text("{ это не JSON", encoding="utf-8", newline="\n")

    statuses = store.status_all()

    assert store.load_error is not None
    assert "JSON" in store.load_error
    assert all(not status.configured for status in statuses)


def test_foreign_format_version_sets_load_error(tmp_path: Path) -> None:
    """Чужая версия формата — тоже ``load_error`` (P5 сменит base64 на Fernet)."""
    store = store_for(tmp_path)
    store.path.write_text(
        json.dumps({"version": 99, "providers": {}}), encoding="utf-8", newline="\n"
    )

    assert store.status_all()  # не падает
    assert store.load_error is not None
    assert "99" in store.load_error


def test_load_error_is_cleared_by_successful_save(tmp_path: Path) -> None:
    """Сохранение перезаписывает файл целиком — старая ошибка больше не актуальна."""
    store = store_for(tmp_path)
    store.path.write_text("{ это не JSON", encoding="utf-8", newline="\n")
    assert store.status_all() and store.load_error is not None

    store.save("edgar", {"user_agent": "GapAudit me@example.com"})

    assert store.load_error is None
    assert store.status("edgar").configured is True


def test_missing_file_is_not_a_load_error(tmp_path: Path) -> None:
    """Отсутствие файла — штатное состояние, а не повреждение."""
    store = store_for(tmp_path)

    assert store.status_all()
    assert store.load_error is None
