/* Экран «Задачи» (шаг 2a): список, фильтр по статусу, отмена/продолжение,
   ручное обновление. Живого обновления (SSE) здесь нет намеренно — оно в 2b. */
(function () {
  const $ = (id) => document.getElementById(id);
  const api = window.GapAudit.api;

  /* Коды, при которых кнопка «Продолжить» объясняет причину, а не «ошибка»
     (app/web.py::resume_job: 404 — задачи нет, 409 — не interrupted/нет лога).
     Числа приходят из шаблона (window.GapAuditJobsConfig), чтобы правда о
     контракте жила в одном месте. */
  const RESUME_ERRORS = (window.GapAuditJobsConfig || {}).resumeErrors || [];

  const NO_JOBS = 'Нет задач. Поставьте первую через POST /api/jobs ' +
    'или дождитесь, пока P3 добавит кнопки запуска.';
  const NO_MATCH = 'Нет задач с этим статусом. Сбросьте фильтр или ' +
    'поставьте задачу через POST /api/jobs.';

  let currentStatus = '';
  let allJobs = [];   // последний ответ /api/jobs (без фильтра — см. load)

  function fmtProgress(p) { return Math.round((p || 0) * 100) + '%'; }
  function fmtId(id) { return id ? id.slice(0, 8) : '—'; }

  /* Метка времени: сегодня — только время, иначе дата + время. */
  function fmtTime(value) {
    if (!value) return '—';
    const d = new Date(value);
    if (isNaN(d.getTime())) return value;
    const today = new Date();
    if (d.toDateString() === today.toDateString()) {
      return d.toLocaleTimeString('ru-RU');
    }
    return d.toLocaleString('ru-RU', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    });
  }

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

  function progressCell(job) {
    const td = document.createElement('td');
    const wrap = document.createElement('span');
    wrap.className = 'progress-wrap';
    const bar = document.createElement('progress');
    bar.className = 'progress';
    bar.max = 1;
    bar.value = Math.max(0, Math.min(1, job.progress || 0));
    const pct = document.createElement('span');
    pct.className = 'pct';
    pct.textContent = fmtProgress(job.progress);
    wrap.appendChild(bar);
    wrap.appendChild(pct);
    td.appendChild(wrap);
    return td;
  }

  function actionsCell(job) {
    const td = document.createElement('td');
    td.className = 'col-actions';
    if (job.status === 'queued' || job.status === 'running') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-danger';
      btn.dataset.act = 'cancel';
      btn.dataset.id = job.id;
      btn.textContent = 'Отмена';
      td.appendChild(btn);
    }
    if (job.status === 'interrupted') {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-primary';
      btn.dataset.act = 'resume';
      btn.dataset.id = job.id;
      btn.textContent = 'Продолжить';
      td.appendChild(btn);
    }
    return td;
  }

  /* Строка собирается DOM-методами, а не innerHTML: message приходит из
     stdout подпроцесса и не должен трактоваться как разметка. */
  function renderRow(job) {
    const tr = document.createElement('tr');
    tr.dataset.status = job.status;
    tr.appendChild(cell(fmtId(job.id), 'mono'));
    tr.appendChild(cell(job.kind));
    const statusTd = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'status status-' + job.status;
    badge.textContent = job.status;
    statusTd.appendChild(badge);
    tr.appendChild(statusTd);
    tr.appendChild(progressCell(job));
    tr.appendChild(cell(job.message || '—', 'muted'));
    tr.appendChild(cell(fmtTime(job.started_at), 'mono'));
    tr.appendChild(actionsCell(job));
    tr.title = job.id;   /* полный id — по наведению */
    return tr;
  }

  function renderEmpty(text) {
    const tbody = $('jobs-body');
    tbody.innerHTML = '';
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 7;
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = text;
    td.appendChild(box);
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  function renderCounter(shown) {
    const suffix = currentStatus ? ' (фильтр: ' + currentStatus + ')' : '';
    $('counter').textContent = shown + ' ' +
      plural(shown, 'задача', 'задачи', 'задач') + suffix;
  }

  function render() {
    const jobs = currentStatus
      ? allJobs.filter((j) => j.status === currentStatus)
      : allJobs;
    if (!jobs.length) {
      renderEmpty(allJobs.length ? NO_MATCH : NO_JOBS);
      renderCounter(0);
      return;
    }
    const tbody = $('jobs-body');
    tbody.innerHTML = '';
    jobs.forEach((job) => tbody.appendChild(renderRow(job)));
    renderCounter(jobs.length);
  }

  /* Список берём без ?status= и фильтруем здесь: confirmClose и счётчик
     «в работе» должны видеть все задачи, иначе под фильтром «готово» окно
     закрывалось бы не спросив про работающую задачу. Серверный фильтр
     /api/jobs?status= остаётся и покрыт тестами эндпоинта. */
  async function load() {
    try {
      const data = await api('GET', '/api/jobs');
      allJobs = (data && data.jobs) || [];
      render();
    } catch (e) {
      renderEmpty('Не удалось получить список задач: ' + e.message);
      $('counter').textContent = 'ошибка';
    }
  }

  /* Замена app.js-заглушки: desktop.py спрашивает перед закрытием окна. */
  window.confirmClose = function () {
    const active = allJobs.filter(
      (j) => j.status === 'queued' || j.status === 'running').length;
    const what = active > 0
      ? 'Активных задач: ' + active + '. '
      : '';
    return window.confirm(what + 'Остановить и выйти?');
  };

  document.addEventListener('DOMContentLoaded', function () {
    $('refresh').addEventListener('click', load);

    $('status-filter').addEventListener('click', function (ev) {
      const btn = ev.target.closest('.chip');
      if (!btn) return;
      document.querySelectorAll('#status-filter .chip').forEach(
        (c) => c.classList.remove('active'));
      btn.classList.add('active');
      currentStatus = btn.dataset.status || '';
      render();
    });

    $('jobs-body').addEventListener('click', async function (ev) {
      const btn = ev.target.closest('button[data-act]');
      if (!btn) return;
      const id = btn.dataset.id;
      const act = btn.dataset.act;
      btn.disabled = true;
      try {
        const path = act === 'cancel'
          ? '/api/jobs/' + id + '/cancel'
          : '/api/jobs/' + id + '/resume';
        await api('POST', path);
        await load();
      } catch (e) {
        const known = act === 'resume' && RESUME_ERRORS.indexOf(e.status) >= 0;
        window.alert((known ? 'Продолжить задачу нельзя. ' : 'Ошибка. ') +
                     e.message);
        btn.disabled = false;
      }
    });

    load();
  });
})();
