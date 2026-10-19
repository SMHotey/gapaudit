/* Экран «Настройки» (шаг 2c; вкладки «Пороги» / «API-ключи» — шаг P4a).

   Две вкладки одной страницы: «Пороги» читает GET /api/settings и пишет
   PUT /api/settings, «API-ключи» — GET/PUT /api/settings/api-keys и
   POST .../{provider}/check, DELETE .../{provider}. Значения приходят из API и
   попадают в DOM только через value/textContent — в HTML их нет (AGENTS
   gotcha 37: данные не собираются разметкой из строк; здесь карточки строятся
   исключительно DOM-методами).

   Значений ключей API не отдаёт (только статус), поэтому после перезапуска окна
   поля пустые, а статус — «настроен»: пользователь может перезаписать ключ, и
   старые значения уйдут. Исключение — несекретные поля (meta_fields: user_agent,
   client_id, actor_id): они возвращаются как meta_public и подставляются в поле,
   иначе было бы не видно, что именно введено.

   Ответ PUT /api/settings содержит warnings («пороги повлияют на матрицу»,
   «max_parallel_jobs — после перезапуска»); экран показывает их рядом с
   «Сохранено», а не модальным окном. */
(function () {
  const api = window.GapAudit.api;

  /* Числовые поля формы и имена полей Settings совпадают один-в-один:
     так форма и контракт PUT не расходятся при добавлении порога. */
  const NUMBER_FIELDS = [
    'alive_weekly', 'alive_monthly', 'dead_weekly', 'dead_monthly',
    'stale_days', 'adoption_delta_pct', 'cache_ttl_hours',
    'max_parallel_jobs', 'registry_limit',
  ];

  /* Состояние провайдера -> подпись и класс бейджа. Классы цветов —
     существующие (style.css на шаге P4a не меняется): серый «не настроен»,
     нейтральный «настроен», зелёный «проверка пройдена», красный «ошибка».
     Машинное состояние отдаётся отдельно, в data-status: по нему его читают
     тесты UI-контракта, а не по русскому тексту. */
  const PROVIDER_STATUS = {
    not_configured: { text: 'не настроен', cls: 'status status-cancelled' },
    configured: { text: 'настроен', cls: 'status status-queued' },
    check_ok: { text: 'проверка пройдена', cls: 'status status-done' },
    check_error: { text: 'ошибка проверки', cls: 'status status-failed' },
  };

  let current = null;   /* последний ответ GET /api/settings */

  function form() { return document.getElementById('settings-form'); }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function showMsg(elementId, text, kind) {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.textContent = text;
    el.className = 'settings-msg ' + (kind || '');
    el.hidden = false;
  }

  function hideMsg(elementId) {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.hidden = true;
    el.textContent = '';
  }

  /* Результат «Проверить» / ошибка сохранения — внутри карточки провайдера,
     а не в общем сообщении экрана: пользователь смотрит на конкретный ключ. */
  function showResult(node, text, kind) {
    node.textContent = text;
    node.className = 'check-result settings-msg ' + (kind || '');
    node.hidden = false;
  }

  // --- вкладки ---------------------------------------------------------------

  function showTab(name) {
    const panels = document.querySelectorAll('[data-tab-panel]');
    Array.prototype.forEach.call(panels, function (panel) {
      panel.hidden = panel.getAttribute('data-tab-panel') !== name;
    });
    const tabs = document.querySelectorAll('[data-tab]');
    Array.prototype.forEach.call(tabs, function (button) {
      const on = button.getAttribute('data-tab') === name;
      button.className = on ? 'btn btn-primary tab-btn' : 'btn tab-btn';
      button.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }

  // --- вкладка «Пороги» (шаг 2c) --------------------------------------------

  function fill(data) {
    current = data;
    const settings = data.settings || {};
    const fields = form().elements;
    NUMBER_FIELDS.forEach(function (name) {
      if (fields[name]) fields[name].value = settings[name];
    });
    fields.keywords.value = (settings.keywords || []).join('\n');

    const readonly = data.readonly || {};
    document.getElementById('ro-data-dir').textContent = readonly.data_dir || '—';
    document.getElementById('ro-db-path').textContent = readonly.db_path || '—';
    document.getElementById('ro-version').textContent =
      'GapAudit v' + (readonly.version || '—');
  }

  async function loadSettings() {
    try {
      const data = await api('GET', '/api/settings');
      fill(data);
      hideMsg('settings-msg');
    } catch (e) {
      showMsg('settings-msg', 'Не удалось загрузить настройки: ' + e.message, 'err');
    }
  }

  /* Форма -> тело PUT. Пустые строки keywords отброшены: пользователь мог
     оставить лишний перевод строки, и это не должен быть «пустое слово». */
  function collect() {
    const fields = form().elements;
    const body = {};
    NUMBER_FIELDS.forEach(function (name) {
      body[name] = parseFloat(fields[name].value);
    });
    body.keywords = fields.keywords.value.split('\n')
      .map(function (word) { return word.trim(); })
      .filter(function (word) { return word.length > 0; });
    return body;
  }

  // --- вкладка «API-ключи» (шаг P4a) ---------------------------------------

  function providerState(provider) {
    if (!provider.configured) return 'not_configured';
    if (provider.last_check_ok === true) return 'check_ok';
    if (provider.last_check_ok === false) return 'check_error';
    return 'configured';
  }

  function statusBadge(provider) {
    const state = providerState(provider);
    const spec = PROVIDER_STATUS[state];
    const badge = document.createElement('span');
    badge.className = spec.cls;
    badge.setAttribute('data-status', state);
    badge.textContent = spec.text;
    if (provider.last_check) badge.title = 'проверка: ' + provider.last_check;
    return badge;
  }

  function docLink(text, anchor) {
    const link = document.createElement('a');
    link.href = '/docs/API-KEYS.md' + (anchor ? '#' + anchor : '');
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = text;
    return link;
  }

  /* Поле ввода: секретные — password (значение API не отдаёт, поле всегда
     пустое), несекретные (meta_fields) — text с подставленным meta_public. */
  function fieldInput(provider, name) {
    const isPublic = (provider.meta_fields || []).indexOf(name) >= 0;
    const input = document.createElement('input');
    input.className = 'search-input';
    input.type = isPublic ? 'text' : 'password';
    input.name = name;
    input.autocomplete = 'off';
    input.required = true;
    if (isPublic) input.value = (provider.meta_public || {})[name] || '';
    return input;
  }

  function actionButton(text, cls, act) {
    const button = document.createElement('button');
    button.type = act === 'save' ? 'submit' : 'button';
    button.className = cls;
    if (act !== 'save') button.setAttribute('data-act', act);
    button.textContent = text;
    return button;
  }

  function providerCard(provider) {
    const card = document.createElement('fieldset');
    card.className = 'provider-card';
    card.setAttribute('data-provider', provider.id);

    const legend = document.createElement('legend');
    legend.textContent = provider.label;
    card.appendChild(legend);
    card.appendChild(statusBadge(provider));

    const hint = document.createElement('p');
    hint.className = 'muted';
    hint.textContent = 'Требуется: ' + (provider.fields || []).join(', ') + '. ';
    hint.appendChild(docLink('как получить ключ', provider.docs_anchor));
    card.appendChild(hint);

    /* Несекретные значения показываем и текстом: поле можно случайно
       очистить, а видеть, что настроено, нужно. */
    const meta = provider.meta_public || {};
    const publicNames = Object.keys(meta);
    if (publicNames.length) {
      const info = document.createElement('div');
      info.className = 'readonly-info';
      publicNames.forEach(function (name) {
        const row = document.createElement('div');
        const label = document.createElement('span');
        label.className = 'muted';
        label.textContent = name + ':';
        const value = document.createElement('code');
        value.textContent = meta[name];
        row.appendChild(label);
        row.appendChild(value);
        info.appendChild(row);
      });
      card.appendChild(info);
    }

    const providerForm = document.createElement('form');
    providerForm.className = 'provider-form';
    (provider.fields || []).forEach(function (name) {
      const label = document.createElement('label');
      label.appendChild(document.createTextNode(name + ':'));
      label.appendChild(fieldInput(provider, name));
      providerForm.appendChild(label);
    });

    const actions = document.createElement('div');
    actions.className = 'form-actions';
    const save = actionButton('Сохранить', 'btn btn-primary', 'save');
    const check = actionButton('Проверить', 'btn', 'check');
    const remove = actionButton('Удалить', 'btn btn-danger', 'delete');
    actions.appendChild(save);
    actions.appendChild(check);
    actions.appendChild(remove);
    providerForm.appendChild(actions);

    const result = document.createElement('div');
    result.className = 'check-result settings-msg';
    result.hidden = true;
    providerForm.appendChild(result);
    card.appendChild(providerForm);

    providerForm.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      const values = {};
      const empty = (provider.fields || []).filter(function (name) {
        const value = (providerForm.elements[name].value || '').trim();
        if (value) values[name] = value;
        return !value;
      });
      if (empty.length) {
        showResult(result, 'Заполните поле: ' + empty.join(', '), 'err');
        return;
      }
      save.disabled = true;
      try {
        await api('PUT', '/api/settings/api-keys', {
          provider: provider.id,
          values: values,
        });
        showMsg('api-keys-msg', 'Ключ ' + provider.label + ' сохранён.', 'ok');
        await loadProviders();   /* статус — из API, не «на память» */
      } catch (e) {
        showResult(result, 'Ошибка: ' + e.message, 'err');
      } finally {
        save.disabled = false;
      }
    });

    check.addEventListener('click', async function () {
      check.disabled = true;
      try {
        const resp = await api(
          'POST',
          '/api/settings/api-keys/' + encodeURIComponent(provider.id) + '/check'
        );
        showResult(
          result,
          resp.reason || (resp.ok ? 'Ключ работает.' : 'Проверка не пройдена.'),
          resp.ok ? 'ok' : 'err'
        );
      } catch (e) {
        showResult(result, 'Ошибка: ' + e.message, 'err');
      } finally {
        check.disabled = false;
      }
    });

    remove.addEventListener('click', async function () {
      if (!window.confirm('Удалить ключ ' + provider.label + '?')) return;
      remove.disabled = true;
      try {
        await api(
          'DELETE',
          '/api/settings/api-keys/' + encodeURIComponent(provider.id)
        );
        showMsg('api-keys-msg', 'Ключ ' + provider.label + ' удалён.', 'ok');
        await loadProviders();
      } catch (e) {
        showResult(result, 'Ошибка: ' + e.message, 'err');
        remove.disabled = false;
      }
    });

    return card;
  }

  async function loadProviders() {
    const list = document.getElementById('providers-list');
    if (!list) return;
    try {
      const data = await api('GET', '/api/settings/api-keys');
      clear(list);
      (data.providers || []).forEach(function (provider) {
        list.appendChild(providerCard(provider));
      });
    } catch (e) {
      showMsg('api-keys-msg', 'Не удалось загрузить список ключей: ' + e.message, 'err');
    }
  }

  /* F5 = «Обновить»: обе вкладки, активную вкладку не сбрасываем. */
  async function load() {
    await loadSettings();
    await loadProviders();
  }

  document.addEventListener('DOMContentLoaded', function () {
    window.__gapauditRefresh = load;

    const tabs = document.querySelectorAll('[data-tab]');
    Array.prototype.forEach.call(tabs, function (button) {
      button.addEventListener('click', function () {
        showTab(button.getAttribute('data-tab'));
      });
    });

    form().addEventListener('submit', async function (ev) {
      ev.preventDefault();
      const button = form().querySelector('button[type=submit]');
      button.disabled = true;
      try {
        const resp = await api('PUT', '/api/settings', collect());
        fill({
          settings: resp.settings,
          readonly: (current && current.readonly) || {},
          editable_keys: (current && current.editable_keys) || [],
        });
        const warnings = resp.warnings || [];
        showMsg(
          'settings-msg',
          'Сохранено.' + (warnings.length ? ' ' + warnings.join(' ') : ''),
          'ok'
        );
      } catch (e) {
        showMsg('settings-msg', 'Ошибка: ' + e.message, 'err');
      } finally {
        button.disabled = false;
      }
    });

    document.getElementById('reset').addEventListener('click', function () {
      if (current) fill(current);
      hideMsg('settings-msg');
    });

    load();
  });
})();
