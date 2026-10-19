/* Экран «Сейчас решать» (шаг 2b): матрица adoption x признак + карточка пакета.

   Данные — GET /api/matrix (точки, квадранты, пороги, счётчики). Матрица
   рисуется в SVG «руками»: логарифмическая ось X (недельные скачивания),
   две строки по признаку ниши. Клик по точке — карточка пакета.

   Правило экрана (AGENTS gotcha 37): имена пакетов, вердикты и summary —
   данные из задачи/PyPI. Они попадают в DOM только через textContent /
   createTextNode, никогда через innerHTML: имя вида `<img onerror=...>`
   иначе стало бы разметкой, а кавычка в имени — сломанной вёрсткой. */
(function () {
  const api = window.GapAudit.api;
  const $ = (id) => document.getElementById(id);
  const SVGNS = 'http://www.w3.org/2000/svg';

  const X0 = 60;          /* левый край области графика */
  const X1 = 940;         /* правый край */
  const ROW_H = 160;      /* высота строки квадранта */
  const ROW_TOP = 20;     /* y верхней строки (с признаком) */
  const ROW_BOTTOM = 220; /* y нижней строки (без признака) */

  let current = null;     /* последний ответ /api/matrix */

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

  /* «1 пакет / 2 пакета / 5 пакетов» — сводка читается как русский текст. */
  function plural(n, one, few, many) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }

  /* Логарифмическая шкала: 1..max -> X0..X1. Порог входит в домен, иначе
     линия «порог 100/нед» уезжала бы за край, когда все точки ниже него.
     Домен нулевой ширины (одна точка) -> центр области. */
  function xScale(week, minW, maxW) {
    const lo = Math.log10(Math.max(1, minW));
    const hi = Math.log10(Math.max(1, maxW));
    const span = hi - lo;
    const t = span > 1e-9
      ? (Math.log10(Math.max(1, week)) - lo) / span
      : 0.5;
    return X0 + Math.max(0, Math.min(1, t)) * (X1 - X0);
  }

  function domain(data) {
    const weeks = data.points.map((p) => p.adoption_week);
    const threshold = data.thresholds.alive_weekly || 1;
    const low = Math.min.apply(null, weeks.concat([threshold]));
    /* Одна декада запаса слева: иначе при единственном пакете порог попадает
       ровно на левый край и его линия сливается с рамкой области. */
    return {
      min: Math.max(1, low / 10),
      max: Math.max.apply(null, weeks.concat([threshold])),
    };
  }

  function renderMatrix(data) {
    const svg = $('matrix-svg');
    svg.innerHTML = '';
    if (!data.points.length) {
      $('matrix-empty').hidden = false;
      return;
    }
    $('matrix-empty').hidden = true;

    const { min, max } = domain(data);
    const xthr = xScale(data.thresholds.alive_weekly, min, max);

    /* Подписи квадрантов: что означает каждая клетка. */
    const rows = [
      {
        y: ROW_TOP, label: 'с MCP',
        left: 'не взлетело', right: 'не лезть',
        fill: 'rgba(248,113,113,0.05)',
      },
      {
        y: ROW_BOTTOM, label: 'без MCP',
        left: 'рынка нет', right: 'ВОТ СЮДА',
        fill: 'rgba(74,222,128,0.06)',
      },
    ];

    rows.forEach((row) => {
      svgEl('rect', {
        x: X0, y: row.y, width: X1 - X0, height: ROW_H, fill: row.fill,
      }, svg);
      svgText(X0 + 6, row.y + 16, row.label, {
        fill: '#8b98a5', 'font-size': '12',
      }, svg);
      svgText(X0 + 10, row.y + 34, row.left, {
        fill: '#6e7b88', 'font-size': '11',
      }, svg);
      svgText(X1 - 10, row.y + 34, row.right, {
        fill: row.right === 'ВОТ СЮДА' ? '#4ade80' : '#6e7b88',
        'font-size': '12', 'text-anchor': 'end',
      }, svg);
      svgEl('line', {
        x1: X0, y1: row.y + ROW_H, x2: X1, y2: row.y + ROW_H,
        stroke: '#2a3542', 'stroke-width': '1',
      }, svg);
    });

    /* Порог: вертикальная линия через обе строки + подпись сверху. */
    svgEl('line', {
      x1: xthr, y1: ROW_TOP, x2: xthr, y2: ROW_BOTTOM + ROW_H,
      stroke: '#fbbf24', 'stroke-dasharray': '4 4', 'stroke-width': '1',
    }, svg);
    svgText(xthr, 14, 'порог ' + fmtNum(data.thresholds.alive_weekly) + '/нед', {
      fill: '#fbbf24', 'font-size': '10', 'text-anchor': 'middle',
    }, svg);

    /* Точки. Точки с одинаковым adoption в одной строке разводим по высоте:
       иначе они полностью накрывают друг друга и клик открывает последнюю. */
    const seen = { flag: 0, noflag: 0 };
    data.points.forEach((point) => {
      const rowKey = point.flag ? 'flag' : 'noflag';
      const rowY = point.flag ? ROW_TOP : ROW_BOTTOM;
      const slot = seen[rowKey] += 1;
      const shift = (slot % 5) * 22 - 44;   /* -44..44, пять полос */
      const cx = xScale(point.adoption_week, min, max);
      const cy = rowY + ROW_H / 2 + shift;
      const circle = svgEl('circle', {
        cx: cx.toFixed(1), cy: cy.toFixed(1), r: '6',
        fill: point.flag ? '#f87171' : '#4ade80',
        stroke: '#0f1419', 'stroke-width': '1',
        class: 'matrix-point',
      }, svg);
      circle.dataset.name = point.name;
      circle.setAttribute('tabindex', '0');
      circle.setAttribute('role', 'button');
      const title = svgEl('title', {}, circle);
      title.textContent = point.name + ' — ' + fmtNum(point.adoption_week) +
        '/нед, ' + (point.flag ? 'MCP есть' : 'MCP нет');
      circle.addEventListener('click', () => openCard(point.name));
      circle.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          openCard(point.name);
        }
      });
    });

    /* Ось X: границы домена как есть (adoption в неделю). */
    svgEl('line', {
      x1: X0, y1: ROW_BOTTOM + ROW_H, x2: X1, y2: ROW_BOTTOM + ROW_H,
      stroke: '#3a4856', 'stroke-width': '1',
    }, svg);
    svgText(X0, ROW_BOTTOM + ROW_H + 16, fmtNum(min) + '/нед', {
      fill: '#8b98a5', 'font-size': '11',
    }, svg);
    svgText(X1, ROW_BOTTOM + ROW_H + 16, fmtNum(max) + '/нед', {
      fill: '#8b98a5', 'font-size': '11', 'text-anchor': 'end',
    }, svg);
  }

  function renderCandidates(data) {
    const tbody = $('candidates-body');
    tbody.innerHTML = '';
    const names = data.quadrants.high_no_flag || [];
    if (!names.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 5;
      const box = document.createElement('div');
      box.className = 'empty';
      box.textContent = data.points.length
        ? 'Нет живых пакетов без признака — либо всё занято, либо ' +
          'adoption ниже порога.'
        : 'Данных с adoption нет — таблица заполнится после проверки пакетов.';
      td.appendChild(box);
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    const byName = {};
    data.points.forEach((point) => { byName[point.name] = point; });
    names.forEach((name) => {
      const point = byName[name];
      if (!point) return;
      const tr = document.createElement('tr');

      const nameTd = document.createElement('td');
      nameTd.className = 'mono';
      nameTd.textContent = point.name;
      tr.appendChild(nameTd);

      const weekTd = document.createElement('td');
      weekTd.textContent = fmtNum(point.adoption_week);
      tr.appendChild(weekTd);

      const verdictTd = document.createElement('td');
      verdictTd.textContent = point.verdict || '—';
      tr.appendChild(verdictTd);

      const measuredTd = document.createElement('td');
      measuredTd.className = 'mono';
      measuredTd.textContent = (point.measured_at || '').slice(0, 16);
      tr.appendChild(measuredTd);

      const actionTd = document.createElement('td');
      actionTd.className = 'col-actions';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn';
      btn.textContent = 'Карточка';
      btn.addEventListener('click', () => openCard(point.name));
      actionTd.appendChild(btn);
      tr.appendChild(actionTd);

      tbody.appendChild(tr);
    });
  }

  function renderSummary(data) {
    $('count-total').textContent = data.counts.total;
    $('count-total-word').textContent =
      plural(data.counts.total, 'пакет', 'пакета', 'пакетов');
    $('count-with-week').textContent = data.counts.with_week;
    $('count-high-no-flag').textContent = data.counts.high_no_flag;
    const stamps = data.points
      .map((point) => point.measured_at || '')
      .filter((value) => value);
    if (stamps.length) {
      $('matrix-updated').textContent =
        'замер: ' + stamps.sort()[stamps.length - 1].slice(0, 16);
    } else {
      $('matrix-updated').textContent = 'замеров нет';
    }
  }

  async function load() {
    try {
      current = await api('GET', '/api/matrix');
      renderSummary(current);
      renderMatrix(current);
      renderCandidates(current);
    } catch (e) {
      $('matrix-svg').innerHTML = '';
      $('matrix-empty').hidden = false;
      $('matrix-empty').textContent = 'Не удалось загрузить матрицу: ' + e.message;
    }
  }

  /* --- карточка пакета ---------------------------------------------------- */

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

  function closeCard() {
    $('card-overlay').hidden = true;
    $('card-content').innerHTML = '';
  }

  function renderCard(name, card_data, history) {
    const pkg = card_data.package || {};
    const events = card_data.events || [];
    const card = $('card-content');
    card.innerHTML = '';

    const title = document.createElement('h2');
    title.textContent = name;
    card.appendChild(title);

    const latest = (card_data.observations || [])[0] || {};
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
    row('признак ниши', latest.flag_value ? ('есть (' + (latest.flag || '') + ')') : 'нет', stats);
    row('замер', latest.measured_at, stats);
    card.appendChild(stats);

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
    closeBtn.addEventListener('click', closeCard);
    actions.appendChild(verifyBtn);
    actions.appendChild(closeBtn);
    card.appendChild(actions);
  }

  async function openCard(name) {
    const card = $('card-content');
    card.innerHTML = '';
    const loading = document.createElement('div');
    loading.className = 'muted';
    loading.textContent = 'Загружаю карточку…';
    card.appendChild(loading);
    $('card-overlay').hidden = false;
    try {
      const encoded = encodeURIComponent(name);
      /* История отдельным запросом: /api/packages/{name} отдаёт только
         последнее наблюдение (карточка «Пакеты» P3), а мини-графику нужен
         ряд из /history. Оба эндпоинта уже есть с шага 2 — Store не трогаем. */
      const [cardData, historyData] = await Promise.all([
        api('GET', '/api/packages/' + encoded),
        api('GET', '/api/packages/' + encoded + '/history?limit=50'),
      ]);
      renderCard(name, cardData, (historyData && historyData.observations) || []);
    } catch (e) {
      card.innerHTML = '';
      const error = document.createElement('div');
      error.className = 'empty';
      error.textContent = 'Не удалось загрузить карточку: ' + e.message;
      card.appendChild(error);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('refresh').addEventListener('click', load);

    $('card-overlay').addEventListener('click', function (ev) {
      if (ev.target === $('card-overlay')) closeCard();
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && !$('card-overlay').hidden) closeCard();
    });

    if (window.GapAuditSSE) {
      window.GapAuditSSE.subscribe(function (event) {
        if (event !== 'done') return;
        /* Наблюдения пишет хук lifespan в отдельном пуле (шаг 3d-b): событие
           done уходит в SSE раньше, чем строка ложится в SQLite. Поэтому
           перечитываем не сразу, а с небольшой задержкой — иначе точка новой
           задачи не попала бы в ответ. */
        setTimeout(load, 400);
      });
    }

    load();
  });
})();
