/* GapAudit — экран «Сравнение» (шаг 3c, DEVPLAN §9 п. 6).
   Таблица «параметр x пакет» из GET /api/compare?names=a,b,c. Колонки — пакеты,
   строки — метрики последнего замера; 2–3 имени, границу проверяет сервер (400).

   Данные рисуются DOM-методами (createElement/textContent), innerHTML для них
   запрещён (gotcha 37): имя пакета приходит из сети, и кавычка в нём сломала бы
   разметку. innerHTML здесь нет вообще — даже для статики.

   Экспорт: POST /api/compare/export — тот же набор имён, что на экране, файл в
   ответе. Ссылку создаём сами и отзываем после клика (blob-URL живёт до отзыва,
   а WebView2 держит его, пока открыта страница). */
(function () {
  const api = window.GapAudit.api;
  const $ = (id) => document.getElementById(id);

  /* Что показать в строках: те же поля, что отдаёт /api/compare. Порядок —
     порядок вопросов при выборе ниши (что, насколько живое, занято ли). */
  const ROWS = [
    ['source', 'Источник'],
    ['version', 'Версия'],
    ['released_at', 'Релиз'],
    ['adoption_week', 'Скачиваний/нед'],
    ['adoption_month', 'Скачиваний/мес'],
    ['flag_label', 'Признак (MCP)'],
    ['verdict', 'Вердикт'],
    ['measured_at', 'Замер'],
  ];

  let lastData = null;

  function fmt(value) {
    if (value === null || value === undefined || value === '') return '—';
    if (typeof value === 'number') return value.toLocaleString('ru-RU');
    return String(value);
  }

  function message(text, kind) {
    const box = $('compare-msg');
    box.textContent = text;
    box.className = 'settings-msg' + (kind ? ' ' + kind : '');
    box.hidden = false;
  }

  function emptyState(text) {
    $('compare-head').replaceChildren();
    const head = document.createElement('tr');
    const th = document.createElement('th');
    th.textContent = 'Параметр';
    head.appendChild(th);
    $('compare-head').appendChild(head);

    const row = document.createElement('tr');
    const cell = document.createElement('td');
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = text;
    cell.appendChild(box);
    row.appendChild(cell);
    $('compare-body').replaceChildren(row);
  }

  function render(data) {
    lastData = data;
    const packages = data.packages || [];
    const missing = data.missing || [];
    if (!packages.length) {
      emptyState(missing.length ? 'Нет данных: ' + missing.join(', ') : 'Нет данных');
      return;
    }

    const head = $('compare-head');
    head.replaceChildren();
    const headRow = document.createElement('tr');
    const paramTh = document.createElement('th');
    paramTh.textContent = 'Параметр';
    headRow.appendChild(paramTh);
    packages.forEach(function (pkg) {
      const th = document.createElement('th');
      th.className = 'mono';
      th.textContent = pkg.name;
      headRow.appendChild(th);
    });
    head.appendChild(headRow);

    const body = $('compare-body');
    body.replaceChildren();
    ROWS.forEach(function ([key, label]) {
      const row = document.createElement('tr');
      const labelCell = document.createElement('td');
      labelCell.className = 'muted';
      labelCell.textContent = label;
      row.appendChild(labelCell);
      packages.forEach(function (pkg) {
        const cell = document.createElement('td');
        cell.className = 'mono';
        cell.textContent = fmt(pkg[key]);
        row.appendChild(cell);
      });
      body.appendChild(row);
    });

    if (missing.length) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = packages.length + 1;
      cell.className = 'muted';
      cell.textContent = 'Нет данных: ' + missing.join(', ');
      row.appendChild(cell);
      body.appendChild(row);
    }
  }

  function currentNames() {
    return ($('compare-form').elements.names.value || '').trim();
  }

  async function load() {
    const names = currentNames();
    if (!names) {
      $('compare-msg').hidden = true;
      return;
    }
    try {
      const data = await api('GET', '/api/compare?names=' + encodeURIComponent(names));
      render(data);
      $('compare-msg').hidden = true;
    } catch (e) {
      // 400 — граница имён (1 или 4), 404 — ни одного замера: текст сервера
      // понятнее собственного «ошибка запроса», поэтому показываем его.
      lastData = null;
      emptyState('Сравнение не построено');
      message('Ошибка: ' + e.message, 'err');
    }
  }

  async function exportCsv() {
    if (!lastData || !lastData.packages || !lastData.packages.length) {
      message('Сначала постройте сравнение — выгружать нечего', 'err');
      return;
    }
    const names = lastData.packages.map((pkg) => pkg.name).join(',');
    const url = '/api/compare/export?format=csv&names=' + encodeURIComponent(names);
    const token = (window.GapAudit && window.GapAudit.token) || '';
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
      link.download = 'compare.csv';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () {
        URL.revokeObjectURL(href);
      }, 1000);
    } catch (e) {
      message('экспорт не удался: ' + e.message, 'err');
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    /* F5 = «Обновить» (gotcha 39): перечитывает то же сравнение, а не
       перезагружает страницу — иначе пропадут введённые имена. */
    window.__gapauditRefresh = load;

    $('compare-form').addEventListener('submit', function (ev) {
      ev.preventDefault();
      load();
    });
    $('refresh').addEventListener('click', load);
    $('export-csv').addEventListener('click', exportCsv);
  });
})();
