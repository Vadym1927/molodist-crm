/* MOLODIST — сайт: заявки й кліки → CRM (з мітками реклами й gclid), події для GA4 / Google Ads через dataLayer. */
(function () {
  var M = window.MLDS || {}, B = M.base || '', K = 'mlds_utm', T0 = Date.now();
  window.dataLayer = window.dataLayer || [];
  function ck(n) { var m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]*)')); return m ? decodeURIComponent(m[1]) : ''; }
  try {                                                   // мітки з першої сторінки візиту — 90 днів
    var q = new URLSearchParams(location.search), m = {}, any = false;
    ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'gclid'].forEach(function (k) { var v = q.get(k); if (v) { m[k] = v; any = true; } });
    if (any) localStorage.setItem(K, JSON.stringify({ m: m, t: Date.now(), ref: document.referrer }));
  } catch (e) {}
  function saved() {
    var s = {}; try { var x = JSON.parse(localStorage.getItem(K) || 'null'); if (x && Date.now() - x.t < 90 * 864e5) s = x.m; } catch (e) {}
    if (!s.gclid) { var g = ck('_gcl_aw').split('.'); if (g.length > 2) s.gclid = g.slice(2).join('.'); }
    var ga = ck('_ga').split('.'); if (ga.length > 3) s.cid = ga[2] + '.' + ga[3];
    return s;
  }
  function post(url, d, cb) {
    fetch(B + url, { method: 'POST', body: JSON.stringify(d), headers: { 'Content-Type': 'application/json' }, keepalive: true })
      .then(function (r) { return r.json(); }).then(function (j) { cb && cb(j); }).catch(function () { cb && cb({ ok: false }); });
  }
  function phone(v) { var d = (v || '').replace(/\D/g, ''); if (d.length === 9) d = '380' + d; if (d.length === 10 && d[0] === '0') d = '38' + d; return d; }
  document.querySelectorAll('form[data-lead]').forEach(function (f) {
    var tel = f.querySelector('[name=phone]');
    tel && tel.addEventListener('focus', function () { if (!tel.value) tel.value = '+380'; });
    f.addEventListener('submit', function (e) {
      e.preventDefault();
      var ok = f.querySelector('.ok'), er = f.querySelector('.err'), bt = f.querySelector('button');
      er.hidden = true;
      var p = phone(tel.value);
      if (p.length !== 12 || p.indexOf('380') !== 0) { er.textContent = 'Перевірте номер телефону: +380 і 9 цифр.'; er.hidden = false; tel.focus(); return; }
      var d = saved(); d.name = (f.name && f.name.value || '').trim(); d.phone = '+' + p; d.interest = M.interest || '';
      d.formname = (M.slug || 'home') + ':' + f.getAttribute('data-lead'); d.page = location.href; d.website = f.website ? f.website.value : '';
      d.doctor = f.getAttribute('data-doctor') || ''; d.t = Math.round((Date.now() - T0) / 1000);
      bt.disabled = true;
      post('/api/lead', d, function (j) {
        bt.disabled = false;
        if (j && j.ok) {
          ok.hidden = false; f.querySelectorAll('label,button').forEach(function (x) { x.style.display = 'none'; });
          window.dataLayer.push({ event: 'generate_lead', form_id: d.formname, lead_interest: d.interest });
        } else { er.textContent = 'Не вдалося надіслати. Зателефонуйте нам або напишіть у Telegram.'; er.hidden = false; }
      });
    });
  });
  document.querySelectorAll('[data-doc]').forEach(function (a) {          // «Записатися до лікаря» → лікар у заявці
    a.addEventListener('click', function () { var f = document.querySelector('#zapys form'); if (f) f.setAttribute('data-doctor', a.getAttribute('data-doc')); });
  });
  var EV = { phone: 'phone_click', telegram: 'click_telegram', viber: 'click_viber', instagram: 'click_instagram', booking: 'click_booking' };
  function kind(a) {
    var h = (a.getAttribute('href') || '').toLowerCase();
    if (h.indexOf('tel:') === 0) return 'phone';
    if (/t\.me\/|telegram\.me|tg:\/\//.test(h)) return 'telegram';
    if (/viber:|viber\.com|vb\.me/.test(h)) return 'viber';
    if (/instagram\.com|ig\.me/.test(h)) return 'instagram';
    if (h === '#zapys') return 'booking';
    return null;
  }
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a') : null; if (!a) return;
    var k = kind(a); if (!k) return;
    window.dataLayer.push({ event: EV[k], link_url: a.href, page_slug: M.slug });
    if (k === 'booking') return;
    var d = saved(); if (!d.gclid && !/google/i.test(d.utm_source || '')) return;   // у CRM — лише кліки з реклами
    d.kind = k; d.href = a.href; d.page = location.href;
    post('/api/click', d);
  }, true);
})();
