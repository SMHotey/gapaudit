/* Экран «Каталоги» (шаг 3b): список каталогов и форма сборки по фразам.

   Данные — GET /api/catalogs (id, queries, items_count, created_at).
   Строки собираются DOM-методами: фразы и id приходят из задачи, и innerHTML
   сделал бы из них разметку (AGENTS gotcha 37). innerHTML здесь только для
   очистки контейнеров — данных в нём нет.

   Сборка — POST /api/catalogs, ответ 202 {job_id, catalog_id}: экран сразу
   уходит на /catalogs/{catalog_id}, а тот покажет «каталог собирается», пока
   хук lifespan не запишет результат задачи. Ждать здесь нечего — сборка идёт
   минутами, и «подождите» модального окна в проекте нет (DEVPLAN §9). */
(function () {
  const api = window.GapAudit.api;
  const $ = (id) => document.getElementById(id);

  const NO_CATALOGS = 'Каталогов нет. Соберите первый: «Новый каталог» — ' +
    'поиск идёт по GitHub, npm и реестру MCP, это занимает минуты.';

  const SOURCES = ['github', 'npm', 'registry'];

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
    const tbody = $('catalogs-body');
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

  function navigate(catalogId) {
    location.href = '/catalogs/' + encodeURIComponent(catalogId);
  }

  function row(catalog) {
    const tr = document.createElement('tr');
    tr.title = 'Открыть каталог ' + catalog.id;

    tr.appendChild(cell(String(catalog.id || '').slice(0, 8), 'mono'));
    const queries = catalog.queries || [];
    tr.appendChild(cell(queries.length ? queries.join(', ') : '—'));
    tr.appendChild(cell(String(catalog.items_count || 0), 'mono'));
    tr.appendChild(
      cell((catalog.created_at || '—').slice(0, 16).replace('T', ' '), 'mono')
    );

    const actions = document.createElement('td');
    actions.className = 'col-actions';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn';
    btn.textContent = 'Открыть';
    btn.addEventListener('click', function (ev) {
      /* строка тоже кликабельна: гасим всплытие, чтобы не открыть дважды */
      ev.stopPropagation();
      navigate(catalog.id);
    });
    actions.appendChild(btn);
    tr.appendChild(actions);

    tr.addEventListener('click', function () {
      navigate(catalog.id);
    });
    return tr;
  }

  function render(catalogs) {
    if (!catalogs.length) {
      emptyRow(NO_CATALOGS);
      return;
    }
    const tbody = $('catalogs-body');
    tbody.innerHTML = '';
    catalogs.forEach((catalog) => tbody.appendChild(row(catalog)));
  }

  function showMessage(kind, text) {
    const box = $('catalog-msg');
    box.className = 'settings-msg ' + kind;
    box.textContent = text;
    box.hidden = false;
  }

  function hideMessage() {
    const box = $('catalog-msg');
    box.hidden = true;
    box.textContent = '';
    box.className = 'settings-msg';
  }

  async function load() {
    try {
      const data = await api('GET', '/api/catalogs');
      render(data.catalogs || []);
    } catch (e) {
      emptyRow('Не удалось получить список каталогов: ' + e.message);
    }
  }

  async function submitCatalog(ev) {
    ev.preventDefault();
    const form = ev.target;
    const phrases = String(form.elements.phrases.value || '')
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    if (!phrases.length) {
      showMessage('err', 'Введите хотя бы одну фразу — по одной на строку.');
      return;
    }
    const body = {
      phrases: phrases,
      min_stars: Number(form.elements.min_stars.value || 0),
      pages: Number(form.elements.pages.value || 2),
      sources: SOURCES.slice(),
    };
    try {
      const data = await api('POST', '/api/catalogs', body);
      showMessage('ok', 'Задача создана, каталог появится после завершения.');
      $('new-catalog-form').hidden = true;
      /* Экран каталога сам покажет «собирается» и обновится — так пользователь
         видит, куда уехала задача, и может закрыть окно без потери идеи. */
      setTimeout(function () {
        navigate(data.catalog_id);
      }, 700);
      load();
    } catch (e) {
      showMessage('err', 'Не удалось создать задачу: ' + e.message);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    /* F5 = «Обновить» (AGENTS gotcha 39). */
    window.__gapauditRefresh = load;

    $('new-catalog').addEventListener('click', function () {
      hideMessage();
      $('new-catalog-form').hidden = false;
      const area = document.querySelector('#catalog-form textarea');
      if (area) area.focus();
    });

    $('cancel-new').addEventListener('click', function () {
      hideMessage();
      $('new-catalog-form').hidden = true;
    });

    $('catalog-form').addEventListener('submit', submitCatalog);
    $('refresh').addEventListener('click', load);
    load();
  });
})();
