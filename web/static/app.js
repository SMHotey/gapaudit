/* GapAudit — общий клиент API (шаг 2a).
   Подключается в base.html на любой экран. Экранные скрипты живут в
   {% block scripts %} и работают через window.GapAudit.

   Токен. С шага 3a основной путь — HttpOnly cookie `gapaudit_token`: сервер
   выставляет её на первом же переходе с ?token= (см. LocalGuardMiddleware
   в app/web.py), и дальше навигация по ссылкам работает без query. JS токен
   в cookie не видит (HttpOnly) и видеть не должен — поэтому он остаётся
   запасным путём: токен из ?token= кладётся в sessionStorage и уходит
   заголовком X-GapAudit-Token. Запасной путь нужен старым закладкам и
   запросам, сделанным до первого HTML-перехода (когда cookie ещё нет), а
   также SSE: EventSource не умеет заголовки и берёт токен из query.

   Порядок важен: новый запуск окна приносит новый токен в query, и он должен
   побеждать запомненный — иначе после перезапуска desktop.py вкладка со
   старым токеном получала бы 403. */
(function () {
  const TOKEN_STORAGE_KEY = 'gapaudit-token';

  function resolveToken() {
    const fromQuery = new URLSearchParams(location.search).get('token') || '';
    if (fromQuery) {
      try { sessionStorage.setItem(TOKEN_STORAGE_KEY, fromQuery); } catch (e) { /* приватный режим */ }
      return fromQuery;
    }
    try { return sessionStorage.getItem(TOKEN_STORAGE_KEY) || ''; } catch (e) { return ''; }
  }

  const token = resolveToken();

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
        setText('version', 'GapAudit v' + h.version);
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

  /* F5 = «Обновить» (шаг 2c, DEVPLAN §9). В WebView2 поведение F5 по умолчанию
     не определено (может открыть devtools или не сделать ничего), поэтому
     клавишу перехватываем сами: экран, определивший window.__gapauditRefresh
     (jobs.js / matrix.js / settings.js), перечитывает свои данные; остальные
     экраны получают обычную перезагрузку страницы. */
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'F5') return;
    e.preventDefault();
    if (typeof window.__gapauditRefresh === 'function') {
      window.__gapauditRefresh();
    } else {
      location.reload();
    }
  });
})();
