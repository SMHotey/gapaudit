/* Карточка пакета (шаг 3a): общий модуль для «Сейчас решать», «Пакетов»
   и «Мониторинга».

   История: до 3a карточка жила внутри matrix.js, и второй экран с теми же
   данными (таблица «Пакетов», таймлайн «Мониторинга») получил бы свою копию
   — с расхождениями в полях и в обработке ошибок. Теперь один модуль:
   разметка оверлея — в base.html (одна на все экраны), логика — здесь.

   Правило (AGENTS gotcha 37): имя пакета, summary, вердикт и detail события
   приходят из задачи/PyPI. В DOM они попадают только через textContent /
   createTextNode, никогда через innerHTML.

   API:
     window.GapAuditCard.open(name)      — загрузить и открыть карточку;
     window.GapAuditCard.close()         — закрыть;
     window.GapAuditCard.render(name, data) — нарисовать из готовых данных
       (data = {package, observations, events, history}) — для тестов. */
(function () {
  const api = window.GapAudit.api;
  const $ = (id) => document.getElementById(id);
  const SVGNS = 'http://www.w3.org/2000/svg';

  let lastFocus = null;   /* куда вернуть фокус после закрытия */

  function svgEl(name, attrs, parent) {
    const el = document.createElementNS(SVGNS, name);
    Object.keys(attrs || {}).forEach((key) => el.setAttribute(key, attrs[key]));
    if (parent) parent.appendChild(el);
    return el;
  }

  function svgText(x, y, text, attrs, parent) {
    const el = svgEl('text', Object.assign({ x: x, y: y }, attrs || {}), parent);
    el.textContent = text;
    return el;
  }

  function fmtNum(value) {
    return (value || 0).toLocaleString('ru-RU');
  }

  function row(label, value, parent) {
    const labelEl = document.createElement('div');
    labelEl.className = 'muted';
    labelEl.textContent = label;
    const valueEl = document.createElement('div');
    valueEl.textContent = value || '—';
    parent.appendChild(labelEl);
    parent.appendChild(valueEl);
  }

  /* Мини-график adoption по истории. История приходит новыми сверху —
     для линии разворачиваем: время слева направо. */
  function miniGraph(observations) {
    const wrap = document.createElement('div');
    wrap.className = 'card-graph';
    const points = observations.slice().reverse();
    if (points.length < 2) {
      wrap.className = 'card-graph muted';
      wrap.textContent = 'История adoption: ' +
        points.length + ' замер(а) — линия появится со второго.';
      return wrap;
    }
    const weeks = points.map((p) => p.adoption_week || 0);
    const maxW = Math.max.apply(null, weeks.concat([1]));
    const n = points.length;
    const coords = points.map((p, i) => {
      const x = 20 + i * (360 / Math.max(1, n - 1));
      const y = 100 - ((p.adoption_week || 0) / maxW) * 80;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');

    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('viewBox', '0 0 400 120');
    svg.setAttribute('class', 'mini-graph');
    svgEl('polyline', {
      points: coords, fill: 'none', stroke: '#4aa3ff', 'stroke-width': '2',
    }, svg);
    svgText(400, 14, fmtNum(maxW) + '/нед', {
      'text-anchor': 'end', fill: '#8b98a5', 'font-size': '11',
    }, svg);
    wrap.appendChild(svg);
    return wrap;
  }

  function renderEvents(events, card) {
    const list = document.createElement('ul');
    list.className = 'card-events';
    if (!events.length) {
      const item = document.createElement('li');
      item.className = 'muted';
      item.textContent = 'Событий нет';
      list.appendChild(item);
    } else {
      events.slice(0, 10).forEach((event) => {
        const item = document.createElement('li');
        item.textContent = [event.at || '', event.kind || '',
                            event.detail || event.name || ''].join(' · ');
        list.appendChild(item);
      });
    }
    card.appendChild(list);
  }

  function overlay() { return $('card-overlay'); }
  function content() { return $('card-content'); }

  function close() {
    const box = overlay();
    const card = content();
    if (box) box.hidden = true;
    if (card) card.innerHTML = '';   /* очистка, не данные — gotcha 37 */
    if (lastFocus && lastFocus.focus) {
      try { lastFocus.focus(); } catch (e) { /* элемент ушёл из DOM */ }
    }
    lastFocus = null;
  }

  /* name, data = {package, observations, events, history} */
  function render(name, data) {
    const card = content();
    if (!card) return;
    const payload = data || {};
    const pkg = payload.package || {};
    const events = payload.events || [];
    const history = payload.history || [];
    card.innerHTML = '';

    const title = document.createElement('h2');
    title.textContent = name;
    card.appendChild(title);

    const latest = (payload.observations || [])[0] || {};
    const info = document.createElement('div');
    info.className = 'card-info';
    /* Версия и дата релиза живут в observations, а не в packages (схема §5):
       в строке пакета только source/url/summary. Поэтому берём из последнего
       замера и падаем на строку пакета, если замера нет. */
    row('источник', pkg.source || latest.source, info);
    row('версия', latest.version, info);
    row('релиз', latest.released_at, info);
    row('summary', pkg.summary, info);
    row('url', pkg.url, info);
    card.appendChild(info);

    const stats = document.createElement('div');
    stats.className = 'card-info';
    row('adoption/нед', latest.adoption_week != null
      ? fmtNum(latest.adoption_week) : '—', stats);
    row('adoption/мес', latest.adoption_month != null
      ? fmtNum(latest.adoption_month) : '—', stats);
    row('вердикт', latest.verdict, stats);
    /* В схеме две колонки (DEVPLAN §5): flag — метка ключевого слова (TEXT),
       flag_value — 0/1. Признак читаем из flag_value: bool("MCP") истинно
       всегда, и «MCP нет» на экране стало бы «есть». */
    row('признак ниши', latest.flag_value
      ? ('есть (' + (latest.flag || '') + ')') : 'нет', stats);
    row('замер', latest.measured_at, stats);
    card.appendChild(stats);

    /* Momentum (шаг P4b, §18.3): Δ adoption за 7/30/90 дней. Данные — из
       signals.momentum того же ответа /api/packages/{name} (вариант A шага:
       один запрос на карточку). Подпись '↑ +42.0%/30д' и цвет считает сервер
       (core.collectors.momentum): второй расчёт в JS разошёлся бы с матрицей
       при первой правке порога. */
    const signals = payload.signals || {};
    if (signals.momentum) {
      const m = signals.momentum;
      const block = document.createElement('div');
      block.className = 'card-momentum';
      const label = document.createElement('div');
      label.className = 'muted';
      label.textContent = 'Momentum';
      const value = document.createElement('div');
      value.className = 'momentum-value momentum-' + (m.color || 'gray');
      value.textContent = (m.label_30d || '—') +
        (m.delta_7d != null ? ' · 7д ' + m.delta_7d.toFixed(1) + '%' : '') +
        (m.delta_90d != null ? ' · 90д ' + m.delta_90d.toFixed(1) + '%' : '');
      block.appendChild(label);
      block.appendChild(value);
      if (m.samples < 2) {
        const note = document.createElement('div');
        note.className = 'muted';
        note.style.fontSize = '12px';
        note.textContent = 'Мало данных: ' + m.samples + ' замер(а)';
        block.appendChild(note);
      }
      card.appendChild(block);
    }

    /* Cross-ecosystem (шаг P4c, §18.3): 3 полосы (PyPI/npm/GitHub) с Δ
       adoption по концепту. Данные — из signals.cross_ecosystem того же ответа
       /api/packages/{name}; имя пакета, экосистема и числа попадают в DOM
       только через textContent (gotcha 37), innerHTML — только у статической
       шапки таблицы. Свои значения (self_*) лежат в сигнале: карточка не
       считает momentum второй раз на клиенте. */
    if (signals.cross_ecosystem) {
      const ce = signals.cross_ecosystem;
      const div = document.createElement('div');
      div.className = 'card-cross-eco';
      const label = document.createElement('div');
      label.className = 'muted';
      label.textContent = 'Cross-ecosystem · ' + ce.concept;
      div.appendChild(label);
      const table = document.createElement('table');
      table.className = 'cross-eco-table';
      const thead = document.createElement('thead');
      thead.innerHTML = '<tr><th>Пакет</th><th>Экосистема</th>' +
                        '<th>Δ 30д</th><th>adoption/нед</th></tr>';
      table.appendChild(thead);
      const tbody = document.createElement('tbody');
      /* сам пакет первой строкой (с подсветкой self) + соседи по концепту */
      const rows = [{
        name: name,
        ecosystem: ce.ecosystem,
        momentum_30d: ce.self_momentum_30d,
        adoption_week: ce.self_adoption_week,
        self: true,
      }].concat(ce.peers || []);
      rows.forEach((r) => {
        const tr = document.createElement('tr');
        if (r.self) tr.className = 'self';
        const tdName = document.createElement('td');
        tdName.textContent = r.name;
        tdName.className = 'mono';
        tr.appendChild(tdName);
        const tdEco = document.createElement('td');
        tdEco.textContent = r.ecosystem;
        tr.appendChild(tdEco);
        const tdMom = document.createElement('td');
        /* Ноль — стрелка «→», как в label_30d у momentum: «↓ 0.0%» соврало
           бы о знаке (роста нет, но и падения нет). */
        tdMom.textContent = r.momentum_30d != null
          ? (r.momentum_30d > 0 ? '↑ +' : r.momentum_30d < 0 ? '↓ ' : '→ ') +
            r.momentum_30d.toFixed(1) + '%'
          : '—';
        tdMom.className = 'mono';
        tr.appendChild(tdMom);
        const tdAdopt = document.createElement('td');
        tdAdopt.textContent = r.adoption_week != null
          ? r.adoption_week.toLocaleString('ru-RU') : '—';
        tdAdopt.className = 'mono';
        tr.appendChild(tdAdopt);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      div.appendChild(table);
      if (ce.comparison === 'faster') {
        const note = document.createElement('div');
        note.className = 'muted';
        note.style.fontSize = '12px';
        note.textContent = 'Пакет растёт быстрее соседей по концепту';
        div.appendChild(note);
      } else if (ce.comparison === 'slower') {
        const note = document.createElement('div');
        note.className = 'muted';
        note.style.fontSize = '12px';
        note.textContent = 'Пакет отстаёт от соседей по концепту';
        div.appendChild(note);
      }
      const anchor = card.querySelector('.card-graph');
      if (anchor) card.insertBefore(div, anchor);
      else card.appendChild(div);
    }

    /* Jobs (шаг P5a, §18.3): top-5 стран по вакансиям (Adzuna). Данные — из
       signals.jobs ответа /api/packages/{name}; имя страны и числа в DOM
       попадают только через textContent (gotcha 37), innerHTML — только у
       статической шапки таблицы. Три состояния различаются по signals_status:
       собран (таблица), ключ не настроен (ссылка в Настройки), ещё не
       собирали (блок не рисуется — прежнее поведение карточки P4c). */
    if (signals.jobs) {
      const j = signals.jobs;
      const div = document.createElement('div');
      div.className = 'card-jobs';
      const label = document.createElement('div');
      label.className = 'muted';
      label.textContent = 'Jobs · спрос на труд';
      div.appendChild(label);
      if (j.error) {
        const err = document.createElement('div');
        err.className = 'muted';
        err.textContent = 'Ошибка Adzuna: ' + j.error;
        div.appendChild(err);
      }
      const table = document.createElement('table');
      table.className = 'jobs-table';
      table.innerHTML = '<thead><tr><th>Страна</th><th>Вакансий</th></tr></thead>';
      const tbody = document.createElement('tbody');
      (j.by_country || []).slice(0, 5).forEach((row) => {
        const tr = document.createElement('tr');
        const tdC = document.createElement('td');
        tdC.textContent = row.label;
        tr.appendChild(tdC);
        const tdN = document.createElement('td');
        tdN.textContent = fmtNum(row.count);
        tdN.className = 'mono';
        tr.appendChild(tdN);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      div.appendChild(table);
      const total = document.createElement('div');
      total.className = 'muted';
      total.textContent = 'Всего: ' + fmtNum(j.total) + ' · источник: The Adzuna API';
      div.appendChild(total);
      const anchor0 = card.querySelector('.card-graph');
      if (anchor0) card.insertBefore(div, anchor0);
      else card.appendChild(div);
    } else if (payload.signals_status &&
               payload.signals_status.jobs === 'not_configured') {
      const div = document.createElement('div');
      div.className = 'card-jobs card-jobs-empty';
      const label = document.createElement('div');
      label.className = 'muted';
      label.textContent = 'Jobs';
      const msg = document.createElement('div');
      msg.className = 'muted';
      msg.textContent = 'API-ключ Adzuna не настроен — см. Настройки → API-ключи';
      div.appendChild(label);
      div.appendChild(msg);
      const anchor1 = card.querySelector('.card-graph');
      if (anchor1) card.insertBefore(div, anchor1);
      else card.appendChild(div);
    }

    /* Patents (шаг P5b, §18.3): патентный импульс PatentsView — таблица по
       годам + импульс + строка источника. Три состояния по signals_status:
       собран (таблица), ключ не настроен (ссылка в Настройки), ключ есть,
       но не собирали («ещё не собирали»; кнопка «Собрать» — P5f, §0.4).
       Данные в DOM попадают только через textContent (gotcha 37), innerHTML —
       только у статической шапки таблицы. */
    const patents = signals.patents;
    const patentsStatus = payload.signals_status &&
                          payload.signals_status.patents;
    if (patents || patentsStatus === 'not_configured' ||
        patentsStatus === 'not_collected') {
      const div = document.createElement('div');
      div.className = 'card-patents';
      const label = document.createElement('div');
      label.className = 'muted';
      label.textContent = 'Patents · патентный импульс';
      div.appendChild(label);
      if (patents && patents.configured) {
        const table = document.createElement('table');
        table.className = 'patents-table';
        table.innerHTML = '<thead><tr><th>Год</th><th>Патентов</th></tr></thead>';
        const tbody = document.createElement('tbody');
        (patents.by_year || []).forEach((row) => {
          const tr = document.createElement('tr');
          const tdY = document.createElement('td');
          tdY.textContent = row.year;
          tdY.className = 'mono';
          tr.appendChild(tdY);
          const tdC = document.createElement('td');
          tdC.textContent = fmtNum(row.count);
          tdC.className = 'mono';
          tr.appendChild(tdC);
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        div.appendChild(table);
        const total = document.createElement('div');
        total.className = 'muted';
        total.textContent = 'Всего: ' + fmtNum(patents.total) +
          ' · источник: PatentsView API';
        div.appendChild(total);
        if (patents.impulse_pct !== null &&
            patents.impulse_pct !== undefined) {
          const imp = document.createElement('div');
          imp.className = 'muted';
          const sign = patents.impulse_pct > 0 ? '+' : '';
          imp.textContent = 'Импульс: ' + sign +
            patents.impulse_pct.toFixed(1) + '% за 3 года';
          div.appendChild(imp);
        }
      } else if (patentsStatus === 'not_configured') {
        const msg = document.createElement('div');
        msg.className = 'muted';
        msg.textContent = 'API-ключ PatentsView не настроен — ' +
          'см. Настройки → API-ключи';
        div.appendChild(msg);
      } else {
        const msg = document.createElement('div');
        msg.className = 'muted';
        msg.textContent = 'Ещё не собирали';
        div.appendChild(msg);
      }
      const anchor2 = card.querySelector('.card-graph');
      if (anchor2) card.insertBefore(div, anchor2);
      else card.appendChild(div);
    }

    card.appendChild(miniGraph(history));

    const eventsTitle = document.createElement('div');
    eventsTitle.className = 'muted';
    eventsTitle.textContent = 'События';
    card.appendChild(eventsTitle);
    renderEvents(events, card);

    const actions = document.createElement('div');
    actions.className = 'card-actions';
    const verifyBtn = document.createElement('button');
    verifyBtn.type = 'button';
    verifyBtn.className = 'btn btn-primary';
    verifyBtn.textContent = 'Проверить по GitHub';
    verifyBtn.addEventListener('click', async () => {
      verifyBtn.disabled = true;
      verifyBtn.textContent = 'Ставлю задачу…';
      try {
        await api('POST', '/api/jobs', {
          kind: 'github.verify',
          params: { targets: [name], keyword: 'mcp' },
        });
        verifyBtn.textContent = 'Задача создана';
      } catch (e) {
        verifyBtn.textContent = 'Проверить по GitHub';
        verifyBtn.disabled = false;
        window.alert('Не удалось поставить задачу: ' + e.message);
      }
    });
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'btn';
    closeBtn.textContent = 'Закрыть';
    closeBtn.addEventListener('click', close);
    actions.appendChild(verifyBtn);
    actions.appendChild(closeBtn);
    card.appendChild(actions);
  }

  async function open(name) {
    const card = content();
    const box = overlay();
    if (!card || !box) return;
    lastFocus = document.activeElement;
    card.innerHTML = '';
    const loading = document.createElement('div');
    loading.className = 'muted';
    loading.textContent = 'Загружаю карточку…';
    card.appendChild(loading);
    box.hidden = false;
    try {
      const encoded = encodeURIComponent(name);
      /* История отдельным запросом: /api/packages/{name} отдаёт только
         последнее наблюдение, а мини-графику нужен ряд из /history. */
      const [cardData, historyData] = await Promise.all([
        api('GET', '/api/packages/' + encoded),
        api('GET', '/api/packages/' + encoded + '/history?limit=50'),
      ]);
      render(name, Object.assign({}, cardData, {
        history: (historyData && historyData.observations) || [],
      }));
    } catch (e) {
      card.innerHTML = '';
      const error = document.createElement('div');
      error.className = 'empty';
      error.textContent = 'Не удалось загрузить карточку: ' + e.message;
      card.appendChild(error);
    }
  }

  window.GapAuditCard = { open: open, close: close, render: render };

  document.addEventListener('DOMContentLoaded', function () {
    const box = overlay();
    if (!box) return;   /* страница без оверлея (статический ответ) */
    box.addEventListener('click', function (ev) {
      if (ev.target === box) close();
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && !box.hidden) close();
    });
  });
})();
