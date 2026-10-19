/* GapAudit — общий канал событий (шаг 2b).
   Одна точка подключения SSE для всех экранов: экранный скрипт не создаёт
   EventSource сам, а подписывается через window.GapAuditSSE.subscribe(fn).

   Токен берётся из window.GapAudit.token (его выставляет app.js, поэтому
   sse.js подключается в base.html после app.js): EventSource не умеет
   ставить заголовки, и для потока токен идёт в query — это единственное
   исключение из правила «токен только заголовком» (матрица токена в
   app/web.py).

   Переподключение: EventSource переподключается сам, пока канал не закрыт
   явно. Обработчик error поэтому смотрит на readyState: CLOSED — это наше
   же закрытие (или отказ сервера), тогда одна повторная попытка через
   RETRY_MS; иначе (CONNECTING) браузер уже переподключается, и второй
   поток событий не нужен. */
(function () {
  const RETRY_MS = 3000;
  const EVENTS = [
    'created', 'started', 'progress', 'done', 'failed', 'cancelled',
  ];

  let es = null;
  let retryTimer = null;
  const handlers = [];

  function emit(event, data) {
    handlers.slice().forEach(function (handler) {
      try {
        handler(event, data);
      } catch (e) {
        /* ошибка подписчика не должна ломать остальные экраны */
      }
    });
  }

  function connect() {
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    if (es) es.close();
    const token = (window.GapAudit && window.GapAudit.token) || '';
    if (!token) return;   /* без токена поток всё равно получит 403 */
    es = new EventSource('/api/stream?token=' + encodeURIComponent(token));
    EVENTS.forEach(function (name) {
      es.addEventListener(name, function (msg) {
        let data = {};
        try { data = JSON.parse(msg.data); } catch (e) { /* не JSON */ }
        emit(name, data);
      });
    });
    es.addEventListener('error', function () {
      if (!es || es.readyState !== EventSource.CLOSED) return;
      /* закрыто нами или сервером: одна повторная попытка, без шторма */
      if (retryTimer) return;
      retryTimer = setTimeout(function () {
        retryTimer = null;
        connect();
      }, RETRY_MS);
    });
  }

  window.GapAuditSSE = {
    /* Подписка до старта потока: очередь обработчиков общая, порядок — как
       подписались. Возвращает функцию отписки (экран может снять обработчик). */
    subscribe: function (handler) {
      handlers.push(handler);
      return function () {
        const index = handlers.indexOf(handler);
        if (index >= 0) handlers.splice(index, 1);
      };
    },
    start: connect,
    stop: function () {
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      if (es) { es.close(); es = null; }
    },
  };

  document.addEventListener('DOMContentLoaded', connect);
})();
