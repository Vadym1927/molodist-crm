/* MOLODIST — поведінка на сторінках сайту → /api/ev (без персональних даних): блоки, кліки, форма, банер, заявка, де закрили.
   Підключається на всіх сторінках: нова головна, сторінки болей, сторінки послуг, запис і сторінки з архіву Tilda. */
(function () {
    var d = document, M = window.MLDS || window.MLDS_TILDA || {}, B = M.base || '', T0 = Date.now(), Q = [], last = 'top', maxPct = 0, sent = false, sid;
    try { sid = sessionStorage.getItem('mlds_sid') || (Math.random().toString(36).slice(2) + Date.now().toString(36)); sessionStorage.setItem('mlds_sid', sid); } catch (x) { sid = Math.random().toString(36).slice(2); }
    function ctx() {
      var o = {}; try { var s = JSON.parse(localStorage.getItem('mlds_utm') || 'null'); if (s && s.m) o = s.m; } catch (x) {}
      try { o.ref = sessionStorage.getItem('mlds_ref') || document.referrer || ''; } catch (x) {}
      if (!o.gclid) { var m = document.cookie.match(/(?:^|; )_gcl_aw=([^;]*)/); if (m) { var g = decodeURIComponent(m[1]).split('.'); if (g.length > 2) o.gclid = g.slice(2).join('.'); } }
      return o;
    }
    function flush(beacon) {
      if (!Q.length) return;
      var body = JSON.stringify(Object.assign(ctx(), { sid: sid, page: location.pathname, ev: Q.splice(0, 60) }));
      try { if (beacon && navigator.sendBeacon) { navigator.sendBeacon(B + '/api/ev', new Blob([body], { type: 'text/plain' })); return; } } catch (x) {}
      fetch(B + '/api/ev', { method: 'POST', body: body, headers: { 'Content-Type': 'text/plain' }, keepalive: true }).catch(function () {});
    }
    function ev(k, t, v) { Q.push({ k: k, t: t, v: v }); if (Q.length >= 20) flush(); }
    window.MLDS_ev = ev;
    ev('view', document.title.slice(0, 80));
    var secs = d.querySelectorAll('main > section, main > div > section, main > article > section, #allrecords > .t-rec');
    if ('IntersectionObserver' in window) {
      var seen = {}, io = new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          if (!e.isIntersecting) return;
          var s = e.target, n;
          if (s.classList.contains('t-rec')) {                       // сторінки з архіву Tilda: блок називаємо за заголовком
            if (s.offsetHeight < 80) return;
            var h = s.querySelector('h1,h2,h3,.t-title,.t-heading,[field="title"]');
            n = h ? (h.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40) : '';
            n = n || ('блок ' + (Array.prototype.indexOf.call(secs, s) + 1));
          } else n = s.id || (s.className || '').split(' ').filter(function (c) { return /^(h-|l-)/.test(c); }).map(function (c) { return c.replace(/^(h-|l-)/, ''); })[0] || 'sec';
          if (n === 'hero' || n === 'a') n = 'hero';
          last = n;
          var pct = Math.round(100 * (window.scrollY + window.innerHeight) / d.documentElement.scrollHeight); if (pct > maxPct) maxPct = pct;
          if (!seen[n]) { seen[n] = 1; ev('sec', n, Math.min(100, pct)); }
        });
      }, { threshold: 0.25 });
      secs.forEach(function (s) { io.observe(s); });
    }
    d.addEventListener('click', function (e) {
      var a = e.target.closest ? e.target.closest('a,button') : null; if (!a || a.closest('#h-modal form')) return;
      var t = a.getAttribute('data-interest') || a.getAttribute('aria-label') || (a.textContent || '').replace(/\s+/g, ' ').trim();
      ev('click', (t || a.getAttribute('href') || '?').slice(0, 60) + ' · ' + last);
    }, true);
    var mo = d.getElementById('h-modal');
    if (mo && 'MutationObserver' in window) new MutationObserver(function () { if (!mo.hidden) ev('form', (mo.className || '').replace('h-modal', '').trim() || 'std'); }).observe(mo, { attributes: true, attributeFilter: ['hidden'] });
    var pp = d.getElementById('h-pop');
    if (pp && 'MutationObserver' in window) new MutationObserver(function () { if (!pp.hidden) ev('pop', 'banner'); }).observe(pp, { attributes: true, attributeFilter: ['hidden'] });
    d.addEventListener('submit', function (e) {
      var f = e.target; if (!f.matches || !f.matches('form[data-lead]')) return;
      setTimeout(function () { var ok = f.querySelector('.ok'); if (ok && !ok.hidden) { ev('lead', f.getAttribute('data-lead')); flush(); } }, 2500);
    });
    function bye() { if (sent) return; sent = true; ev('exit', last, Math.round((Date.now() - T0) / 1000)); flush(true); }
    d.addEventListener('visibilitychange', function () { if (d.visibilityState === 'hidden') bye(); else sent = false; });
    window.addEventListener('pagehide', bye);
    setInterval(function () { flush(); }, 15000);
  })();
