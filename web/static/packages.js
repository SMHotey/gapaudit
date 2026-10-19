/* Экран «Пакеты» (шаг 3a): поиск по имени/описанию, таблица с пагинацией,
   карточка пакета в общем оверлее (card.js).

   Данные — GET /api/packages/search?q=&limit=&offset=; ответ несёт total, по
   нему считается число страниц. Строки собираются DOM-методами: имя и summary
   приходят из задачи/PyPI, и innerHTML превратил бы `<img onerror=...>` в
   разметку, а кавычку в имени — в сломанную вёрстку (AGENTS gotcha 37).
   innerHTML здесь только для очистки контейнеров — данных в нём нет. */
(function () {
  const api = window.GapAudit.api;
  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 50;
  const state = { q: '', offset: 0, total: 0 };

  const NO_PACKAGES = 'Пакетов нет. Запустите проверку: POST /api/jobs ' +
    '{"kind":"packages.check","params":{"names":["..."]}}.';
  const NO_MATCH = 'Ничего не найдено по этому запросу. Очистите поле поиска.';

  function plural(n, one, few, many) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  function cell(text, cls) {
    const td = document.createElement('td');
    if (cls) td.className = cls;
    td.textContent = text;
    return td;
  }

  function emptyRow(text) {
    const tbody = $('packages-body');
    tbody.innerHTML = '';
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5;
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = text;
    td.appendChild(box);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  function row(pkg) {
    const tr = document.createElement('tr');
    tr.appendChild(cell(pkg.name, 'mono'));
    tr.appendChild(cell(pkg.source || '—'));
    tr.appendChild(cell(pkg.summary || '—', 'muted'));
    /* Время замера — ISO-строка из SQLite: «2026-10-19T10:00» читается
       глазом лучше, чем полная метка с секундами и Z. */
    tr.appendChild(cell((pkg.last_seen || '—').slice(0, 16).replace('T', ' '), 'mono'));

    const actions = document.createElement('td');
    actions.className = 'col-actions';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn';
    btn.textContent = 'Карточка';
    btn.addEventListener('click', function () {
      window.GapAuditCard.open(pkg.name);
    });
    actions.appendChild(btn);
    tr.appendChild(actions);
    return tr;
  }

  function render(packages) {
    if (!packages.length) {
      emptyRow(state.q ? NO_MATCH : NO_PACKAGES);
      return;
    }
    const tbody = $('packages-body');
    tbody.innerHTML = '';
    packages.forEach((pkg) => tbody.appendChild(row(pkg)));
  }

  function renderCounter() {
    $('packages-counter').textContent = state.total + ' ' +
      plural(state.total, 'пакет', 'пакета', 'пакетов') +
      (state.q ? ' · поиск: ' + state.q : '');
  }

  function renderPagination() {
    const el = $('packages-pagination');
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
      q: state.q, limit: PAGE_SIZE, offset: state.offset,
    });
    try {
      const data = await api('GET', '/api/packages/search?' + params.toString());
      state.total = data.total || 0;
      render(data.packages || []);
      renderCounter();
      renderPagination();
    } catch (e) {
      emptyRow('Не удалось получить список пакетов: ' + e.message);
      $('packages-counter').textContent = 'ошибка';
      $('packages-pagination').innerHTML = '';
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    /* F5 = «Обновить» (AGENTS gotcha 39). */
    window.__gapauditRefresh = load;

    let debounce = null;
    $('packages-q').addEventListener('input', function (ev) {
      clearTimeout(debounce);
      const value = ev.target.value.trim();
      debounce = setTimeout(function () {
        state.q = value;
        state.offset = 0;
        load();
      }, 250);
    });
    $('refresh').addEventListener('click', load);
    load();
  });
})();
