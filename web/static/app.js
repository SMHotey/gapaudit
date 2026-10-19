/* GapAudit — общий клиент API (шаг 2a).
   Подключается в base.html на любой экран. Экранные скрипты живут в
   {% block scripts %} и работают через window.GapAudit.

   Токен читается из location.search: desktop.py открывает окно с ?token=...
   (вариант A из DEVPLAN §7). Дальше он уходит заголовком X-GapAudit-Token —
   в query токен допустим только для SSE (матрица токена в app/web.py). */
(function () {
  const token = new URLSearchParams(location.search).get('token') || '';

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  window.GapAudit = {
    token: token,

    /* Обёртка над fetch: JSON, токен,Error с полем status (нужен, чтобы
       отличать 404/409 «Продолжить» от прочих ошибок). */
    api: async function (method, path, body) {
      const opts = {
        method: method,
        headers: {
          'X-GapAudit-Token': token,
          'Content-Type': 'application/json',
        },
      };
      if (body !== undefined) opts.body = JSON.stringify(body);
      const resp = await fetch(path, opts);
      const text = await resp.text();
      let json = null;
      try { json = text ? JSON.parse(text) : null; } catch (e) { /* не JSON */ }
      if (!resp.ok) {
        const detail = (json && json.detail) || resp.statusText;
        const error = new Error(resp.status + ': ' + detail);
        error.status = resp.status;
        throw error;
      }
      return json;
    },

    /* /api/health: точки db/gh в шапке и каталог данных в подвале.
       gh_ok === null — проверить не удалось (серый), false — не авторизован. */
    health: async function () {
      try {
        const h = await this.api('GET', '/api/health');
        const db = document.getElementById('health-db');
        const gh = document.getElementById('health-gh');
        if (db) db.className = 'dot ' + (h.db_ok ? 'ok' : 'err');
        if (gh) {
          gh.className = 'dot ' +
            (h.gh_ok === true ? 'ok' : (h.gh_ok === false ? 'err' : ''));
        }
        setText('health-text', 'v' + h.version);
        setText('data-dir', h.data_dir || '—');
        const dir = document.getElementById('data-dir');
        if (dir) dir.title = h.data_dir || '';
      } catch (e) {
        const db = document.getElementById('health-db');
        const gh = document.getElementById('health-gh');
        if (db) db.className = 'dot err';
        if (gh) gh.className = 'dot err';
        setText('health-text', 'health недоступен');
      }
    },
  };

  /* Подтверждение закрытия окна. desktop.py зовёт window.confirmClose()
     (см. confirm_close_handler): false — окно остаётся. Экран «Задачи»
     подменяет эту функцию своей, чтобы спросить про активные задачи;
     здесь — безопасное значение по умолчанию, если экранный скрипт не
     загрузился (иначе undefined заблокировал бы закрытие навсегда). */
  window.confirmClose = function () { return true; };

  document.addEventListener('DOMContentLoaded', function () {
    window.GapAudit.health();
  });
})();
