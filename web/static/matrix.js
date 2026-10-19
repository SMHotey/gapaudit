/* Экран «Сейчас решать» (шаг 2b): матрица adoption x признак + карточка пакета.

   Данные — GET /api/matrix (точки, квадранты, пороги, счётчики). Матрица
   рисуется в SVG «руками»: логарифмическая ось X (недельные скачивания),
   две строки по признаку ниши. Клик по точке — карточка пакета: с шага 3a
   её рисует общий модуль card.js (window.GapAuditCard), а не этот файл.

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

  /* --- momentum (шаг P4b, §18.3) ------------------------------------------
     Цвет точки — это Δ adoption за 30 дней, а не признак ниши. До P4b
     зелёная точка означала «MCP нет» и читалась как «хорошо»; теперь «высокий
     adoption» перестал быть синонимом «хорошо»: зелёный — растёт, красный —
     падает, серый — стабилен или данных мало. Порог ±5% и подписи приходят с
     сервера (momentum_color), здесь только три цвета. */
  const MOMENTUM_COLORS = { green: '#4ade80', gray: '#94a3b8', red: '#f87171' };
  const MOMENTUM_LEGEND = [
    ['green', 'растёт (momentum > +5%)'],
    ['gray', 'стабилен'],
    ['red', 'падает (< -5%)'],
  ];

  function momentumFill(point) {
    return MOMENTUM_COLORS[point.momentum_color] || MOMENTUM_COLORS.gray;
  }

  /* Легенда живёт в DOM под матрицей, а не в SVG: viewBox занят до 400 по
     высоте, и третья строка подписей вылезла бы за край. Элемент один:
     перечитывание экрана (SSE/«Обновить») не должно плодить копии. */
  function renderMomentumLegend() {
    const svg = $('matrix-svg');
    if (!svg || !svg.parentNode) return;
    if ($('momentum-legend')) return;
    const box = document.createElement('div');
    box.id = 'momentum-legend';
    box.className = 'momentum-legend';
    MOMENTUM_LEGEND.forEach(function (item) {
      const span = document.createElement('span');
      const dot = document.createElement('span');
      dot.className = 'dot';
      dot.style.background = MOMENTUM_COLORS[item[0]];
      span.appendChild(dot);
      span.appendChild(document.createTextNode(item[1]));
      box.appendChild(span);
    });
    svg.parentNode.insertBefore(box, svg.nextSibling);
  }

  function renderMatrix(data) {
    const svg = $('matrix-svg');
    svg.innerHTML = '';
    renderMomentumLegend();
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
        fill: momentumFill(point),
        stroke: '#0f1419', 'stroke-width': '1',
        class: 'matrix-point',
        'data-momentum': point.momentum_color || 'gray',
      }, svg);
      circle.dataset.name = point.name;
      circle.setAttribute('tabindex', '0');
      circle.setAttribute('role', 'button');
      const title = svgEl('title', {}, circle);
      title.textContent = point.name + ' — ' + fmtNum(point.adoption_week) +
        '/нед, ' + (point.flag ? 'MCP есть' : 'MCP нет') +
        (point.momentum_30d != null
          ? ', momentum ' + (point.momentum_30d > 0 ? '+' : '') +
            point.momentum_30d.toFixed(1) + '%/30д'
          : ', momentum: мало данных');
      circle.addEventListener('click', () => window.GapAuditCard.open(point.name));
      circle.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          window.GapAuditCard.open(point.name);
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
      btn.addEventListener('click', () => window.GapAuditCard.open(point.name));
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
    /* Здесь — когда экран последний раз читал API (локальное время), а дата
       самого замера живёт отдельной строкой под матрицей: это разные факты,
       и «свежесть окна» не должна выдавать себя за свежесть данных. */
    const now = new Date();
    $('matrix-updated').textContent = 'обновлено в ' +
      now.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  /* «Последний замер: <дата> · Обновить». Берётся самый свежий measured_at из
     точек: у пакетов он разный (задачу могли догонять частями), и подписывать
     экран датой одного пакета нельзя. Нет ни одной даты — строку скрываем,
     а не показываем прочерк: пустая метка выглядит как «данных нет», хотя
     данные могут быть без замеров. */
  function renderLastMeasured(data) {
    const stamps = data.points
      .map((point) => point.measured_at || '')
      .filter((value) => value)
      .sort();
    const box = $('last-measured');
    if (!stamps.length) {
      box.hidden = true;
      return;
    }
    $('last-measured-time').textContent =
      stamps[stamps.length - 1].slice(0, 16).replace('T', ' ');
    box.hidden = false;
  }

  async function load() {
    try {
      current = await api('GET', '/api/matrix');
      renderSummary(current);
      renderMatrix(current);
      renderCandidates(current);
      renderLastMeasured(current);
    } catch (e) {
      $('matrix-svg').innerHTML = '';
      $('matrix-empty').hidden = false;
      $('matrix-empty').textContent = 'Не удалось загрузить матрицу: ' + e.message;
    }
  }

  /* --- карточка пакета ----------------------------------------------------
     Переехала в card.js (шаг 3a): тот же оверлей и те же данные нужны экранам
     «Пакеты» и «Мониторинг». Здесь остаётся только вызов
     window.GapAuditCard.open(name) — разметку, запросы и ошибки держит модуль. */

  /* Выгрузка точек матрицы (шаг 3c): POST /api/matrix/export — CSV в ответе,
     точки те же, что на экране (сервер считает их общим построителем). Ссылку
     создаём сами и отзываем после клика. */
  async function exportCsv() {
    const token = (window.GapAudit && window.GapAudit.token) || '';
    try {
      const resp = await fetch('/api/matrix/export?format=csv', {
        method: 'POST',
        headers: { 'X-GapAudit-Token': token },
      });
      if (!resp.ok) throw new Error(resp.status + ' ' + resp.statusText);
      const blob = await resp.blob();
      const href = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = 'matrix.csv';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () {
        URL.revokeObjectURL(href);
      }, 1000);
    } catch (e) {
      $('matrix-empty').hidden = false;
      $('matrix-empty').textContent = 'экспорт не удался: ' + e.message;
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    /* F5 = «Обновить»: app.js перехватывает клавишу и зовёт эту функцию
       (AGENTS gotcha 39). Экран без своей функции оставил бы F5 браузеру. */
    window.__gapauditRefresh = load;

    $('refresh').addEventListener('click', load);
    $('export-csv').addEventListener('click', exportCsv);
    /* Ссылка «Обновить» рядом с датой замера: hash в href гасим, иначе
       WebView2 прыгнул бы к якорю и потерял позицию прокрутки. */
    $('last-measured-refresh').addEventListener('click', function (ev) {
      ev.preventDefault();
      load();
    });

    /* Оверлей карточки и Escape — в card.js (шаг 3a): модуль один на все
       экраны, и навешивать обработчики здесь значило бы открывать карточку
       двумя путями с разным поведением по Escape. */

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
