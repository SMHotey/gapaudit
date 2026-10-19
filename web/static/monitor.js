/* Экран «Мониторинг» (шаг 3a): таймлайн событий events с фильтрами.

   Данные — GET /api/events?kind=&name=&limit=&offset=; ответ несёт total, по
   нему считается число страниц. Строки собираются DOM-методами: имя пакета и
   detail события приходят из задачи, и innerHTML сделал бы из них разметку
   (AGENTS gotcha 37). innerHTML здесь только для очистки контейнеров.

   Клик по имени открывает общую карточку пакета (window.GapAuditCard,
   /static/card.js) — тот же оверлей, что на «Сейчас решать» и «Пакетах». */
(function () {
  const api = window.GapAudit.api;
  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 50;
  const state = { kind: '', name: '', offset: 0, total: 0 };

  /* Подписи видов событий — как их называет core/diffing.py. Неизвестный вид
     (новый kind до правки экрана) показывается кодом, а не пустым местом. */
  const KIND_LABEL = {
    release: 'Релиз',
    flag_appeared: 'MCP появился',
    flag_removed: 'MCP убран',
    adoption_change: 'Δ adoption',
    verdict_change: 'Вердикт изменился',
    new: 'Новое',
    missing: 'Пропало',
  };
  const KIND_COLOR = {
    flag_appeared: 'var(--ok)',
    flag_removed: 'var(--warn)',
    release: 'var(--accent)',
  };

  const NO_EVENTS = 'Событий нет. Поставьте задачу: POST /api/jobs ' +
    '{"kind":"monitor.diff","params":{"diff_from":"raw/predecessors.json"}}.';
  const NO_MATCH = 'Нет событий с этими фильтрами. Сбросьте фильтр или ' +
    'нажмите «Все».';

  function plural(n, one, few, many) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  function emptyTimeline(text) {
    const el = $('monitor-timeline');
    el.innerHTML = '';
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = text;
    el.appendChild(box);
  }

  function item(event) {
    const wrap = document.createElement('div');
    wrap.className = 'timeline-item';

    const time = document.createElement('div');
    time.className = 'timeline-time mono';
    time.textContent = (event.at || '').slice(0, 16).replace('T', ' ');
    wrap.appendChild(time);

    const body = document.createElement('div');
    body.className = 'timeline-body';

    const label = document.createElement('span');
    label.className = 'timeline-kind';
    label.style.color = KIND_COLOR[event.kind] || 'var(--text-dim)';
    label.textContent = KIND_LABEL[event.kind] || event.kind || '—';
    body.appendChild(label);

    const nameLink = document.createElement('a');
    nameLink.href = '#';
    nameLink.className = 'mono';
    nameLink.textContent = event.name || '—';
    nameLink.addEventListener('click', function (ev) {
      /* hash в href гасим: WebView2 прыгнул бы к якорю и потерял прокрутку. */
      ev.preventDefault();
      window.GapAuditCard.open(event.name);
    });
    body.appendChild(nameLink);

    if (event.detail) {
      const detail = document.createElement('span');
      detail.className = 'muted';
      detail.textContent = ' — ' + event.detail;
      body.appendChild(detail);
    }

    wrap.appendChild(body);
    return wrap;
  }

  function render(events) {
    if (!events.length) {
      emptyTimeline(state.kind || state.name ? NO_MATCH : NO_EVENTS);
      return;
    }
    const el = $('monitor-timeline');
    el.innerHTML = '';
    events.forEach((event) => el.appendChild(item(event)));
  }

  function renderCounter() {
    $('monitor-counter').textContent = state.total + ' ' +
      plural(state.total, 'событие', 'события', 'событий');
  }

  function renderPagination() {
    const el = $('monitor-pagination');
    el.innerHTML = '';
    const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
    const current = Math.floor(state.offset / PAGE_SIZE) + 1;
    if (pages <= 1) return;

    const prev = document.createElement('button');
    prev.type = 'button';
    prev.className = 'btn';
    prev.textContent = '←';
    prev.disabled = current === 1;
    prev.addEventListener('click', function () {
      state.offset = Math.max(0, state.offset - PAGE_SIZE);
      load();
    });

    const label = document.createElement('span');
    label.className = 'muted';
    label.textContent = ' ' + current + ' / ' + pages + ' ';

    const next = document.createElement('button');
    next.type = 'button';
    next.className = 'btn';
    next.textContent = '→';
    next.disabled = current === pages;
    next.addEventListener('click', function () {
      state.offset = state.offset + PAGE_SIZE;
      load();
    });

    el.appendChild(prev);
    el.appendChild(label);
    el.appendChild(next);
  }

  async function load() {
    const params = new URLSearchParams({
      kind: state.kind,
      name: state.name,
      limit: PAGE_SIZE,
      offset: state.offset,
    });
    try {
      const data = await api('GET', '/api/events?' + params.toString());
      state.total = data.total || 0;
      render(data.events || []);
      renderCounter();
      renderPagination();
    } catch (e) {
      emptyTimeline('Не удалось получить события: ' + e.message);
      $('monitor-counter').textContent = 'ошибка';
      $('monitor-pagination').innerHTML = '';
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    /* F5 = «Обновить» (AGENTS gotcha 39). */
    window.__gapauditRefresh = load;

    $('kind-filter').addEventListener('click', function (ev) {
      const btn = ev.target.closest('.chip');
      if (!btn) return;
      document.querySelectorAll('#kind-filter .chip').forEach(
        (chip) => chip.classList.remove('active'));
      btn.classList.add('active');
      state.kind = btn.dataset.kind || '';
      state.offset = 0;
      load();
    });

    let debounce = null;
    $('monitor-name').addEventListener('input', function (ev) {
      clearTimeout(debounce);
      const value = ev.target.value.trim();
      debounce = setTimeout(function () {
        state.name = value;
        state.offset = 0;
        load();
      }, 250);
    });

    $('refresh').addEventListener('click', load);
    load();
  });
})();
