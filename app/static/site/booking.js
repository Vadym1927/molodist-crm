/* MOLODIST — онлайн-запис: послуга → спеціаліст → день і час → контакти. Дані — з CRM (прайс, графік, вільні вікна). */
(function () {
  var M = window.MLDS || {}, B = M.base || '', T0 = Date.now();
  var $ = function (s, r) { return (r || document).querySelector(s); }, $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  window.dataLayer = window.dataLayer || [];
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function money(v) { return Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' ₴'; }
  function mins(m) { return m >= 60 ? (Math.floor(m / 60) + ' год' + (m % 60 ? ' ' + (m % 60) + ' хв' : '')) : m + ' хв'; }
  function ck(n) { var m = document.cookie.match(new RegExp('(?:^|; )' + n + '=([^;]*)')); return m ? decodeURIComponent(m[1]) : ''; }
  function marks() {                                        // мітки реклами — ті самі, що у форм сайту (site.js)
    var s = {}; try { var x = JSON.parse(localStorage.getItem('mlds_utm') || 'null'); if (x && Date.now() - x.t < 90 * 864e5) s = x.m || {}; } catch (e) {}
    s = JSON.parse(JSON.stringify(s));
    if (!s.gclid) { var g = ck('_gcl_aw').split('.'); if (g.length > 2) s.gclid = g.slice(2).join('.'); }
    var ga = ck('_ga').split('.'); if (ga.length > 3) s.cid = ga[2] + '.' + ga[3];
    s.page = location.href.split('#')[0]; s.t = Math.round((Date.now() - T0) / 1000);
    return s;
  }
  function api(url, body, cb) {
    var o = body ? { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } } : {};
    fetch(B + url, o).then(function (r) { return r.json().catch(function () { return { ok: false }; }); })
      .then(cb).catch(function () { cb({ ok: false, msg: 'Немає звʼязку. Спробуйте ще раз або зателефонуйте нам.' }); });
  }
  function telMask(tel) {
    if (!tel) return;
    function fmt(v) {
      var d = (v || '').replace(/\D/g, '');
      if (d[0] === '0') d = '38' + d; else if (d && d.indexOf('380') !== 0 && '380'.indexOf(d) !== 0) d = '380' + d;
      d = d.slice(0, 12);
      var r = '+' + d.slice(0, 3);
      if (d.length > 3) r += ' ' + d.slice(3, 5);
      if (d.length > 5) r += ' ' + d.slice(5, 8);
      if (d.length > 8) r += ' ' + d.slice(8, 10);
      if (d.length > 10) r += ' ' + d.slice(10, 12);
      return r;
    }
    tel.setAttribute('maxlength', '17');
    tel.addEventListener('focus', function () { if (!tel.value) tel.value = '+380 '; });
    tel.addEventListener('input', function () { tel.value = fmt(tel.value); });
    tel.addEventListener('blur', function () { if (/^\+?380\s*$/.test(tel.value)) tel.value = ''; });
  }
  function telOk(v) { var d = (v || '').replace(/\D/g, ''); return d.length === 12 && d.indexOf('380') === 0; }

  /* ---------- розділи: підсвітка в навігації ---------- */
  var jump = $$('.ob-jump a');
  if (jump.length && 'IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) jump.forEach(function (a) { a.classList.toggle('on', a.getAttribute('href') === '#' + e.target.id); }); });
    }, { rootMargin: '-40% 0px -55% 0px' });
    jump.forEach(function (a) { var t = $(a.getAttribute('href')); if (t) io.observe(t); });
  }

  /* ---------- мій запис: скасування ---------- */
  var my = $('.ob-mycard[data-token]');
  if (my) {
    var cb = $('[data-ob-cancel]', my);
    if (cb) cb.addEventListener('click', function () {
      if (cb.dataset.sure !== '1') { cb.dataset.sure = '1'; cb.textContent = 'Так, скасувати запис'; return; }
      cb.disabled = true;
      api('/api/booking/cancel', { token: my.dataset.token }, function (j) {
        if (j && j.ok) { location.reload(); return; }
        cb.disabled = false; var er = $('.err', my); er.textContent = (j && j.msg) || 'Не вдалося скасувати. Зателефонуйте нам, будь ласка.'; er.hidden = false;
      });
    });
  }

  /* ---------- сертифікати й абонементи ---------- */
  var dlg = $('#ob-buy');
  if (dlg) {
    var bf = $('#ob-buy-form'), cur = null;
    telMask(bf.phone);
    $$('[data-ob-buy]').forEach(function (b) {
      b.addEventListener('click', function () {
        cur = { kind: b.dataset.obBuy, id: b.dataset.id };
        $('#ob-buy-kind').textContent = cur.kind === 'sub' ? 'Абонемент' : 'Подарунковий сертифікат';
        $('#ob-buy-name').textContent = b.dataset.name; $('#ob-buy-price').textContent = b.dataset.price;
        $('.ok', bf).hidden = true; $('.err', bf).hidden = true;
        $$('label,button.btn', bf).forEach(function (x) { x.style.display = ''; });
        if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
        window.dataLayer.push({ event: 'select_item', item_category: cur.kind, item_name: b.dataset.name });
      });
    });
    $$('[data-ob-close]', dlg).forEach(function (x) { x.addEventListener('click', function () { dlg.close ? dlg.close() : dlg.removeAttribute('open'); }); });
    dlg.addEventListener('click', function (e) { if (e.target === dlg && dlg.close) dlg.close(); });
    bf.addEventListener('submit', function (e) {
      e.preventDefault();
      var er = $('.err', bf), bt = $('button.btn', bf); er.hidden = true;
      if ((bf.name.value || '').trim().length < 2) { er.textContent = 'Вкажіть, будь ласка, імʼя.'; er.hidden = false; bf.name.focus(); return; }
      if (!telOk(bf.phone.value)) { er.textContent = 'Перевірте номер телефону: +380 і 9 цифр.'; er.hidden = false; bf.phone.focus(); return; }
      var d = marks(); d.kind = cur.kind; d.id = cur.id; d.name = bf.name.value.trim(); d.phone = bf.phone.value; d.to = bf.to.value.trim(); d.website = bf.website.value;
      bt.disabled = true;
      api('/api/order', d, function (j) {
        bt.disabled = false;
        if (j && j.ok) {
          window.dataLayer.push({ event: 'generate_lead', form_id: 'zapys:' + cur.kind, lead_interest: j.title, value: j.price, currency: 'UAH' });
          if (j.pay_url) { location.href = j.pay_url; return; }
          $('.ok', bf).hidden = false; $$('label,button.btn', bf).forEach(function (x) { x.style.display = 'none'; });
        } else { er.textContent = (j && j.msg) || 'Не вдалося надіслати. Зателефонуйте нам або напишіть у Telegram.'; er.hidden = false; }
      });
    });
  }

  /* ---------- запис ---------- */
  var root = $('#ob'); if (!root) return;
  var D = JSON.parse($('#ob-data').textContent), SV = {}, DOC = {};
  D.cats.forEach(function (c) { c.items.forEach(function (s) { s.cat = c.name; SV[s.id] = s; }); });
  D.docs.forEach(function (d) { DOC[d.id] = d; });
  var st = { svc: null, doc: null, anyDoc: false, day: null, time: null, onlyDoc: null, cat: '', days: [] };
  var elList = $('#ob-list'), elCats = $('#ob-cats'), elQ = $('#ob-q'), elDocs = $('#ob-docs'), elDays = $('#ob-days'), elSlots = $('#ob-slots'), form = $('#ob-form');
  telMask(form.phone);

  function go(n) {
    if (n > 1 && !st.svc) n = 1;
    if (n > 2 && !(st.doc || st.anyDoc)) n = 2;
    if (n > 3 && !st.time) n = 3;
    root.dataset.step = n;
    $$('.ob-pane', root).forEach(function (p) { p.hidden = +p.dataset.pane !== n; });
    $$('.ob-steps li', root).forEach(function (li) { var k = +li.dataset.go; li.classList.toggle('on', k === n); li.classList.toggle('done', k < n && n < 5); });
    if (n === 5) $$('.ob-steps li', root).forEach(function (li) { li.classList.add('done'); li.classList.remove('on'); });
    summary();
  }
  function price() { if (!st.svc) return null; var p = st.doc && st.svc.dp && st.svc.dp[st.doc.id]; return p || st.svc.price; }
  function summary() {
    $('#s-svc').innerHTML = st.svc ? esc(st.svc.name) + '<small>' + mins(st.svc.min) + '</small>' : '—';
    $('#s-doc').textContent = st.doc ? st.doc.name : (st.anyDoc ? 'Будь-який вільний спеціаліст' : '—');
    var d = st.day && st.days.filter(function (x) { return x.d === st.day; })[0];
    $('#s-when').textContent = d && st.time ? d.label + ', ' + st.time : '—';
    $('#s-price').textContent = st.svc ? money(price()) : '—';
  }
  function visible(s) { return !st.onlyDoc || s.docs.indexOf(st.onlyDoc) >= 0; }
  function renderCats() {
    var cs = D.cats.filter(function (c) { return c.items.some(visible); });
    if (st.cat && !cs.some(function (c) { return c.name === st.cat; })) st.cat = '';
    elCats.innerHTML = '<button type="button" class="' + (st.cat ? '' : 'on') + '" data-cat="">Усі</button>' + cs.map(function (c) {
      return '<button type="button" class="' + (st.cat === c.name ? 'on' : '') + '" data-cat="' + esc(c.name) + '">' + esc(c.name) + '</button>'; }).join('');
  }
  function renderList() {
    var q = (elQ.value || '').trim().toLowerCase(), h = '', n = 0;
    D.cats.forEach(function (c) {
      if (st.cat && !q && c.name !== st.cat) return;
      var its = c.items.filter(function (s) { return visible(s) && (!q || (s.name + ' ' + c.name).toLowerCase().indexOf(q) >= 0); });
      if (!its.length) return;
      n += its.length;
      h += '<div class="ob-cat"><h3>' + esc(c.name) + '</h3>' + its.map(function (s) {
        return '<button type="button" class="ob-svc' + (st.svc && st.svc.id === s.id ? ' on' : '') + '" data-svc="' + s.id + '"><span><b>' + esc(s.name) + '</b>' +
          (s.desc ? '<small>' + esc(s.desc) + '</small>' : '') + '</span><span class="m">' + mins(s.min) + '</span><span class="p">' + money(s.price) + '</span></button>'; }).join('') + '</div>';
    });
    elList.innerHTML = h || '<p class="muted">Нічого не знайдено. Спробуйте іншу назву або зателефонуйте нам — підкажемо.</p>';
  }
  function renderDocs() {
    var ids = st.svc.docs.filter(function (i) { return DOC[i]; }), h = '';
    if (ids.length > 1) h += '<button type="button" class="ob-doc any' + (st.anyDoc ? ' on' : '') + '" data-doc="0"><span class="ob-ph"><i>✦</i></span><span><b>Будь-який вільний спеціаліст</b><small>Більше вільного часу на вибір</small></span></button>';
    h += ids.map(function (i) {
      var d = DOC[i], p = st.svc.dp && st.svc.dp[i];
      return '<button type="button" class="ob-doc' + (st.doc && st.doc.id === i ? ' on' : '') + '" data-doc="' + i + '"><span class="ob-ph">' +
        (d.photo ? '<img src="' + esc(d.photo) + '" alt="">' : '<i>' + esc(d.initials) + '</i>') + '</span><span><b>' + esc(d.name) + '</b><small>' + esc(d.role) + '</small>' +
        (p && p !== st.svc.price ? '<em>' + money(p) + '</em>' : '') + '</span></button>'; }).join('');
    elDocs.innerHTML = h;
  }
  function loadDays() {
    elDays.innerHTML = '<p class="muted">Шукаємо вільний час…</p>'; elSlots.innerHTML = '';
    var key = st.svc.id + ':' + (st.doc ? st.doc.id : 0);
    api('/api/booking/days?service=' + st.svc.id + '&doctor=' + (st.doc ? st.doc.id : 0), null, function (j) {
      if (key !== st.svc.id + ':' + (st.doc ? st.doc.id : 0)) return;
      st.days = (j && j.days) || [];
      if (!st.days.length) {
        elDays.innerHTML = '<p class="ob-none">На найближчі тижні вільного часу онлайн немає. Напишіть або зателефонуйте нам — адміністратор підбере варіант.</p>';
        return;
      }
      if (!st.day || !st.days.some(function (x) { return x.d === st.day; })) { st.day = st.days[0].d; st.time = null; }
      renderDays(); renderSlots();
    });
  }
  function renderDays() {
    elDays.innerHTML = st.days.map(function (x) {
      return '<button type="button" class="' + (x.d === st.day ? 'on' : '') + '" data-day="' + x.d + '"><small>' + x.wd + '</small><b>' + x.n + '</b><small>' + x.m.slice(0, 3) + '</small></button>'; }).join('');
    var on = $('.on', elDays); if (on && on.scrollIntoView) on.scrollIntoView({ block: 'nearest', inline: 'center' });
  }
  function renderSlots() {
    var d = st.days.filter(function (x) { return x.d === st.day; })[0]; if (!d) { elSlots.innerHTML = ''; return; }
    var g = [['Ранок', []], ['День', []], ['Вечір', []]];
    d.slots.forEach(function (s) { var h = +s.t.slice(0, 2); g[h < 12 ? 0 : h < 17 ? 1 : 2][1].push(s); });
    elSlots.innerHTML = '<p class="ob-hint">' + esc(d.label.charAt(0).toUpperCase() + d.label.slice(1)) + '</p>' + g.filter(function (x) { return x[1].length; }).map(function (x) {
      return '<div class="ob-sg"><span>' + x[0] + '</span><div>' + x[1].map(function (s) {
        return '<button type="button" class="' + (st.time === s.t ? 'on' : '') + '" data-time="' + s.t + '">' + s.t + '</button>'; }).join('') + '</div></div>'; }).join('');
  }
  function pickDoc(id) {
    st.onlyDoc = id || null;
    var box = $('#ob-docsel');
    if (st.onlyDoc && DOC[id]) { box.hidden = false; $('span', box).textContent = 'Послуги, які проводить ' + DOC[id].name; } else box.hidden = true;
    if (st.svc && !visible(st.svc)) { st.svc = null; st.doc = null; st.anyDoc = false; st.time = null; }
    renderCats(); renderList();
  }

  elCats.addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; st.cat = b.dataset.cat; elQ.value = ''; renderCats(); renderList(); });
  elQ.addEventListener('input', renderList);
  elList.addEventListener('click', function (e) {
    var b = e.target.closest('[data-svc]'); if (!b) return;
    st.svc = SV[b.dataset.svc]; st.time = null; st.day = null; st.days = [];
    window.dataLayer.push({ event: 'booking_service', item_name: st.svc.name, item_category: st.svc.cat, value: st.svc.price, currency: 'UAH' });
    if (st.onlyDoc && st.svc.docs.indexOf(st.onlyDoc) >= 0) { st.doc = DOC[st.onlyDoc]; st.anyDoc = false; go(3); loadDays(); }
    else if (st.svc.docs.length === 1) { st.doc = DOC[st.svc.docs[0]]; st.anyDoc = false; go(3); loadDays(); }
    else { st.doc = null; st.anyDoc = false; renderDocs(); go(2); }
    renderList(); root.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  elDocs.addEventListener('click', function (e) {
    var b = e.target.closest('[data-doc]'); if (!b) return;
    var id = +b.dataset.doc; st.doc = id ? DOC[id] : null; st.anyDoc = !id; st.time = null; st.day = null;
    go(3); loadDays();
  });
  elDays.addEventListener('click', function (e) { var b = e.target.closest('[data-day]'); if (!b) return; st.day = b.dataset.day; st.time = null; renderDays(); renderSlots(); summary(); });
  elSlots.addEventListener('click', function (e) {
    var b = e.target.closest('[data-time]'); if (!b) return; st.time = b.dataset.time; go(4);
    window.dataLayer.push({ event: 'booking_time', item_name: st.svc.name });
    setTimeout(function () { try { form.name.focus({ preventScroll: true }); } catch (x) {} }, 50);
  });
  root.addEventListener('click', function (e) {
    var b = e.target.closest('[data-go]'); if (!b || root.dataset.step === '5') return;
    var n = +b.dataset.go; if (n === 2 && st.svc) renderDocs(); if (n === 3 && st.svc && (st.doc || st.anyDoc) && !st.days.length) loadDays();
    if (b.tagName === 'LI' && n > +root.dataset.step) return;
    go(n);
  });
  $('[data-ob-nodoc]').addEventListener('click', function () { pickDoc(null); });
  $$('[data-ob-doc]').forEach(function (a) { a.addEventListener('click', function () { if (root.dataset.step === '5') return; pickDoc(+a.dataset.obDoc); st.doc = null; st.anyDoc = false; go(1); }); });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var er = $('.err', form), bt = $('button.btn', form); er.hidden = true;
    if ((form.name.value || '').trim().length < 2) { er.textContent = 'Вкажіть, будь ласка, імʼя.'; er.hidden = false; form.name.focus(); return; }
    if (!telOk(form.phone.value)) { er.textContent = 'Перевірте номер телефону: +380 і 9 цифр.'; er.hidden = false; form.phone.focus(); return; }
    var d = marks(); d.service = st.svc.id; d.doctor = st.doc ? st.doc.id : 0; d.day = st.day; d.time = st.time;
    d.name = form.name.value.trim(); d.phone = form.phone.value; d.comment = form.comment.value.trim(); d.website = form.website.value;
    bt.disabled = true; bt.textContent = 'Записуємо…';
    api('/api/booking', d, function (j) {
      bt.disabled = false; bt.textContent = 'Записатися';
      if (j && j.ok) {
        $('#ob-done-when').textContent = j.when.charAt(0).toUpperCase() + j.when.slice(1);
        $('#ob-done-what').textContent = j.service + (j.doctor ? ' · ' + j.doctor : '') + (j.price ? ' · ' + money(j.price) : '');
        $('#ob-ics').href = B + '/zapys.ics?v=' + encodeURIComponent(j.token);
        $('#ob-mylink').href = B + '/zapys?v=' + encodeURIComponent(j.token);
        $('#s-doc').textContent = j.doctor || $('#s-doc').textContent;
        try { localStorage.setItem('mlds_booking', JSON.stringify({ v: j.token, at: Date.now() })); } catch (x) {}
        window.dataLayer.push({ event: 'booking_complete', form_id: 'zapys:booking', lead_interest: j.service, value: j.price || 0, currency: 'UAH' });
        window.dataLayer.push({ event: 'generate_lead', form_id: 'zapys:booking', lead_interest: j.service });
        go(5); root.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else {
        er.textContent = (j && j.msg) || 'Не вдалося записати. Зателефонуйте нам або напишіть у Telegram.'; er.hidden = false;
        if (j && j.error === 'taken') { st.time = null; go(3); loadDays(); }
      }
    });
  });

  renderCats(); renderList(); summary();
  var pre = D.pre || {};
  if (pre.doctor && DOC[pre.doctor]) pickDoc(pre.doctor);
  if (pre.service && SV[pre.service]) { var b0 = $('[data-svc="' + pre.service + '"]', elList); if (b0) b0.click(); }
})();
