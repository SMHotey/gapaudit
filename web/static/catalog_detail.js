/* Экран каталога (шаг 3b): элементы с серверными фильтрами, пагинация, экспорт.

   Данные — GET /api/catalogs/{id} (метаданные) и GET /api/catalogs/{id}/items
   (страница). Фильтры серверные: каталог бывает на тысячи строк, и грузить их
   все ради фильтрации в браузере — ровно то, чего план запрещает (DEVPLAN §9).

   Строки собираются DOM-методами, innerHTML — только для очистки контейнера:
   имя проекта и описание приходят из GitHub/npm, то есть от чужих людей
   (AGENTS gotcha 37). Имя — внешняя ссылка на GitHub: каталожная запись хранит
   только имя (url в схеме §5 нет), а для GitHub-строки имя и есть owner/repo.

   Пока каталога нет, экран не врёт «не найден»: он смотрит статус задачи с тем
   же id и обновляется сам. Запись каталога асинхронна и приходит на событие
   done на сотни миллисекунд позже (AGENTS gotcha 38), поэтому опрос — 3 с. */
(function () {
  const api = window.GapAudit.api;
  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 100;
  const RETRY_MS = 3000;
  const REFRESH_AFTER_DONE_MS = 400;

  const match = location.pathname.match(/\/catalogs\/([^/?#]+)/);
  const catalogId = match ? decodeURIComponent(match[1]) : '';
  const state = { alive: '', minStars: '', q: '', offset: 0, total: 0 };
  let retryTimer = null;
  let meta = null;

  const NO_ITEMS = 'В каталоге нет элементов. Пересоберите его с другими фразами.';
  const NO_MATCH = 'Нет элементов с этими фильтрами. Сбросьте фильтр или нажмите «Все».';

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
    const tbody = $('items-body');
    tbody.innerHTML = '';
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 6;
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = text;
    td.appendChild(box);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  /* Ссылка на проект: сначала url из источника находки (миграция 2, шаг 3c) —
     GitHub отдаёт html_url, npm — links.npm. Если url нет (старый каталог,
     запись реестра без ссылки), работает прежнее правило: owner/repo — прямая
     страница GitHub, одиночное имя (пакет npm) — поиск GitHub по имени. Обе
     ведут наружу в новой вкладке: уходить из приложения в текущей нельзя. */
  function projectUrl(item) {
    const url = String((item && item.url) || '');
    if (url.indexOf('http://') === 0 || url.indexOf('https://') === 0) return url;
    const value = String((item && item.name) || '');
    if (!value) return '';
    if (value.indexOf('/') >= 0) return 'https://github.com/' + value;
    return 'https://github.com/search?q=' + encodeURIComponent(value) +
      '&type=repositories';
  }

  function row(item) {
    const tr = document.createElement('tr');

    const nameCell = document.createElement('td');
    const link = document.createElement('a');
    link.className = 'mono catalog-link';
    link.href = projectUrl(item);
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = item.name || '—';
    nameCell.appendChild(link);
    tr.appendChild(nameCell);

    tr.appendChild(
      cell(item.stars === null || item.stars === undefined ? '—' : String(item.stars), 'mono')
    );
    tr.appendChild(cell(item.lang || '—'));
    tr.appendChild(cell(item.pushed || '—', 'mono'));
    tr.appendChild(cell(item.desc || '—', 'muted'));
    tr.appendChild(cell(item.source || '—'));
    return tr;
  }

  function render(items) {
    if (!items.length) {
      emptyRow(state.alive || state.q || state.minStars ? NO_MATCH : NO_ITEMS);
      return;
    }
    const tbody = $('items-body');
    tbody.innerHTML = '';
    items.forEach((item) => tbody.appendChild(row(item)));
  }

  function setTitle(text) {
    $('catalog-title').textContent = text;
  }

  function renderTitle() {
    const queries = (meta && meta.queries) || [];
    const shortId = String((meta && meta.id) || catalogId).slice(0, 8);
    setTitle('Каталог ' + shortId + (queries.length ? ' · ' + queries.join(', ') : ''));
    $('catalog-title').title = queries.length
      ? 'Фразы: ' + queries.join(', ')
      : '';
  }

  function renderCounter() {
    const filters = [];
    if (state.alive === 'true') filters.push('живые');
    if (state.alive === 'false') filters.push('заброшенные');
    if (state.q) filters.push('поиск: ' + state.q);
    if (state.minStars) filters.push('от ' + state.minStars + ' ★');
    $('items-counter').textContent = state.total + ' ' +
      plural(state.total, 'элемент', 'элемента', 'элементов') +
      (filters.length ? ' · ' + filters.join(' · ') : '');
  }

  function renderPagination() {
    const el = $('items-pagination');
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
      loadItems();
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
      loadItems();
    });

    el.appendChild(prev);
    el.appendChild(label);
    el.appendChild(next);
  }

  function itemsQuery() {
    const params = new URLSearchParams({
      alive: state.alive,
      limit: PAGE_SIZE,
      offset: state.offset,
    });
    if (state.q) params.set('q', state.q);
    if (state.minStars) params.set('min_stars', state.minStars);
    return params.toString();
  }

  async function loadItems() {
    try {
      const data = await api(
        'GET',
        '/api/catalogs/' + encodeURIComponent(catalogId) + '/items?' + itemsQuery()
      );
      state.total = data.total || 0;
      render(data.items || []);
      renderCounter();
      renderPagination();
    } catch (e) {
      emptyRow('Не удалось получить элементы каталога: ' + e.message);
      $('items-counter').textContent = 'ошибка';
      $('items-pagination').innerHTML = '';
    }
  }

  function scheduleRetry() {
    if (retryTimer) return;
    retryTimer = setTimeout(function () {
      retryTimer = null;
      load();
    }, RETRY_MS);
  }

  /* Каталога ещё нет: смотрим задачу с тем же id. queued/running — «собирается»
     и повтор через 3 с; иначе сборка завершилась, а каталога нет (хук не смог
     записать) — говорим прямо, вместо бесконечного «загрузка». */
  async function handleMissing() {
    let job = null;
    try {
      job = await api('GET', '/api/jobs/' + encodeURIComponent(catalogId));
    } catch (e) {
      job = null;
    }
    const status = job && job.status;
    if (status === 'queued' || status === 'running') {
      setTitle('Каталог собирается…');
      const percent = job.progress ? Math.round(job.progress * 100) + '%' : 'в очереди';
      $('items-counter').textContent = percent;
      emptyRow(
        'Каталог ещё собирается (' +
          (status === 'running' ? 'идёт сборка' : 'задача в очереди') +
          '). Экран обновится сам, когда появится результат.'
      );
      scheduleRetry();
      return;
    }
    setTitle('Каталог ' + String(catalogId).slice(0, 8));
    $('items-counter').textContent = 'не найден';
    emptyRow(
      'Каталог не найден' +
        (status ? ' (задача: ' + status + ')' : '') +
        '. Вернитесь к списку: «← К списку».'
    );
  }

  async function load() {
    if (!catalogId) {
      setTitle('Каталог');
      emptyRow('Не удалось определить каталог из адреса страницы.');
      return;
    }
    try {
      meta = await api('GET', '/api/catalogs/' + encodeURIComponent(catalogId));
    } catch (e) {
      if (e.status === 404) {
        await handleMissing();
        return;
      }
      setTitle('Каталог ' + String(catalogId).slice(0, 8));
      emptyRow('Не удалось прочитать каталог: ' + e.message);
      return;
    }
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    renderTitle();
    await loadItems();
  }

  /* Экспорт: POST с токеном заголовком, ответ — файл. Ссылку создаём сами и
     гасим после клика: blob-URL живёт до отзыва, а WebView2 держит его, пока
     страница открыта. */
  async function exportFile(format) {
    if (!catalogId) return;
    const token = (window.GapAudit && window.GapAudit.token) || '';
    const url = '/api/catalogs/' + encodeURIComponent(catalogId) +
      '/export?format=' + format;
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'X-GapAudit-Token': token },
      });
      if (!resp.ok) throw new Error(resp.status + ' ' + resp.statusText);
      const blob = await resp.blob();
      const href = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = 'catalog-' + catalogId + '.' + format;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () {
        URL.revokeObjectURL(href);
      }, 1000);
    } catch (e) {
      $('items-counter').textContent = 'экспорт не удался: ' + e.message;
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    /* F5 = «Обновить» (AGENTS gotcha 39). */
    window.__gapauditRefresh = load;

    $('alive-filter').addEventListener('click', function (ev) {
      const btn = ev.target.closest('.chip');
      if (!btn) return;
      document.querySelectorAll('#alive-filter .chip').forEach(
        (chip) => chip.classList.remove('active')
      );
      btn.classList.add('active');
      state.alive = btn.dataset.alive || '';
      state.offset = 0;
      loadItems();
    });

    let starsTimer = null;
    $('filter-stars').addEventListener('input', function (ev) {
      clearTimeout(starsTimer);
      const value = ev.target.value.trim();
      starsTimer = setTimeout(function () {
        state.minStars = value === '' ? '' : String(Math.max(0, parseInt(value, 10) || 0));
        state.offset = 0;
        loadItems();
      }, 300);
    });

    let qTimer = null;
    $('filter-q').addEventListener('input', function (ev) {
      clearTimeout(qTimer);
      const value = ev.target.value.trim();
      qTimer = setTimeout(function () {
        state.q = value;
        state.offset = 0;
        loadItems();
      }, 250);
    });

    $('export-csv').addEventListener('click', function () {
      exportFile('csv');
    });
    $('export-md').addEventListener('click', function () {
      exportFile('md');
    });
    $('refresh').addEventListener('click', load);

    /* Задача завершилась — каталог уже почти в БД (gotcha 38: done приходит
       раньше записи, поэтому не синхронный reload, а короткая задержка). */
    if (window.GapAuditSSE) {
      window.GapAuditSSE.subscribe(function (event, data) {
        if (event !== 'done' || !data || data.job_id !== catalogId) return;
        setTimeout(load, REFRESH_AFTER_DONE_MS);
      });
    }

    load();
  });
})();
