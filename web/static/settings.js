/* Экран «Настройки» (шаг 2c): пороги, keywords, readonly-поля.

   Читает GET /api/settings, сохраняет PUT /api/settings. Значения приходят из
   API и попадают в DOM только через value/textContent — в HTML их нет
   (AGENTS gotcha 37: данные не собираются innerHTML'ом).

   Ответ PUT содержит новые значения и warnings: «пороги повлияют на матрицу —
   нажмите Обновить» / «max_parallel_jobs вступит в силу после перезапуска».
   Экран показывает их рядом с «Сохранено», а не модальным окном: пользователь
   должен видеть, что изменение принято, но пересчёт ещё не сделан. */
(function () {
  const api = window.GapAudit.api;

  /* Числовые поля формы и имена полей Settings совпадают один-в-один:
     так форма и контракт PUT не расходятся при добавлении порога. */
  const NUMBER_FIELDS = [
    'alive_weekly', 'alive_monthly', 'dead_weekly', 'dead_monthly',
    'stale_days', 'adoption_delta_pct', 'cache_ttl_hours',
    'max_parallel_jobs', 'registry_limit',
  ];

  let current = null;   /* последний ответ GET /api/settings */

  function form() { return document.getElementById('settings-form'); }

  function showMsg(text, kind) {
    const el = document.getElementById('settings-msg');
    el.textContent = text;
    el.className = 'settings-msg ' + (kind || '');
    el.hidden = false;
  }

  function hideMsg() {
    const el = document.getElementById('settings-msg');
    el.hidden = true;
    el.textContent = '';
  }

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

  async function load() {
    try {
      const data = await api('GET', '/api/settings');
      fill(data);
      hideMsg();
    } catch (e) {
      showMsg('Не удалось загрузить настройки: ' + e.message, 'err');
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

  document.addEventListener('DOMContentLoaded', function () {
    /* F5 = «Обновить» (app.js перехватывает клавишу и зовёт эту функцию). */
    window.__gapauditRefresh = load;

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
          'Сохранено.' + (warnings.length ? ' ' + warnings.join(' ') : ''),
          'ok'
        );
      } catch (e) {
        showMsg('Ошибка: ' + e.message, 'err');
      } finally {
        button.disabled = false;
      }
    });

    document.getElementById('reset').addEventListener('click', function () {
      if (current) fill(current);
      hideMsg();
    });

    load();
  });
})();
