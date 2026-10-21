/* GapAudit — экран «Поиск ниши» (шаг P5f-1, DEVPLAN §19).
   Сквозной workflow: фраза → задача niche.find → ранжированный список
   подсфер по Gap Score.

   Два адреса, и оба нужны:
   * POST /api/niche/find — ставит задачу, отвечает 202 {job_id, status};
   * GET /api/jobs/{id}  — прогресс и статус задачи (поллинг раз в 1.5 с);
   * GET /api/niche/result/{id} — готовый result.json (200) либо 202 с
     прогрессом, если задача ещё идёт (защита от гонки: done пришёл, а файл
     пишется следующим шагом).

   P5f-2 добавил три вещи, каждая — про «что я реально вижу»:
   * строку «Проверено N из M» (stats.checked / stats.found): проверяются
     первые CHECK_LIMIT строк каталога по звёздам, и без этой строки список
     выглядел полным;
   * фильтры результата (momentum > 0, без MCP, gap >= X) — чисто клиентские,
     перерисовка из уже полученного result.json, без новых запросов;
   * экспорт CSV/MD — POST /api/niche/export?job_id=...&format=..., файл
     скачивается браузером (Blob + <a download>).

   Данные (имена пакетов, вердикты, числа) рисуются только DOM-методами и
   textContent (gotcha 37): имя пакета с кавычкой не должно ломать вёрстку.
   innerHTML здесь нет вовсе — даже для пустого состояния строка собирается
   createElement'ом. */
(function () {
  const api = window.GapAudit.api;
  const $ = (id) => document.getElementById(id);

  const POLL_MS = 1500;
  const TERMINAL = ['failed', 'cancelled', 'interrupted'];

  let pollTimer = null;
  let currentJobId = null;   /* задача, за которой идёт поллинг */
  let lastResult = null;     /* последний result.json — источник перерисовки фильтров */
  let resultJobId = null;    /* чей это результат: нужен экспорту после остановки поллинга */

  /* Тело POST /api/niche/find: только известные поля; пустые чекбоксы
     означают «сигналы не собирать» (score по adoption + признаку ниши). */
  function collect() {
    const f = $('niche-form').elements;
    const kinds = [];
    if (f.signal_jobs.checked) kinds.push('jobs');
    if (f.signal_patents.checked) kinds.push('patents');
    if (f.signal_funding.checked) kinds.push('funding');
    if (f.signal_search.checked) kinds.push('search_interest');
    return {
      query: f.query.value.trim(),
      min_stars: parseInt(f.min_stars.value, 10) || 0,
      pages: parseInt(f.pages.value, 10) || 2,
      limit: parseInt(f.limit.value, 10) || 50,
      sources: ['github', 'npm', 'registry'],
      signal_kinds: kinds,
    };
  }

  function showMsg(text, kind) {
    const el = $('niche-msg');
    el.textContent = text || '';
    el.className = 'settings-msg' + (kind ? ' ' + kind : '');
    el.hidden = !text;
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function cell(text, className) {
    const td = document.createElement('td');
    if (className) td.className = className;
    td.textContent = text;
    return td;
  }

  function money(value) {
    return value == null ? '—' : Number(value).toLocaleString('ru-RU');
  }

  function momentum(value) {
    if (value == null) return '—';
    const arrow = value > 0 ? '↑ +' : (value < 0 ? '↓ ' : '→ ');
    return arrow + Number(value).toFixed(1) + '%';
  }

  /* Фильтры результата (P5f-2). Ни одно условие не выключено по умолчанию:
     пустые чекбоксы и gap = 0 — «показать всё». */
  function applyFilters(items) {
    const momentumPos = $('filter-momentum-positive').checked;
    const noFlag = $('filter-no-flag').checked;
    const gapMin = parseFloat($('filter-gap-min').value) || 0;
    return items.filter((it) => {
      if (momentumPos && !(it.momentum_30d > 0)) return false;
      if (noFlag && it.flag) return false;
      if (gapMin > 0 && !(it.gap_score >= gapMin)) return false;
      return true;
    });
  }

  /* Строка «Проверено N из M»: M — весь каталог, N — то, что реально проверено
     (CHECK_LIMIT строк по звёздам). Если чисел нет (старый result.json) —
     строка не показывается, а не врёт нулями. */
  function renderStats(data) {
    const stats = data.stats || {};
    const box = $('niche-stats');
    if (stats.checked == null || stats.found == null) {
      box.hidden = true;
      return;
    }
    box.textContent = 'Проверено ' + stats.checked + ' из ' + stats.found +
      ' по звёздам. Увеличьте лимит или проверяйте по требованию.';
    box.hidden = false;
  }

  function setExportEnabled(enabled) {
    $('niche-export-csv').disabled = !enabled;
    $('niche-export-md').disabled = !enabled;
  }

  function emptyRow(text, rows) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = rows;
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = text;
    td.appendChild(box);
    tr.appendChild(td);
    return tr;
  }

  function render(data) {
    /* Источник перерисовки — последний result.json: фильтры меняют только
       представление, новый запрос к серверу не нужен. */
    lastResult = data;
    renderStats(data);
    const all = data.items || [];
    const items = applyFilters(all);
    $('niche-filters').hidden = all.length === 0;
    const tbody = $('niche-body');
    tbody.replaceChildren();
    if (!items.length) {
      tbody.appendChild(emptyRow(all.length
        ? 'Под фильтры ничего не подошло — ослабьте условия.'
        : 'Ничего не найдено. Проверьте фразу или увеличьте страницы.', 6));
      $('niche-counter').textContent = all.length
        ? '0 из ' + all.length + ' результатов'
        : '0 результатов';
      return;
    }
    items.forEach((item, index) => {
      const tr = document.createElement('tr');
      /* Gap Score — главное число экрана: первым после имени, округлённое. */
      tr.appendChild(cell((index + 1) + '. ' + item.name, 'mono'));
      tr.appendChild(cell(item.gap_score == null
        ? '—' : String(Math.round(item.gap_score)), 'mono gap-cell'));
      tr.appendChild(cell(momentum(item.momentum_30d), 'mono'));
      tr.appendChild(cell(item.flag ? 'есть' : 'нет'));
      tr.appendChild(cell(money(item.adoption_week), 'mono'));
      tr.appendChild(cell(item.verdict || '—'));
      tbody.appendChild(tr);
    });
    const stats = data.stats || {};
    const used = (stats.signals_used || []).join(', ');
    const shown = items.length === all.length ? '' : ' из ' + all.length;
    $('niche-counter').textContent = items.length + shown + ' результатов'
      + (used ? ' · сигналы: ' + used : ' · сигналы: нет (ключи не настроены)');
  }

  /* Экспорт (P5f-2): POST /api/niche/export читает result.json задачи и отдаёт
     файл. Токен — заголовком (тот же запасной путь, что у api()): cookie
     HttpOnly, JS её не видит. Скачивание — Blob + временный <a download>. */
  async function downloadExport(fmt) {
    if (!resultJobId) return;
    const url = '/api/niche/export?job_id=' + encodeURIComponent(resultJobId) +
      '&format=' + fmt;
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'X-GapAudit-Token': window.GapAudit.token },
      });
      if (!resp.ok) {
        showMsg('Ошибка экспорта: ' + resp.status, 'err');
        return;
      }
      const blob = await resp.blob();
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'niche-' + resultJobId + '.' + fmt;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
    } catch (e) {
      showMsg('Ошибка экспорта: ' + e.message, 'err');
    }
  }

  function finishWithError(text) {
    stopPolling();
    currentJobId = null;
    $('niche-progress').hidden = true;
    showMsg(text, 'err');
  }

  /* Готовый результат: 200 — данные; 202 — задача ещё идёт (гонка с
     записью файла), продолжаем поллинг статуса. */
  async function fetchResult(jobId) {
    try {
      const data = await api('GET', '/api/niche/result/' + jobId);
      if (data && data.status) {
        $('niche-progress-bar').value = data.progress || 0;
        $('niche-progress-text').textContent =
          (data.message || data.status) + ' (' +
          Math.round((data.progress || 0) * 100) + '%)';
        return;
      }
      stopPolling();
      currentJobId = null;
      resultJobId = jobId;
      $('niche-progress').hidden = true;
      $('niche-progress-bar').value = 1;
      render(data || {});
      setExportEnabled(true);
    } catch (e) {
      finishWithError('Ошибка результата: ' + e.message);
    }
  }

  async function pollJob(jobId) {
    let job = null;
    try {
      job = await api('GET', '/api/jobs/' + jobId);
    } catch (e) {
      finishWithError('Ошибка статуса задачи: ' + e.message);
      return;
    }
    if (!job) return;
    if (job.status === 'done') {
      $('niche-progress-bar').value = 1;
      $('niche-progress-text').textContent = 'Готово, читаю результат…';
      await fetchResult(jobId);
      return;
    }
    if (TERMINAL.indexOf(job.status) >= 0) {
      finishWithError('Задача ' + job.status +
        (job.error ? ': ' + job.error : (job.message ? ': ' + job.message : '')));
      return;
    }
    $('niche-progress').hidden = false;
    $('niche-progress-bar').value = job.progress || 0;
    $('niche-progress-text').textContent = (job.message || job.status) +
      ' (' + Math.round((job.progress || 0) * 100) + '%)';
  }

  function startPolling(jobId) {
    currentJobId = jobId;
    $('niche-progress').hidden = false;
    $('niche-progress-bar').value = 0;
    $('niche-progress-text').textContent = 'Запуск…';
    stopPolling();
    pollJob(jobId);
    pollTimer = setInterval(() => pollJob(jobId), POLL_MS);
  }

  /* Обновление экрана: идущая задача — перечитать статус и результат;
     иначе — просто перечитать статус последней задачи (если он есть). */
  function load() {
    if (currentJobId) {
      pollJob(currentJobId);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    window.__gapauditRefresh = load;

    $('niche-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      showMsg('');
      const payload = collect();
      if (!payload.query) {
        showMsg('Введите фразу', 'err');
        return;
      }
      /* Новый поиск: прошлый результат больше не «текущий» — экспорт и фильтры
         относятся к нему, пока не пришёл новый. */
      lastResult = null;
      resultJobId = null;
      setExportEnabled(false);
      $('niche-filters').hidden = true;
      $('niche-stats').hidden = true;
      try {
        const resp = await api('POST', '/api/niche/find', payload);
        startPolling(resp.job_id);
      } catch (e) {
        /* 503 — демо-режим: Niche Finder там недоступен (P5f-2). Поллинг не
           начинается: задачи нет, ждать нечего — показываем причину как есть. */
        const text = String((e && e.message) || '');
        if (e && e.status === 503) {
          showMsg(text.replace(/^503:\s*/, ''), 'err');
        } else {
          showMsg('Ошибка: ' + text, 'err');
        }
      }
    });

    $('refresh').addEventListener('click', load);

    /* Фильтры — перерисовка из уже полученного результата. */
    ['filter-momentum-positive', 'filter-no-flag', 'filter-gap-min']
      .forEach((id) => {
        $(id).addEventListener('change', () => {
          if (lastResult) render(lastResult);
        });
      });

    $('niche-export-csv').addEventListener('click', () => downloadExport('csv'));
    $('niche-export-md').addEventListener('click', () => downloadExport('md'));
  });
})();
