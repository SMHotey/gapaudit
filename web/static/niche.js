/* GapAudit — экран «Поиск ниши» (шаг P5f-1, DEVPLAN §19).
   Сквозной workflow: фраза → задача niche.find → ранжированный список
   подсфер по Gap Score.

   Два адреса, и оба нужны:
   * POST /api/niche/find — ставит задачу, отвечает 202 {job_id, status};
   * GET /api/jobs/{id}  — прогресс и статус задачи (поллинг раз в 1.5 с);
   * GET /api/niche/result/{id} — готовый result.json (200) либо 202 с
     прогрессом, если задача ещё идёт (защита от гонки: done пришёл, а файл
     пишется следующим шагом).

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
  let currentJobId = null;

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
    const items = data.items || [];
    const tbody = $('niche-body');
    tbody.replaceChildren();
    if (!items.length) {
      tbody.appendChild(emptyRow(
        'Ничего не найдено. Проверьте фразу или увеличьте страницы.', 6));
      $('niche-counter').textContent = '0 результатов';
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
    $('niche-counter').textContent = items.length + ' результатов'
      + (used ? ' · сигналы: ' + used : ' · сигналы: нет (ключи не настроены)');
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
      $('niche-progress').hidden = true;
      $('niche-progress-bar').value = 1;
      render(data || {});
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
      try {
        const resp = await api('POST', '/api/niche/find', payload);
        startPolling(resp.job_id);
      } catch (e) {
        showMsg('Ошибка: ' + e.message, 'err');
      }
    });

    $('refresh').addEventListener('click', load);
  });
})();
