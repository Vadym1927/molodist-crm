/* MOLODIST — сторінки з архіву Tilda на нашому сервері: форми Tilda → CRM (/api/lead), мітки реклами й gclid. */
(function () {
  var B = (window.MLDS_TILDA || {}).base || '', K = 'mlds_utm', T0 = Date.now();
  function ck(n) { var m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]*)')); return m ? decodeURIComponent(m[1]) : ''; }
  try {
    var q = new URLSearchParams(location.search), m = {}, any = false;
    ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'gclid'].forEach(function (k) { var v = q.get(k); if (v) { m[k] = v; any = true; } });
    if (any) localStorage.setItem(K, JSON.stringify({ m: m, t: Date.now(), ref: document.referrer }));
  } catch (e) {}
  function marks() {
    var s = {}; try { var x = JSON.parse(localStorage.getItem(K) || 'null'); if (x && Date.now() - x.t < 90 * 864e5) s = x.m; } catch (e) {}
    if (!s.gclid) { var g = ck('_gcl_aw').split('.'); if (g.length > 2) s.gclid = g.slice(2).join('.'); }
    return s;
  }
  function done(f, ok) {
    var box = f.querySelector('.js-successbox');
    var btn = f.querySelector('.t-submit'); if (btn) btn.classList.remove('t-btn_sending');
    if (!ok) { alert('Не вдалося надіслати. Перевірте номер телефону або зателефонуйте нам: +38 (093) 375 64 37'); return; }
    if (box) {
      box.style.display = 'block';
      if (!box.textContent.trim()) box.textContent = 'Дякуємо! Ми зателефонуємо вам найближчим часом.';
      var ib = f.querySelector('.t-form__inputsbox'); if (ib) ib.style.display = 'none';
    } else { alert('Дякуємо! Ми зателефонуємо вам найближчим часом.'); }
    try { if (window.MLDS_ev) window.MLDS_ev('lead', 'tilda'); } catch (e) {}
    window.dataLayer = window.dataLayer || []; window.dataLayer.push({ event: 'generate_lead', form: f.getAttribute('name') || f.id || 'tilda' });
  }
  function send(f) {
    var d = {}, fd = new FormData(f);
    fd.forEach(function (v, k) { if (typeof v === 'string' && k.indexOf('tilda') !== 0 && k !== 'form-spec-comments') d[k] = d[k] ? d[k] + ', ' + v : v; });
    var hp = f.querySelector('[name="form-spec-comments"]'); if (hp && hp.value && hp.value !== 'Its good') d.website = hp.value;   // Tilda ставить «Its good»; бот змінює
    var mk = marks(); for (var k in mk) if (!d[k]) d[k] = mk[k];
    d.page = location.href.split('?')[0]; d.formname = f.getAttribute('data-tilda-formname') || f.getAttribute('name') || 'Tilda';
    d.interest = d.interest || document.title.split('|')[0].split('—')[0].trim();
    d.t = Math.round((Date.now() - T0) / 1000);
    var pk = ['Phone', 'phone', 'tel', 'Телефон'].filter(function (k) { return d[k]; })[0] || '';
    var ph = (d[pk] || '').replace(/\D/g, '');
    if (ph.length < 9) {                                      // маска Tilda з прапорцем: номер у видимому полі tildaspec-phone-part[], «Phone» Tilda заповнює лише своїм обробником
      var part = f.querySelector('[name="tildaspec-phone-part[]"]'); if (part) ph = (part.value || '').replace(/\D/g, '');
    }
    if (ph.length === 9) ph = '380' + ph;
    else if (ph.length === 10 && ph[0] === '0') ph = '38' + ph;
    if (ph.length < 11) { alert('Вкажіть, будь ласка, номер телефону.'); return; }
    if (pk) delete d[pk];
    d.phone = '+' + ph;
    var btn = f.querySelector('.t-submit'); if (btn) btn.classList.add('t-btn_sending');
    fetch(B + '/api/lead', { method: 'POST', body: JSON.stringify(d), headers: { 'Content-Type': 'application/json' } })
      .then(function (r) { return r.json(); }).then(function (j) { done(f, j && j.ok); }).catch(function () { done(f, false); });
  }
  function grab(e) {
    var f = e.target && e.target.closest ? e.target.closest('form.t-form, form.js-form-proccess') : null;
    if (!f) return;
    if (e.type === 'click' && !(e.target.closest('.t-submit, [type=submit]'))) return;
    e.preventDefault(); e.stopImmediatePropagation(); send(f);
  }
  window.addEventListener('submit', grab, true);
  window.addEventListener('click', grab, true);
})();
