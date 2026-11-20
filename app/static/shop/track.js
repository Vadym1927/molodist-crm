/* MOLODIST магазин — поведінка відвідувачів → /api/ev (без персональних даних): яка сторінка, які блоки бачили, кліки,
   «в кошик», початок оформлення, замовлення, де закрили вкладку. Щоденний звіт — у Telegram власнику. */
(function () {
  var d = document, T0 = Date.now(), Q = [], last = 'top', sent = false, sid, pt = 'other';
  function B() { return (window.MSHOP || {}).base || ''; }
  try { sid = sessionStorage.getItem('mshop_sid') || (Math.random().toString(36).slice(2) + Date.now().toString(36)); sessionStorage.setItem('mshop_sid', sid); } catch (x) { sid = Math.random().toString(36).slice(2); }
  try { if (!sessionStorage.getItem('mlds_ref')) sessionStorage.setItem('mlds_ref', d.referrer || ''); } catch (x) {}
  function ctx() {
    var o = {}; try { var s = JSON.parse(localStorage.getItem('mlds_utm') || 'null'); if (s && s.m) o = s.m; } catch (x) {}
    try { o.ref = sessionStorage.getItem('mlds_ref') || d.referrer || ''; } catch (x) {}
    var g = location.search.match(/[?&](gclid|gbraid|wbraid)=([^&]+)/); if (g && !o.gclid) o.gclid = decodeURIComponent(g[2]);
    if (!o.gclid) { var m = d.cookie.match(/(?:^|; )_gcl_aw=([^;]*)/); if (m) { var p = decodeURIComponent(m[1]).split('.'); if (p.length > 2) o.gclid = p.slice(2).join('.'); } }
    return o;
  }
  function flush(beacon) {
    if (!Q.length) return;
    var body = JSON.stringify(Object.assign(ctx(), { sid: sid, page: location.pathname, ev: Q.splice(0, 60) }));
    try { if (beacon && navigator.sendBeacon) { navigator.sendBeacon(B() + '/api/ev', new Blob([body], { type: 'text/plain' })); return; } } catch (x) {}
    fetch(B() + '/api/ev', { method: 'POST', body: body, headers: { 'Content-Type': 'text/plain' }, keepalive: true }).catch(function () {});
  }
  function ev(k, t, v) { Q.push({ k: k, t: t, v: v }); if (Q.length >= 20) flush(); }
  window.MSHOP_ev = ev;
  function init() {
  var path = location.pathname.replace(/^\/_shop/, '');
  pt = (d.getElementById('p-add') || d.querySelector('.prod')) ? 'product' : (d.querySelector('.lst') ? 'list' : (/^\/checkout\/done/.test(path) ? 'done' : (/^\/checkout/.test(path) ? 'checkout' : (path === '/' ? 'home' : 'other'))));
  ev('view', pt);
  // блоки сторінки: товар (фото, ціна/кнопка, порада лікаря, опис, відгуки, «з цим купують»), список, кошик/оформлення
  var map = [['.gal', 'gal'], ['.info', 'info'], ['.tip', 'tip'], ['.desc', 'desc'], ['#reviews', 'reviews'], ['.rowb, .scroller', 'rel'],
             ['.flt', 'flt'], ['.lst .grid', 'lst'], ['.hero-def, .hero', 'hero'], ['.co', 'co'], ['.cab, .cab-login', 'cab']];
  if ('IntersectionObserver' in window) {
    var seen = {}, io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (!e.isIntersecting) return;
        var n = e.target.getAttribute('data-trk-n'); last = n;
        var pct = Math.round(100 * (window.scrollY + window.innerHeight) / d.documentElement.scrollHeight);
        if (!seen[n]) { seen[n] = 1; ev('sec', n, Math.min(100, pct)); }
      });
    }, { threshold: 0.2 });
    map.forEach(function (m) { var el = d.querySelector(m[0]); if (el) { el.setAttribute('data-trk-n', m[1]); io.observe(el); } });
  }
  d.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a,button') : null; if (!a) return;
    if (a.hasAttribute('data-add') || a.id === 'p-add') return;           // «в кошик» рахується окремо (подія add_to_cart)
    var t = a.getAttribute('aria-label') || (a.textContent || '').replace(/\s+/g, ' ').trim();
    ev('click', (t || a.getAttribute('href') || '?').slice(0, 60) + ' · ' + last);
  }, true);
  }
  if (d.readyState === 'loading') d.addEventListener('DOMContentLoaded', init); else init();
  // події магазину (ті самі, що йдуть у GA4): кошик, оформлення, замовлення
  var orig = window.mshopTrack;
  window.mshopTrack = function (name, ec) {
    try {
      var it = ec && ec.items && ec.items[0];
      if (name === 'add_to_cart') ev('click', ('🛒 ' + (it && it.item_name || 'товар')).slice(0, 60));
      else if (name === 'begin_checkout') ev('form', 'checkout');
      else if (name === 'purchase') { ev('lead', 'order', Math.round(ec && ec.value || 0)); flush(); }
    } catch (x) {}
    if (orig) return orig.apply(this, arguments);
  };
  function bye() { if (sent) return; sent = true; ev('exit', last, Math.round((Date.now() - T0) / 1000)); flush(true); }
  d.addEventListener('visibilitychange', function () { if (d.visibilityState === 'hidden') bye(); else sent = false; });
  window.addEventListener('pagehide', bye);
  setInterval(function () { flush(); }, 15000);
})();
