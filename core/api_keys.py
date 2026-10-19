"""Хранилище API-ключей внешних провайдеров (шаг P4a): ``<data_dir>/api_keys.json``.

Зачем отдельный файл, а не таблица в SQLite (DEVPLAN §16 п. 7, §18.2): базу
копируют и выгружают целиком, и ключи уехали бы вместе с ней; ``core`` при этом
не должен знать про ``sqlite3`` и про ``app/`` (AGENTS gotcha 19).

Что держит контракт этого модуля:

* **импорт не делает ввода-вывода**: файл не читается и не создаётся, пока не
  позвали метод; каталог данных создаёт lifespan ``app/web.py``;
* **кэша нет**: файл читается на каждый вызов (сериализованный документ —
  сотни байт), поэтому правка файла руками или из другого процесса видна
  следующему вызову сразу. Псевдокод шага предполагал поле ``_cache`` — от
  него отказались ровно по этой причине, а не «за ненадобностью»;
* **значения ключей наружу не уезжают**: в API попадают только
  :class:`ProviderStatus` и :meth:`ApiKeysStore.public_meta` (несекретные
  метаданные). :meth:`ApiKeysStore.get` существует для коллекторов сигналов
  (P5) и в ``app/web.py`` вызываться не должен;
* **модуль ничего не печатает** (правило ``core``): битый файл читается как
  «ничего не настроено», и файл при этом не перезаписывается до явного
  :meth:`ApiKeysStore.save` — иначе одна опечатка в ручной правке стёрла бы
  все ключи. Причина, по которой файл не прочитался, не теряется: она лежит в
  :attr:`ApiKeysStore.load_error` и уезжает в API как ``load_error`` (шаг P4b):
  «тихо пусто» выглядело бы как «ключей не вводили», а это разные состояния;

Шифрование — base64, то есть НЕ криптография (DEVPLAN §18.2): значения защищены
правами файла (0600 на POSIX, ``icacls`` на Windows), а не ключом. Честная
формулировка живёт в ``docs/API-KEYS.md`` и в UI.

Формат файла — ровно §18.2::

    {
      "version": 1,
      "providers": {
        "adzuna": {"encrypted_value": "<base64>", "added_at": "2026-10-20T09:00:00Z",
                   "last_check": null, "last_check_ok": null, "meta": {}},
        "edgar":  {"encrypted_value": "", "added_at": "...",
                   "last_check": null, "last_check_ok": null,
                   "meta": {"user_agent": "GapAudit your-email@example.com"}}
      }
    }

Что секретное, а что нет (одно правило на все шесть провайдеров):
секреты лежат в ``encrypted_value`` (base64 от JSON-словаря полей), а
**несекретные идентификаторы — в ``meta`` открытым текстом**, потому что их
видно в UI и в API (``meta_public`` в §7) и они не дают доступа к чужому API::

    провайдер        encrypted_value (base64)      meta (открыто)
    ---------------- ---------------------------- --------------------
    adzuna           app_id, app_key               —
    patentsview      api_key                        —
    edgar            —  (пусто)                     user_agent
    yandex_wordstat  token                          client_id
    google_trends    api_key                        —
    apify            api_token                      actor_id

``adzuna.app_id`` попадает в секреты вместе с ``app_key``: пара — это и есть
учётные данные, и шаг запрещает хранить значение ключа открытым текстом
(тест «ключ не в plaintext» ставит в ``app_id`` секретную строку). В примере
§18.2 ``meta`` у adzuna показан — там ``app_id`` стоит открытым; это
единственное место, где пример формата и тест шага расходятся, выбран тест.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "FORMAT_VERSION",
    "KEY_FILE_NAME",
    "META_FIELDS",
    "PROVIDERS",
    "ApiKeysStore",
    "ProviderStatus",
]

#: Имя файла ключей внутри каталога данных.
KEY_FILE_NAME = "api_keys.json"

#: Версия формата файла. Версия есть не «на всякий случай»: P5 обещает
#: опциональный Fernet (DEVPLAN §14), и читателю нужно отличать base64 от него.
FORMAT_VERSION = 1

#: Провайдеры, их подписи, вид учётных данных, что вводить и якорь в
#: ``docs/API-KEYS.md`` (шаг P4a; §18.2 «Провайдеры и зачем»).
#: Порядок словаря — порядок карточек в UI.
PROVIDERS: dict[str, dict[str, Any]] = {
    "adzuna": {
        "label": "Adzuna",
        "kind": "api_key",
        "fields": ["app_id", "app_key"],
        "docs_anchor": "adzuna",
    },
    "patentsview": {
        "label": "PatentsView",
        "kind": "api_key",
        "fields": ["api_key"],
        "docs_anchor": "patentsview",
    },
    "edgar": {
        "label": "SEC EDGAR",
        "kind": "user_agent",
        "fields": ["user_agent"],
        "docs_anchor": "sec-edgar",
    },
    "yandex_wordstat": {
        "label": "Yandex Wordstat",
        "kind": "oauth",
        "fields": ["token", "client_id"],
        "docs_anchor": "yandex-wordstat",
    },
    "google_trends": {
        "label": "Google Trends (official alpha)",
        "kind": "api_key",
        "fields": ["api_key"],
        "docs_anchor": "google-trends",
    },
    "apify": {
        "label": "Apify (Google Trends scraper)",
        "kind": "api_key",
        "fields": ["api_token", "actor_id"],
        "docs_anchor": "apify",
    },
}

#: Несекретные поля провайдера: уезжают в ``meta`` открытым текстом и
#: возвращаются в API как ``meta_public`` (§7). Секреты остаются в
#: ``encrypted_value``. Ключи словаря обязаны совпадать с :data:`PROVIDERS`
#: (проверяется тестом «согласие слоёв»): карточка в UI строится из ``fields``,
#: а разделение секрет/не секрет живёт здесь — разойтись они не должны.
META_FIELDS: dict[str, tuple[str, ...]] = {
    "adzuna": (),
    "patentsview": (),
    "edgar": ("user_agent",),
    "yandex_wordstat": ("client_id",),
    "google_trends": (),
    "apify": ("actor_id",),
}

#: Поля записи провайдера в файле — вынесены в константы: их читают и запись,
#: и чтение, и тесты.
_ENCRYPTED_KEY = "encrypted_value"
_ADDED_KEY = "added_at"
_CHECK_KEY = "last_check"
_CHECK_OK_KEY = "last_check_ok"
_META_KEY = "meta"

#: Потолок ожидания ``icacls``: команда локальная, за 20 с обязана ответить.
_ICACLS_TIMEOUT = 20


@dataclass(frozen=True)
class ProviderStatus:
    """Состояние провайдера для API и UI — без значения ключа.

    Единственный путь наружу (``GET /api/settings/api-keys``): ``configured``
    говорит, есть ли полный набор полей, ``last_check``/``last_check_ok`` —
    результат последней проверки (``None`` — не проверяли).
    """

    provider: str
    configured: bool
    last_check: str | None = None
    last_check_ok: bool | None = None


class ApiKeysStore:
    """Чтение и запись ``<data_dir>/api_keys.json``.

    Экземпляр привязан к каталогу данных (в приложении — ``app.state.api_keys``,
    создаётся в lifespan). Все методы читают файл сами: см. «кэша нет» в
    docstring модуля.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / KEY_FILE_NAME
        #: Почему файл не прочитался (битый JSON, не та версия формата) — или
        #: ``None``. Поле, а не исключение: битый файл не должен валить экран
        #: настроек, но и «тихо пусто» — ложь, которую видно только по пустым
        #: полям. API отдаёт причину как ``load_error`` (§7), UI рисует баннер.
        self.load_error: str | None = None

    # --- чтение ---------------------------------------------------------------

    def status_all(self) -> list[ProviderStatus]:
        """Статус всех провайдеров в порядке :data:`PROVIDERS`.

        Значения ключей здесь не появляются — только «настроен / не настроен»
        и результат последней проверки.
        """
        stored = self._providers()
        return [self._status(name, stored.get(name)) for name in PROVIDERS]

    def status(self, provider: str) -> ProviderStatus:
        """Статус одного провайдера; неизвестное имя — ``ValueError``."""
        self.require_provider(provider)
        return self._status(provider, self._providers().get(provider))

    def public_meta(self, provider: str) -> dict[str, str]:
        """Несекретные метаданные провайдера (``client_id``, ``actor_id``, ``user_agent``).

        Безопасны по построению: записать в ``meta`` что-то кроме полей из
        :data:`META_FIELDS` нельзя (:meth:`save` это запрещает), а чужие ключи
        из правленого руками файла здесь отфильтровываются — иначе ``meta``
        стал бы вторым, никем не проверяемым каналом для секрета.
        """
        self.require_provider(provider)
        entry = self._providers().get(provider)
        meta = entry.get(_META_KEY) if isinstance(entry, dict) else None
        if not isinstance(meta, dict):
            return {}
        allowed = META_FIELDS.get(provider, ())
        return {
            name: value
            for name, value in meta.items()
            if name in allowed and isinstance(value, str) and value
        }

    def get(self, provider: str) -> dict[str, str] | None:
        """Расшифрованные поля провайдера; ``None`` — не настроен.

        ТОЛЬКО ДЛЯ КОЛЛЕКТОРОВ (P5). В ``/api/settings/api-keys`` этот метод
        вызывать нельзя: наружу уезжает статус, а не ключ. ``None`` вместо
        исключения — правило graceful degradation (§18.1): нет ключа — сигнал
        «не настроен», приложение работает.

        Возвращаются только объявленные поля провайдера (секрет раскодирован,
        ``meta`` добавлен) — и только если набор полный: половина учётных данных
        хуже их отсутствия.
        """
        self.require_provider(provider)
        entry = self._providers().get(provider)
        if not isinstance(entry, dict):
            return None
        encoded = entry.get(_ENCRYPTED_KEY)
        values = dict(_decode(encoded)) if isinstance(encoded, str) and encoded else {}
        values.update(self.public_meta(provider))
        if not self._complete(provider, values):
            return None
        return {name: values[name] for name in PROVIDERS[provider]["fields"]}

    # --- запись ---------------------------------------------------------------

    def save(
        self,
        provider: str,
        values: dict[str, str],
        meta: dict[str, str] | None = None,
    ) -> None:
        """Сохранить ключ провайдера: файл создаётся, права — только владельцу.

        Валидация до записи: провайдер известен, ``values`` содержит **все**
        объявленные поля (:data:`PROVIDERS`) и ни одного лишнего, каждое значение
        — непустая строка (пробелы по краям снимаются: ключ копируют из письма,
        и хвостовой пробел сломал бы запрос). Нарушение — ``ValueError``; запись
        при этом не делается вовсе, а не «частично применённая».

        ``meta`` — необязательные несекретные метаданные: допустимы только поля
        из :data:`META_FIELDS` (``meta`` виден в API, поэтому произвольный ключ
        здесь был бы способом вынести секрет в открытый канал). Объявленные поля
        из ``values`` попадают в ``meta`` сами.

        Перезапись существующего провайдера сбрасывает ``last_check`` /
        ``last_check_ok`` и обновляет ``added_at``: проверялся прежний ключ, и
        «проверка пройдена» про новое значение ничего не говорит.

        Запись атомарна (tmp-файл рядом + ``os.replace``): файл читают и UI при
        следующем запросе, и подпроцесс коллектора, и половина документа на
        диске означала бы «ключей нет» у одного из них. Права: ``0600`` на POSIX
        и ``icacls <файл> /inheritance:r /grant:r <пользователь>:F`` на Windows
        (см. :func:`_restrict_permissions`).
        """
        self.require_provider(provider)
        fields = list(PROVIDERS[provider]["fields"])
        public_fields = list(META_FIELDS.get(provider, ()))
        if not isinstance(values, dict):
            raise ValueError("values: ожидался объект {поле: значение}")

        cleaned: dict[str, str] = {}
        for name in fields:
            if name not in values:
                raise ValueError(f"{provider}: не заполнено поле {name!r}")
            raw = values[name]
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"{provider}.{name}: значение не может быть пустым")
            cleaned[name] = raw.strip()

        unknown = sorted(name for name in values if name not in fields)
        if unknown:
            raise ValueError(f"{provider}: неизвестные поля: {', '.join(unknown)}")

        public = {name: cleaned[name] for name in public_fields}
        if meta is not None:
            if not isinstance(meta, dict):
                raise ValueError("meta: ожидался объект {поле: значение}")
            undeclared = sorted(name for name in meta if name not in public_fields)
            if undeclared:
                raise ValueError(
                    f"{provider}: в meta допустимы только {public_fields or 'никакие поля'}"
                    f", пришло {', '.join(undeclared)}"
                )
            for name in public_fields:
                if name in meta:
                    value = meta[name]
                    if not isinstance(value, str):
                        raise ValueError(f"{provider}.{name}: ожидалась строка")
                    public[name] = value.strip()

        secret = {name: value for name, value in cleaned.items() if name not in public_fields}
        stored = self._providers()
        stored[provider] = {
            _ENCRYPTED_KEY: _encode(secret) if secret else "",
            _ADDED_KEY: _utcnow_iso(),
            _CHECK_KEY: None,
            _CHECK_OK_KEY: None,
            _META_KEY: public,
        }
        self._write(stored)

    def delete(self, provider: str) -> bool:
        """Удалить запись провайдера: ``True`` — была и удалена, ``False`` — не было.

        Неизвестное имя — тоже ``False``, а не исключение: для ``DELETE``-эндпоинта
        «нет такого ключа» и «нет такого провайдера» — один ответ (404), и решает
        его слой API, а не хранилище.
        """
        stored = self._providers()
        if provider not in stored:
            return False
        del stored[provider]
        self._write(stored)
        return True

    def mark_checked(self, provider: str, ok: bool) -> None:
        """Записать результат проверки ключа (когда проверка появится, P5).

        Незнакомый провайдер — ``ValueError`` (опечатка в имени — ошибка
        вызывающего); провайдер известный, но не настроенный — тихий выход:
        отмечать нечего, и это не повод падать.
        """
        self.require_provider(provider)
        stored = self._providers()
        entry = stored.get(provider)
        if not isinstance(entry, dict):
            return
        entry[_CHECK_KEY] = _utcnow_iso()
        entry[_CHECK_OK_KEY] = bool(ok)
        self._write(stored)

    def clear_all(self) -> None:
        """Удалить файл целиком («сбросить всё» и уборка в тестах). Идемпотентно."""
        self.path.unlink(missing_ok=True)

    # --- внутреннее -----------------------------------------------------------

    def require_provider(self, provider: str) -> None:
        """``ValueError``, если провайдера нет в :data:`PROVIDERS`."""
        if provider not in PROVIDERS:
            raise ValueError(f"неизвестный провайдер: {provider!r}")

    def _status(self, provider: str, entry: Any) -> ProviderStatus:
        """Собрать статус из записи файла (``None`` — записи нет)."""
        if not isinstance(entry, dict):
            return ProviderStatus(provider=provider, configured=False)
        encoded = entry.get(_ENCRYPTED_KEY)
        values = dict(_decode(encoded)) if isinstance(encoded, str) and encoded else {}
        values.update(self.public_meta(provider))
        checked = entry.get(_CHECK_KEY)
        last_check = checked if isinstance(checked, str) and checked else None
        ok = entry.get(_CHECK_OK_KEY)
        return ProviderStatus(
            provider=provider,
            configured=self._complete(provider, values),
            last_check=last_check,
            last_check_ok=ok if isinstance(ok, bool) else None,
        )

    def _complete(self, provider: str, values: dict[str, str]) -> bool:
        """Все объявленные поля провайдера заполнены непустыми строками."""
        return all(values.get(name) for name in PROVIDERS[provider]["fields"])

    def _read(self) -> dict[str, Any]:
        """Документ файла целиком; битый/чужой файл — ``load_error`` и пустой.

        Три состояния, которые нельзя смешивать (шаг P4b, §0.7):

        * файла нет — это норма (ключей ещё не вводили), ``load_error = None``;
        * файл есть, но не читается (не JSON / не объект / ``version`` не та) —
          ``load_error`` с причиной и пустой документ: статусы показываются как
          «не настроен», но UI обязан сказать, что ключи не загружены, а не
          делать вид, что их не было;
        * файл читается — документ как есть, ``load_error = None``.

        ``version`` проверяется строго: P5 обещает Fernet вместо base64, и
        читать чужой формат как свой значило бы показать «не настроено» вместо
        «файл от другой версии».
        """
        self.load_error = None
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": FORMAT_VERSION, "providers": {}}
        except OSError as error:  # нет прав, каталог вместо файла, битая кодировка
            self.load_error = f"файл не читается: {error}"
            return {"version": FORMAT_VERSION, "providers": {}}
        try:
            document = json.loads(text)
        except ValueError as error:
            self.load_error = f"не JSON: {error}"
            return {"version": FORMAT_VERSION, "providers": {}}
        if not isinstance(document, dict):
            self.load_error = "ожидался объект {version, providers}"
            return {"version": FORMAT_VERSION, "providers": {}}
        version = document.get("version")
        if version != FORMAT_VERSION:
            self.load_error = f"версия формата {version!r}, ожидалась {FORMAT_VERSION}"
            return {"version": FORMAT_VERSION, "providers": {}}
        return document

    def _providers(self) -> dict[str, dict[str, Any]]:
        """Записи провайдеров из файла; нет файла или мусор — пустой словарь.

        Записи неизвестных провайдеров сохраняются как есть и переживают
        следующую запись: файл может быть правлен новой версией приложения, и
        терять её данные при сохранении чужого ключа нельзя.
        """
        stored = self._read().get("providers")
        if not isinstance(stored, dict):
            if stored is not None and self.load_error is None:
                self.load_error = "поле providers не объект"
            return {}
        return {
            str(name): entry
            for name, entry in stored.items()
            if isinstance(entry, dict)
        }

    def _write(self, providers: dict[str, dict[str, Any]]) -> None:
        """Атомарно записать документ и ограничить права на файл."""
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        document = {"version": FORMAT_VERSION, "providers": providers}
        handle_fd, tmp_name = tempfile.mkstemp(
            prefix=".api_keys-", suffix=".tmp", dir=str(parent)
        )
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(document, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
            try:
                # mkstemp уже создаёт 0600, но режим — часть контракта файла,
                # и полагаться на деталь реализации mkstemp не стоит. Ошибка не
                # роняет запись: на Windows режим всё равно задаёт icacls.
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        _restrict_permissions(self.path)
        # Запись удалась — прежней ошибки чтения больше нет (шаг P4b): иначе
        # баннер «файл повреждён» висел бы после того, как пользователь сохранил
        # ключ заново и файл перезаписан целиком.
        self.load_error = None


# --- кодирование значений -----------------------------------------------------


def _encode(values: dict[str, str]) -> str:
    """``base64(json(values))`` — не криптография, но и не открытый текст (§18.2).

    ``sort_keys=True`` — чтобы одинаковый набор полей давал одинаковую строку:
    файл читают глазами и сравнивают в диффах, и «ключ изменился» не должно
    означать «переставились поля».
    """
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _decode(encoded: str) -> dict[str, str]:
    """Обратная к :func:`_encode`; битая строка — пустой словарь, не исключение.

    Битый ``encrypted_value`` (обрезанный файл, чужая версия формата) означает
    «ключ не читается»: провайдер показывается как не настроенный, а не валит
    экран настроек.
    """
    try:
        raw = base64.b64decode(encoded.encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - любая порча base64/JSON = «ключ не читается»
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(key): str(value) for key, value in obj.items()}


def _utcnow_iso() -> str:
    """Текущий момент в ISO 8601 UTC: секунды точности, ``Z`` в конце.

    Тот же формат, что у ``app.store.utcnow_iso`` (``2026-10-20T09:00:00Z``):
    строки сравнимы лексикографически, и одна форма времени на весь проект.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _restrict_permissions(path: Path) -> None:
    """Отдать файл только владельцу: ``icacls`` на Windows, на POSIX режим уже 0600.

    Копия подхода ``app/session.py`` (там то же делается для ``session.token``):
    ``os.chmod`` на Windows ACL не ограничивает, поэтому снимается наследование и
    владельцу выдаётся полный доступ. Код выхода не проверяем — локали Windows
    отвечают по-разному, важно только, что команда не упала; отсутствие
    ``icacls`` тоже не ошибка (права останутся унаследованными от каталога).

    Хелпер именно скопирован, а не вынесен в общий модуль: шаг P4a разрешает
    править только ``core/api_keys.py`` (``app/session.py`` в списке файлов нет),
    а зависимость ``app`` → ``core.api_keys`` ради пяти строк хуже дублирования.
    """
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    who = f"{domain}\\{user}" if domain and user else user
    if not who:
        return
    argv = ["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"]
    try:
        subprocess.run(  # noqa: S603 - argv из констант и имени пользователя
            argv, capture_output=True, timeout=_ICACLS_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return
