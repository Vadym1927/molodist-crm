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

  /* ---------- запис: одна або кілька послуг за один візит ---------- */
  var root = $('#ob'); if (!root) return;
  var D = JSON.parse($('#ob-data').textContent), SV = {}, DOC = {}, MAX = 5;
  D.cats.forEach(function (c) { c.items.forEach(function (s) { s.cat = c.name; SV[s.id] = s; }); });
  D.docs.forEach(function (d) { DOC[d.id] = d; });
  var st = { svcs: [], doc: null, anyDoc: false, day: null, time: null, onlyDoc: null, cat: '', days: [] };
  var elList = $('#ob-list'), elCats = $('#ob-cats'), elQ = $('#ob-q'), elDocs = $('#ob-docs'), elDays = $('#ob-days'), elSlots = $('#ob-slots'), form = $('#ob-form'),
    elNext = $('#ob-next');
  telMask(form.phone);

  function has(id) { return st.svcs.some(function (s) { return s.id === id; }); }
  function common() {                                        // спеціалісти, які надають усі обрані послуги
    var ids = null;
    st.svcs.forEach(function (s) { ids = ids === null ? s.docs.slice() : ids.filter(function (i) { return s.docs.indexOf(i) >= 0; }); });
    ids = (ids || []).filter(function (i) { return DOC[i]; });
    return st.onlyDoc ? ids.filter(function (i) { return i === st.onlyDoc; }) : ids;
  }
  function fits(s) {                                         // послугу можна додати до вже обраних (є спільний спеціаліст)
    if (!st.svcs.length || has(s.id)) return true;
    var c = common(); return s.docs.some(function (i) { return c.indexOf(i) >= 0; });
  }
  function totMin() { return st.svcs.reduce(function (a, s) { return a + s.min; }, 0); }
  function priceOf(s) { var p = st.doc && s.dp && s.dp[st.doc.id]; return p || s.price; }
  function totPrice() { return st.svcs.reduce(function (a, s) { return a + priceOf(s); }, 0); }
  function ids() { return st.svcs.map(function (s) { return s.id; }).join(','); }
  function names() { return st.svcs.map(function (s) { return s.name; }).join(', '); }

  function go(n) {
    if (n > 1 && !st.svcs.length) n = 1;
    if (n > 2 && !(st.doc || st.anyDoc)) n = 2;
    if (n > 3 && !st.time) n = 3;
    root.dataset.step = n;
    $$('.ob-pane', root).forEach(function (p) { p.hidden = +p.dataset.pane !== n; });
    $$('.ob-steps li', root).forEach(function (li) { var k = +li.dataset.go; li.classList.toggle('on', k === n); li.classList.toggle('done', k < n && n < 5); });
    if (n === 5) $$('.ob-steps li', root).forEach(function (li) { li.classList.add('done'); li.classList.remove('on'); });
    summary();
  }
  function summary() {
    $('#s-svc').innerHTML = st.svcs.length ? st.svcs.map(function (s) { return '<span>' + esc(s.name) + '</span>'; }).join('') + '<small>' + mins(totMin()) + '</small>' : '—';
    $('#s-doc').textContent = st.doc ? st.doc.name : (st.anyDoc ? 'Будь-який вільний спеціаліст' : '—');
    var d = st.day && st.days.filter(function (x) { return x.d === st.day; })[0];
    $('#s-when').textContent = d && st.time ? d.label + ', ' + st.time : '—';
    $('#s-price').textContent = st.svcs.length ? money(totPrice()) : '—';
    if (elNext) {
      elNext.hidden = !st.svcs.length;
      $('span', elNext).innerHTML = '<b>' + (st.svcs.length === 1 ? 'Обрано 1 послугу' : 'Обрано послуг: ' + st.svcs.length) + '</b> · ' + mins(totMin()) + ' · ' + money(totPrice());
    }
  }
  function visible(s) { return !st.onlyDoc || s.docs.indexOf(st.onlyDoc) >= 0; }
  var ICO = {                                                // лінійні іконки категорій (за ключовими словами назви)
    laser: '<path d="M4 20l9-9"/><path d="M13 11l3-3 3 3-3 3z"/><path d="M19 3v2M22 6h-2M16 3.5l1 1.5M21.5 10l-1.5-1"/>',
    syringe: '<path d="M18 2l4 4"/><path d="M17 7l3-3"/><path d="M19 9L8.7 19.3a2.4 2.4 0 0 1-3.4 0l-.6-.6a2.4 2.4 0 0 1 0-3.4L15 5"/><path d="M9 11l4 4"/><path d="M5 19l-3 3"/><path d="M14 4l6 6"/>',
    hands: '<path d="M7 11V5.5a1.5 1.5 0 0 1 3 0V10"/><path d="M10 10V4.5a1.5 1.5 0 0 1 3 0V10"/><path d="M13 10V5.5a1.5 1.5 0 0 1 3 0V11"/><path d="M16 11V8.5a1.5 1.5 0 0 1 3 0V14a7 7 0 0 1-7 7h-1a6 6 0 0 1-5-2.6L3.3 14a1.6 1.6 0 0 1 2.5-2L7 13"/>',
    stetho: '<path d="M5 3v5a5 5 0 0 0 10 0V3"/><path d="M5 3h2M13 3h2"/><path d="M10 13v2a5 5 0 0 0 10 0v-1"/><circle cx="20" cy="12" r="2"/>',
    target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>',
    drop: '<path d="M12 3s6 6.2 6 10.5a6 6 0 0 1-12 0C6 9.2 12 3 12 3z"/><path d="M9.5 14a2.5 2.5 0 0 0 2.5 2.5"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/>',
    wave: '<path d="M2 12c2.5-6 5-6 7.5 0s5 6 7.5 0 3.5-4 5-2"/><path d="M2 18c2.5-4 5-4 7.5 0s5 4 7.5 0"/>',
    leaf: '<path d="M5 19c0-9 5-14 15-14 0 10-5 15-14 15"/><path d="M5 19c3-5 6-8 10-10"/>',
    lips: '<path d="M2 12c3-4 6-5.5 8-4.5 1 .5 3 .5 4 0 2-1 5 .5 8 4.5-3 4-6.5 6-10 6s-7-2-10-6z"/><path d="M2 12c4 1.200 16 1.200 20 0"/>',
    flask: '<path d="M9 3h6"/><path d="M10 3v6l-5 9a2 2 0 0 0 1.800 3h10.400a2 2 0 0 0 1.800-3l-5-9V3"/><path d="M7.500 15h9"/>',
    lift: '<path d="M4 15l8-8 8 8"/><path d="M4 20l8-8 8 8"/>',
    gift: '<rect x="3" y="8" width="18" height="4" rx="1"/><path d="M5 12v8h14v-8M12 8v12"/><path d="M12 8c-2.500 0-4-1.200-4-2.800S9.500 3 12 8zM12 8c2.500 0 4-1.200 4-2.800S14.500 3 12 8z"/>',
    plus: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    spark: '<path d="M12 3l1.900 5.800L20 11l-6.100 2.200L12 19l-1.900-5.800L4 11l6.100-2.200z"/><path d="M19 3v4M17 5h4"/>'
  };
  var IMAP = [[/лазерн|епіляц/i, 'laser'], [/ipl|судин|пігмент/i, 'sun'], [/ботул|ботокс|диспорт|нейронокс|xeomin/i, 'syringe'], [/контурн|губ/i, 'lips'],
    [/біоревіт|мезо|інʼєкц|ін'єкц|ін’єкц/i, 'syringe'], [/масаж|reset|стратосфер|stratosphere/i, 'hands'], [/консультац|повторний огляд|корекц/i, 'stetho'],
    [/видален|новоутвор|дерматохір/i, 'target'], [/пілінг|очищен|чистк/i, 'drop'], [/rf|ліфтинг|endymed/i, 'lift'], [/догляд/i, 'leaf'],
    [/лаборат|лабарат|аналіз/i, 'flask'], [/сертиф|подарун/i, 'gift'], [/додатков/i, 'plus'], [/апарат/i, 'wave']];
  function ico(name) {
    var k = 'spark'; for (var i = 0; i < IMAP.length; i++) if (IMAP[i][0].test(name)) { k = IMAP[i][1]; break; }
    return '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.600" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICO[k] + '</svg>';
  }
  function catTitle(n) { var m = /^(.*?)\s*\((.*)\)\s*$/.exec(n); return m ? [m[1].trim(), m[2].trim()] : [n, '']; }
  function plural(n) { var a = n % 10, b = n % 100; return n + (a === 1 && b !== 11 ? ' послуга' : a >= 2 && a <= 4 && (b < 12 || b > 14) ? ' послуги' : ' послуг'); }
  function renderCats() {                                    // категорії — плитками з іконками; обрана категорія — список послуг
    var cs = D.cats.filter(function (c) { return c.items.some(visible); });
    if (st.cat && !cs.some(function (c) { return c.name === st.cat; })) st.cat = '';
    var q = (elQ.value || '').trim();
    if (cs.length < 2) { elCats.innerHTML = ''; elCats.className = 'ob-cats'; if (cs.length === 1 && !st.cat) st.cat = cs[0].name; return; }
    if (st.cat || q) {
      elCats.className = 'ob-cats back';
      elCats.innerHTML = '<button type="button" class="ob-allcats" data-cat="">← Усі категорії</button>' + (st.cat && !q ? '<span>' + ico(st.cat) + esc(catTitle(st.cat)[0]) + '</span>' : '');
      return;
    }
    elCats.className = 'ob-cats tiles';
    elCats.innerHTML = cs.map(function (c) {
      var t = catTitle(c.name), n = c.items.filter(visible).length, sel = c.items.filter(function (s) { return has(s.id); }).length;
      return '<button type="button" class="ob-tile' + (sel ? ' has' : '') + '" data-cat="' + esc(c.name) + '"><i>' + ico(c.name) + '</i><b>' + esc(t[0]) + '</b>' +
        (t[1] ? '<small class="note">' + esc(t[1]) + '</small>' : '') + '<small>' + plural(n) + '</small>' + (sel ? '<em>' + sel + '</em>' : '') + '</button>'; }).join('');
  }
  function renderList() {
    var q = (elQ.value || '').trim().toLowerCase(), h = '', full = st.svcs.length >= MAX, multi = D.cats.filter(function (c) { return c.items.some(visible); }).length > 1;
    if (multi && !st.cat && !q) { elList.innerHTML = ''; elList.hidden = true; return; }
    elList.hidden = false;
    D.cats.forEach(function (c) {
      if (st.cat && !q && c.name !== st.cat) return;
      var its = c.items.filter(function (s) { return visible(s) && (!q || (s.name + ' ' + c.name).toLowerCase().indexOf(q) >= 0); });
      if (!its.length) return;
      h += '<div class="ob-cat">' + (q || !multi ? '<h3>' + esc(catTitle(c.name)[0]) + '</h3>' : '') + its.map(function (s) {
        var on = has(s.id), off = !on && (full || !fits(s));
        return '<button type="button" class="ob-svc' + (on ? ' on' : '') + (off ? ' off' : '') + '" data-svc="' + s.id + '" aria-pressed="' + on + '"' + (off ? ' aria-disabled="true"' : '') + '>' +
          '<i class="ck" aria-hidden="true"></i><span><b>' + esc(s.name) + '</b>' + (s.desc ? '<small>' + esc(s.desc) + '</small>' : '') +
          (off && !full ? '<small class="why">Проводить інший спеціаліст — запишіться на цю послугу окремо</small>' : '') +
          '</span><span class="m">' + mins(s.min) + '</span><span class="p">' + money(s.price) + '</span></button>'; }).join('') + '</div>';
    });
    elList.innerHTML = h || '<p class="muted">Нічого не знайдено. Спробуйте іншу назву або зателефонуйте нам — підкажемо.</p>';
  }
  function renderDocs() {
    var c = common(), h = '';
    if (c.length > 1) h += '<button type="button" class="ob-doc any' + (st.anyDoc ? ' on' : '') + '" data-doc="0"><span class="ob-ph"><i>✦</i></span><span><b>Будь-який вільний спеціаліст</b><small>Більше вільного часу на вибір</small></span></button>';
    h += c.map(function (i) {
      var d = DOC[i], p = st.svcs.reduce(function (a, s) { return a + ((s.dp && s.dp[i]) || s.price); }, 0), base = st.svcs.reduce(function (a, s) { return a + s.price; }, 0);
      return '<button type="button" class="ob-doc' + (st.doc && st.doc.id === i ? ' on' : '') + '" data-doc="' + i + '"><span class="ob-ph">' +
        (d.photo ? '<img src="' + esc(d.photo) + '" alt="">' : '<i>' + esc(d.initials) + '</i>') + '</span><span><b>' + esc(d.name) + '</b><small>' + esc(d.role) + '</small>' +
        (p !== base ? '<em>' + money(p) + '</em>' : '') + '</span></button>'; }).join('');
    elDocs.innerHTML = h;
  }
  function key() { return ids() + ':' + (st.doc ? st.doc.id : 0); }
  function loadDays() {
    elDays.innerHTML = '<p class="muted">Шукаємо вільний час…</p>'; elSlots.innerHTML = '';
    var k = key();
    api('/api/booking/days?service=' + ids() + '&doctor=' + (st.doc ? st.doc.id : 0), null, function (j) {
      if (k !== key()) return;
      st.days = (j && j.days) || [];
      if (!st.days.length) {
        elDays.innerHTML = '<p class="ob-none">На найближчі тижні вільного часу онлайн немає' + (st.svcs.length > 1 ? ' для всіх обраних послуг поспіль (' + mins(totMin()) + '). Спробуйте обрати менше послуг або напишіть нам' : '. Напишіть або зателефонуйте нам') + ' — адміністратор підбере варіант.</p>';
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
  function resetTime() { st.time = null; st.day = null; st.days = []; }
  function pickDoc(id) {
    st.onlyDoc = id || null;
    var box = $('#ob-docsel');
    if (st.onlyDoc && DOC[id]) { box.hidden = false; $('span', box).textContent = 'Послуги, які проводить ' + DOC[id].name; } else box.hidden = true;
    st.svcs = st.svcs.filter(visible); st.doc = null; st.anyDoc = false; resetTime();
    renderCats(); renderList(); summary();
  }
  function next() {                                          // від послуг — до спеціаліста або одразу до часу
    if (!st.svcs.length) return;
    var c = common(); resetTime();
    if (c.length === 1) { st.doc = DOC[c[0]]; st.anyDoc = false; go(3); loadDays(); }
    else { if (st.doc && c.indexOf(st.doc.id) < 0) st.doc = null; renderDocs(); go(2); }
    root.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  elCats.addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; st.cat = b.dataset.cat; elQ.value = ''; renderCats(); renderList();
    if (st.cat) root.scrollIntoView({ behavior: 'smooth', block: 'start' }); });
  elQ.addEventListener('input', function () { renderCats(); renderList(); });
  elList.addEventListener('click', function (e) {
    var b = e.target.closest('[data-svc]'); if (!b || b.classList.contains('off')) return;
    var s = SV[b.dataset.svc];
    if (has(s.id)) st.svcs = st.svcs.filter(function (x) { return x.id !== s.id; });
    else { st.svcs.push(s); window.dataLayer.push({ event: 'booking_service', item_name: s.name, item_category: s.cat, value: s.price, currency: 'UAH' }); }
    st.doc = null; st.anyDoc = false; resetTime();
    renderList(); summary();
  });
  if (elNext) $('button', elNext).addEventListener('click', next);
  elDocs.addEventListener('click', function (e) {
    var b = e.target.closest('[data-doc]'); if (!b) return;
    var id = +b.dataset.doc; st.doc = id ? DOC[id] : null; st.anyDoc = !id; resetTime();
    go(3); loadDays();
  });
  elDays.addEventListener('click', function (e) { var b = e.target.closest('[data-day]'); if (!b) return; st.day = b.dataset.day; st.time = null; renderDays(); renderSlots(); summary(); });
  elSlots.addEventListener('click', function (e) {
    var b = e.target.closest('[data-time]'); if (!b) return; st.time = b.dataset.time; go(4);
    window.dataLayer.push({ event: 'booking_time', item_name: names() });
    setTimeout(function () { try { form.name.focus({ preventScroll: true }); } catch (x) {} }, 50);
  });
  root.addEventListener('click', function (e) {
    var b = e.target.closest('[data-go]'); if (!b || root.dataset.step === '5') return;
    var n = +b.dataset.go;
    if (b.tagName === 'LI' && n > +root.dataset.step) return;
    if (n === 2 && st.svcs.length) { if (common().length < 2) n = 1; else renderDocs(); }
    if (n === 3 && st.svcs.length && (st.doc || st.anyDoc) && !st.days.length) loadDays();
    go(n);
  });
  $('[data-ob-nodoc]').addEventListener('click', function () { pickDoc(null); });
  $$('[data-ob-doc]').forEach(function (a) { a.addEventListener('click', function () { if (root.dataset.step === '5') return; pickDoc(+a.dataset.obDoc); go(1); }); });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var er = $('.err', form), bt = $('button.btn', form); er.hidden = true;
    if ((form.name.value || '').trim().length < 2) { er.textContent = 'Вкажіть, будь ласка, імʼя.'; er.hidden = false; form.name.focus(); return; }
    if (!telOk(form.phone.value)) { er.textContent = 'Перевірте номер телефону: +380 і 9 цифр.'; er.hidden = false; form.phone.focus(); return; }
    var d = marks(); d.services = ids(); d.doctor = st.doc ? st.doc.id : 0; d.day = st.day; d.time = st.time;
    d.name = form.name.value.trim(); d.phone = form.phone.value; d.comment = form.comment.value.trim(); d.website = form.website.value;
    bt.disabled = true; bt.textContent = 'Записуємо…';
    api('/api/booking', d, function (j) {
      bt.disabled = false; bt.textContent = 'Записатися';
      if (j && j.ok) {
        $('#ob-done-when').textContent = j.when.charAt(0).toUpperCase() + j.when.slice(1) + '–' + j.end;
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

  $$('[data-ob-svc]').forEach(function (a) { a.addEventListener('click', function () {      // з прайсу — одразу до запису на цю послугу
    var s = SV[a.dataset.obSvc]; if (!s || root.dataset.step === '5') return;
    if (st.onlyDoc && !visible(s)) pickDoc(null);
    if (!has(s.id) && fits(s) && st.svcs.length < MAX) st.svcs.push(s); else if (!has(s.id)) st.svcs = [s];
    st.cat = s.cat; st.doc = null; st.anyDoc = false; resetTime(); elQ.value = ''; go(1); renderCats(); renderList(); summary();
  }); });
  renderCats(); renderList(); summary();
  var pre = D.pre || {};
  if (pre.doctor && DOC[pre.doctor]) pickDoc(pre.doctor);
  if (pre.service && SV[pre.service] && visible(SV[pre.service])) { st.svcs = [SV[pre.service]]; renderList(); summary(); }
})();
