/* Магазин MOLODIST: кошик (localStorage), бічна панель, пошук, товар, оформлення замовлення */
(function () {
  function ga(ev, ec) { if (window.mshopTrack) window.mshopTrack(ev, ec); }
  window.MSHOP_GA = ga;
  var S = window.MSHOP || {}, B = S.base || '', KEY = 'mshop_cart';
  /* джерело візиту: UTM / gclid / fbclid / зовнішній реферер → у замовлення й заявки (останній непрямий перехід, 90 днів) */
  (function () { try {
    var q = new URLSearchParams(location.search), m = {}, keys = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'gclid', 'fbclid', 'gbraid', 'wbraid'];
    keys.forEach(function (k) { var v = q.get(k); if (v) m[k] = v.slice(0, 150); });
    var r = document.referrer ? new URL(document.referrer).hostname : '';
    if (!Object.keys(m).length && r && r !== location.hostname && !/molodist/.test(r)) { m.utm_source = r.replace(/^www\./, ''); m.utm_medium = /google|bing|duckduckgo|yahoo/.test(r) ? 'organic' : 'referral'; }
    if (Object.keys(m).length) { m.landing = location.pathname.slice(0, 150); localStorage.setItem('mlds_utm', JSON.stringify({ m: m, t: Date.now() })); }
    else { var o = JSON.parse(localStorage.getItem('mlds_utm') || 'null'); if (o && Date.now() - o.t > 90 * 864e5) localStorage.removeItem('mlds_utm'); }
  } catch (e) {} })();
  function $(q, r) { return (r || document).querySelector(q); }
  function $$(q, r) { return Array.prototype.slice.call((r || document).querySelectorAll(q)); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function money(v) { return Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' ₴'; }
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '[]') || []; } catch (e) { return []; } }
  function save(c) { try { localStorage.setItem(KEY, JSON.stringify(c)); } catch (e) {} badge(c); }
  function badge(c) { var n = (c || load()).reduce(function (a, x) { return a + x.qty; }, 0); $$('[data-cart-n]').forEach(function (e) { e.textContent = n; e.hidden = !n; }); }
  function api(path, body, cb) {
    var ts = document.querySelector('[name="cf-turnstile-response"]'); if (ts && body && typeof body === 'object' && !Array.isArray(body)) body.cft = ts.value;
    fetch(B + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); }).then(cb).catch(function () { cb(null); });
  }
  function toast(t) { var e = document.createElement('div'); e.className = 'toast'; e.textContent = t; document.body.appendChild(e); setTimeout(function () { e.remove(); }, 2200); }
  function add(id, qty) {
    var c = load(), x = c.filter(function (i) { return i.id === id; })[0];
    if (x) x.qty = Math.min(50, x.qty + qty); else c.push({ id: id, qty: qty });
    save(c);
  }
  function setQty(id, qty) { var c = load().map(function (i) { if (i.id === id) i.qty = qty; return i; }).filter(function (i) { return i.qty > 0; }); save(c); }
  function line(i, small) {
    return '<div class="ci" data-id="' + i.id + '">' + (i.img ? '<img src="' + B + '/img/' + i.img + '?w=300" alt="">' : '<span class="noimg">M</span>') +
      '<div><a href="' + B + '/shop/' + esc(i.slug) + '/">' + esc(i.name) + '</a><small>' + money(i.price) + '</small>' +
      (small ? '<small> × ' + i.qty + '</small>' : '<div class="q"><button type="button" data-d="-1">−</button><b>' + i.qty + '</b><button type="button" data-d="1">+</button></div>') +
      '</div>' + (small ? '<b>' + money(i.total) + '</b>' : '<button type="button" class="rm" aria-label="Прибрати">×</button>') + '</div>';
  }
  var cartEl = $('#cart'), last = null;
  function render(cb) {
    var uc = $('[name=use_coins]');
    var pf = $('#co-form [name=promo]'), pv = pf && pf.getAttribute('data-ok') ? pf.value.trim() : '';
    api('/api/cart', { items: load(), coins: uc && uc.checked ? null : 0, promo: pv }, function (j) {
      if (!j) return;
      last = j;
      if (!window._mshBc && /\/checkout\/$/.test(location.pathname) && j.items.length) { window._mshBc = 1;
        ga('begin_checkout', { currency: 'UAH', value: j.total, items: j.items.map(function (i) { return { item_id: String(i.id), item_name: i.name, price: i.price, quantity: i.qty }; }) }); }
      if (S.me) { try { var sig = JSON.stringify(load()); if (sig !== sessionStorage.getItem('mshop_cs')) { sessionStorage.setItem('mshop_cs', sig); api('/api/cab/cart', { items: load() }, function () {}); } } catch (x) {} }
      var ids = j.items.map(function (i) { return i.id; });
      var c = load().filter(function (i) { return ids.indexOf(i.id) >= 0; });
      if (c.length !== load().length) save(c);
      var l = $('[data-cart-list]');
      if (l) l.innerHTML = j.items.length ? j.items.map(function (i) { return line(i); }).join('') : '<p class="cart-empty">Кошик порожній</p>';
      $$('[data-cart-total]').forEach(function (e) { e.textContent = money(j.total); });
      var f = $('[data-cart-free]');
      if (f) { f.hidden = !S.free || !j.items.length; f.textContent = j.free_delivery ? 'Доставка цього замовлення — безкоштовна' : 'До безкоштовної доставки лишилось ' + money(j.to_free); }
      if (cb) cb(j);
    });
  }
  var FK = 'mshop_fav', favEl = $('#fav');
  function favs() { try { return JSON.parse(localStorage.getItem(FK) || '[]') || []; } catch (e) { return []; } }
  if (S.me && S.wish) { try { var loc = favs(), un = S.wish.concat(loc.filter(function (x) { return S.wish.indexOf(x) < 0; })); localStorage.setItem(FK, JSON.stringify(un));
    if (un.length !== S.wish.length) api('/api/cab/wish', { ids: un }, function () {}); } catch (e) {} }
  function favMark() {
    var f = favs();
    $$('[data-fav]').forEach(function (b) { b.classList.toggle('on', f.indexOf(parseInt(b.getAttribute('data-fav'), 10)) >= 0); });
    $$('[data-fav-n]').forEach(function (e) { e.textContent = f.length; e.hidden = !f.length; });
  }
  function favToggle(id) {
    var f = favs(), i = f.indexOf(id);
    if (i >= 0) f.splice(i, 1); else f.push(id);
    try { localStorage.setItem(FK, JSON.stringify(f.slice(-60))); } catch (e) {}
    if (S.me) api('/api/cab/wish', { ids: f.slice(-60) }, function () {});
    favMark(); if (i < 0) toast('Збережено');
  }
  function favRender() {
    var f = favs(), l = $('[data-fav-list]');
    if (!f.length) { l.innerHTML = '<p class="cart-empty">Тут будуть товари, які ви зберегли — натисніть на сердечко на фото товару.</p>'; return; }
    api('/api/cart', { items: f.map(function (id) { return { id: id, qty: 1 }; }) }, function (j) {
      if (!j) return;
      l.innerHTML = j.items.map(function (i) {
        return '<div class="ci" data-id="' + i.id + '">' + (i.img ? '<img src="' + B + '/img/' + i.img + '?w=300" alt="">' : '<span class="noimg">M</span>') +
          '<div><a href="' + B + '/shop/' + esc(i.slug) + '/">' + esc(i.name) + '</a><small>' + money(i.price) + '</small>' +
          '<div class="q"><button type="button" class="tocart" data-add="' + i.id + '">У кошик</button></div></div><button type="button" class="rm" data-unfav="' + i.id + '" aria-label="Прибрати">×</button></div>';
      }).join('') || '<p class="cart-empty">Збережених товарів зараз немає в наявності.</p>';
    });
  }
  favMark();
  function openCart() { if (!cartEl) return; cartEl.hidden = false; document.body.style.overflow = 'hidden'; render(); }
  function closeAll() { [cartEl, favEl, $('#mnav')].forEach(function (e) { if (e) e.hidden = true; }); document.body.style.overflow = ''; }
  document.addEventListener('click', function (e) {
    var fv = e.target.closest ? e.target.closest('[data-fav],[data-fav-open],[data-unfav]') : null;
    if (fv) {
      e.preventDefault();
      if (fv.hasAttribute('data-fav')) favToggle(parseInt(fv.getAttribute('data-fav'), 10));
      else if (fv.hasAttribute('data-unfav')) { favToggle(parseInt(fv.getAttribute('data-unfav'), 10)); favRender(); }
      else { favEl.hidden = false; document.body.style.overflow = 'hidden'; favRender(); }
      return;
    }
    var t = e.target.closest ? e.target.closest('[data-add],[data-cart-open],[data-cart-close],[data-menu],[data-menu-close],[data-search],.ci button,[data-img],[data-q]') : null;
    if (!t) return;
    if (t.hasAttribute('data-add')) {
      var q = $('#p-qty') && t.id === 'p-add' ? Math.max(1, parseInt($('#p-qty').value, 10) || 1) : 1;
      add(parseInt(t.getAttribute('data-add'), 10), q);
      var pr = parseFloat(t.getAttribute('data-price')) || 0;
      ga('add_to_cart', { currency: 'UAH', value: pr * q, items: [{ item_id: t.getAttribute('data-add'), item_name: t.getAttribute('data-name'), item_brand: t.getAttribute('data-brand') || undefined, price: pr, quantity: q }] });
      openCart();
    } else if (t.hasAttribute('data-cart-open')) openCart();
    else if (t.hasAttribute('data-cart-close') || t.hasAttribute('data-menu-close')) closeAll();
    else if (t.hasAttribute('data-menu')) { $('#mnav').hidden = false; document.body.style.overflow = 'hidden'; }
    else if (t.hasAttribute('data-search')) { var f = $('.hd-search'); f.hidden = !f.hidden; if (!f.hidden) f.q.focus(); }
    else if (t.hasAttribute('data-img')) { $('#gal-main').src = t.getAttribute('data-img'); $$('.thumbs button').forEach(function (b) { b.classList.toggle('on', b === t); }); }
    else if (t.hasAttribute('data-q')) { var i = $('#p-qty'); i.value = Math.max(1, Math.min(50, (parseInt(i.value, 10) || 1) + parseInt(t.getAttribute('data-q'), 10))); }
    else if (t.closest('.ci') && cartEl && cartEl.contains(t)) {
      var id = parseInt(t.closest('.ci').getAttribute('data-id'), 10), cur = load().filter(function (x) { return x.id === id; })[0];
      if (!cur) return;
      setQty(id, t.classList.contains('rm') ? 0 : cur.qty + parseInt(t.getAttribute('data-d'), 10));
      render(co ? renderCo : null);
    }
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeAll(); });
  $$('.vars input').forEach(function (r) { r.addEventListener('change', function () { $('#p-price').textContent = r.dataset.price; $('#p-add').setAttribute('data-add', r.value); }); });
  badge();

  /* ---------- купити в 1 клік ---------- */
  var ocm = $('#ocm'), ocf = $('#ocf');
  if (ocm) {
    document.addEventListener('click', function (e) {
      var b = e.target.closest && e.target.closest('[data-oneclick]');
      if (b) { ocm.hidden = false; ocf.setAttribute('data-id', b.getAttribute('data-oneclick')); ocf.phone.focus(); }
      if (e.target === ocm || (e.target.closest && e.target.closest('[data-oc-close]'))) ocm.hidden = true;
    });
    ocf.addEventListener('submit', function (e) {
      e.preventDefault(); var er = $('.err', ocf), ok = $('.ok', ocf), bt = $('button[type=submit]', ocf); er.hidden = true; bt.disabled = true;
      var d = { id: +ocf.getAttribute('data-id'), qty: $('#p-qty') ? (+$('#p-qty').value || 1) : 1, phone: ocf.phone.value, name: ocf.name.value, website: ocf.website.value };
      try { var m = JSON.parse(localStorage.getItem('mlds_utm') || 'null'); if (m && m.m) for (var k in m.m) d[k] = m.m[k]; } catch (x) {}
      api('/api/oneclick', d, function (j) {
        bt.disabled = false;
        if (j && j.ok) { ok.textContent = 'Дякуємо! Замовлення №' + j.number + ' прийнято — передзвонимо найближчим часом.'; ok.hidden = false; bt.hidden = true;
          ga('generate_lead', { currency: 'UAH', value: j.total, lead_source: 'one_click', items: [{ item_id: String(d.id), quantity: d.qty }] }); }
        else { er.textContent = (j && j.msg) || 'Не вдалося. Зателефонуйте нам.'; er.hidden = false; }
      });
    });
  }
  var ctf = $('#ctf');
  if (ctf) ctf.addEventListener('submit', function (e) {
    e.preventDefault(); var er = $('.err', ctf), ok = $('.ok', ctf), bt = $('button', ctf); er.hidden = true; bt.disabled = true;
    var d = { name: ctf.name.value, phone: ctf.phone.value, message: ctf.message.value, website: ctf.website.value, page: location.href };
    try { var m = JSON.parse(localStorage.getItem('mlds_utm') || 'null'); if (m && m.m) for (var k in m.m) d[k] = m.m[k]; } catch (x) {}
    api('/api/contact', d, function (j) { bt.disabled = false; if (j && j.ok) { ok.hidden = false; ctf.reset(); } else { er.textContent = (j && j.msg) || 'Не вдалося надіслати. Зателефонуйте нам.'; er.hidden = false; } });
  });
  /* ---------- оформлення ---------- */
  var co = $('#co');
  function renderCo(j) {
    j = j || last; if (!j) return;
    $('#co-items').innerHTML = j.items.length ? j.items.map(function (i) { return line(i, true); }).join('') : '<p class="mut">Кошик порожній. <a href="' + B + '/shop/">До каталогу</a></p>';
    var cn = j.coins || {}, useC = cn.use || 0, parts = ($('[name=pay]:checked') || {}).value === 'parts';
    if (parts) useC = 0;
    var pa = j.promo && j.promo.ok ? j.promo.amount : 0;
    $('#co-sum').textContent = money(j.total); $('#co-tot').textContent = money(j.total - useC - pa);
    var pr = $('#co-promo-row'); if (pr) { pr.hidden = !pa; $('#co-promo').textContent = '−' + money(pa); if (pa) $('[data-promo-name]').textContent = 'Промокод ' + j.promo.code; }
    var cb = $('#co-coins');
    if (cb) {
      cb.hidden = !(cn.on && cn.spend && cn.me && cn.balance > 0) || parts;
      var mx = $('[name=use_coins]').checked ? cn.use : cn.max;
      $('[data-coin-max]').textContent = mx; $('[data-coin-sum]').textContent = money(mx);
      $('[data-coin-note]').textContent = 'На рахунку ' + cn.balance + ' MOLODIST Coins. Знижка — до ' + cn.max + ' ₴ на це замовлення' + (cn.max < cn.balance ? ' (ліміт від вартості неакційних товарів)' : '') + '.';
      $('[name=use_coins]').disabled = !cn.max;
      $('#co-coin-row').hidden = !useC; $('#co-coin').textContent = '−' + money(useC);
      var ce = $('#co-earn'); ce.hidden = !(cn.on && cn.earn); ce.textContent = cn.me ? 'За це замовлення отримаєте +' + cn.earn + ' MOLODIST Coins.' : 'Увійдіть у кабінет — і отримаєте +' + cn.earn + ' MOLODIST Coins за це замовлення.';
    }
    var pick = $('[name=delivery]:checked', co);
    var freeNow = S.free && (j.total - pa - (j.coins && $('[name=use_coins]') && $('[name=use_coins]').checked ? (j.coins.use || 0) : 0)) >= S.free;
    $('#co-del').textContent = pick && pick.value === 'pickup' ? 'самовивіз — безкоштовно' : (freeNow ? 'безкоштовно' : 'за тарифами Нової пошти, при отриманні');
    var g = $('#co-gift'); g.hidden = !j.gifts.length;
    if (j.gifts.length && !$('#co-gifts').children.length) $('#co-gifts').innerHTML = j.gifts.map(function (x, n) { return '<label><input type="radio" name="gift" value="' + esc(x) + '"' + (n ? '' : ' checked') + '><span><b>' + esc(x) + '</b></span></label>'; }).join('');
  }
  if (co) {
    var form = $('#co-form'), cityRef = '';
    render(renderCo);
    if (form.use_coins) form.use_coins.addEventListener('change', function () { render(renderCo); });
    var pb = $('[data-promo]', form);
    if (pb) pb.addEventListener('click', function () {
      var m = $('[data-promo-msg]', form); form.promo.setAttribute('data-ok', '1');
      render(function (j) { renderCo(j); var p = j.promo || {}; m.hidden = !form.promo.value.trim(); m.textContent = p.msg || ''; m.style.color = p.ok ? '#256B37' : '#8C2F39';
        if (!p.ok) form.promo.removeAttribute('data-ok'); });
    });
    $$('[name=pay]', form).forEach(function (r) { r.addEventListener('change', function () { renderCo(); }); });
    $$('[name=delivery]', form).forEach(function (r) { r.addEventListener('change', function () {
      var v = ($('[name=delivery]:checked', form) || {}).value; $('#co-np').hidden = v === 'pickup';
      $('[data-courier]', form).hidden = v !== 'np_courier'; $('[data-wh]', form).hidden = v === 'np_courier'; renderCo(); }); });
    form.phone.addEventListener('focus', function () { if (!form.phone.value) form.phone.value = '+380'; });
    // гість ввів телефон — зберігаємо кошик, щоб адміністратор міг допомогти, якщо замовлення не буде оформлене
    var leadSig = '';
    function cartLead() {
      if (co.dataset.me) return;
      var ph = (form.phone.value || '').replace(/\D/g, ''); if (ph.length < 12) return;
      var items = load(); if (!items.length) return;
      var sig = ph + '|' + JSON.stringify(items); if (sig === leadSig) return; leadSig = sig;
      var sid_ = ''; try { sid_ = sessionStorage.getItem('mshop_sid') || ''; } catch (x) {}
      api('/api/cart/lead', { phone: form.phone.value, name: form.name.value, email: form.email ? form.email.value : '', items: items, sid: sid_ }, function () {});
    }
    form.phone.addEventListener('change', cartLead); form.phone.addEventListener('blur', cartLead);
    form.name.addEventListener('blur', function () { leadSig = ''; cartLead(); });
    try { var me = JSON.parse(localStorage.getItem('mlds_me') || 'null'); if (me) { form.name.value = me.name || ''; form.phone.value = me.phone || ''; } } catch (e) {}
    if (co.dataset.me) { try { var cm = JSON.parse(co.dataset.me); ['name', 'phone', 'email', 'city', 'warehouse'].forEach(function (k) { if (cm[k] && cm[k] !== '+') form[k].value = cm[k]; }); cityRef = cm.city_ref || ''; } catch (e) {} }
    if (co.dataset.np) {
      var timer;
      function ac(input, url, pickCb) {
        var box = input.parentNode.querySelector('.ac-l');
        input.addEventListener('input', function () {
          clearTimeout(timer);
          timer = setTimeout(function () {
            var u = url(input.value.trim()); if (!u) { box.hidden = true; return; }
            fetch(B + u).then(function (r) { return r.json(); }).then(function (j) {
              box.innerHTML = (j.rows || []).map(function (x) { return '<button type="button" data-ref="' + esc(x.ref) + '">' + esc(x.name) + '</button>'; }).join('');
              box.hidden = !box.innerHTML;
            });
          }, 250);
        });
        box.addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; input.value = b.textContent; box.hidden = true; pickCb(b.getAttribute('data-ref')); });
        document.addEventListener('click', function (e) { if (!input.parentNode.contains(e.target)) box.hidden = true; });
      }
      ac(form.city, function (q) { return q.length >= 2 ? '/api/np/cities?q=' + encodeURIComponent(q) : ''; }, function (ref) { cityRef = ref; form.warehouse.value = ''; form.warehouse.focus(); });
      ac(form.warehouse, function (q) { return cityRef ? '/api/np/wh?city=' + encodeURIComponent(cityRef) + '&q=' + encodeURIComponent(q) : ''; }, function () {});
      form.warehouse.addEventListener('focus', function () { if (cityRef && !form.warehouse.value) form.warehouse.dispatchEvent(new Event('input')); });
      chips(form, function (r) { cityRef = r; });
    }
    var busy = false;
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (busy) return;                                   // подвійне натискання — одне замовлення
      var er = $('.err', form), bt = $('button.btn', form); er.hidden = true;
      var d = { items: load(), name: form.name.value.trim(), phone: form.phone.value, email: form.email.value.trim(), city: form.city.value.trim(),
        warehouse: form.warehouse.value.trim(), addr: form.addr ? form.addr.value.trim() : '', comment: form.comment.value.trim(), website: form.website.value,
        delivery: ($('[name=delivery]:checked', form) || {}).value, pay: ($('[name=pay]:checked', form) || {}).value, gift: ($('[name=gift]:checked', form) || {}).value || '', coins: form.use_coins && form.use_coins.checked && last && last.coins ? last.coins.use : 0, parts: form.parts ? form.parts.value : '',
        promo: form.promo && form.promo.getAttribute('data-ok') ? form.promo.value.trim() : '' };
      try { var m = JSON.parse(localStorage.getItem('mlds_utm') || 'null'); if (m && m.m) for (var k in m.m) d[k] = m.m[k]; } catch (x) {}
      try { d.sid = sessionStorage.getItem('mshop_sid') || ''; } catch (x) {}
      try {   /* id відвідувача й сеансу GA4 — щоб CRM після оплати надіслала покупку в GA4 з сервера (iPhone, без повернення на «Дякуємо») */
        var gc = document.cookie.match(/(?:^|; )_ga=([^;]*)/); if (gc) { var gp = decodeURIComponent(gc[1]).split('.'); if (gp.length >= 4) d.ga_cid = gp.slice(-2).join('.'); }
        var gs = document.cookie.match(/(?:^|; )_ga_[A-Z0-9]+=([^;]*)/); if (gs) { var sv = decodeURIComponent(gs[1]), sm = sv.match(/\$?s(\d{9,12})/) || sv.match(/^GS1\.\d+\.(\d{9,12})/); if (sm) d.ga_sid = sm[1]; }
      } catch (x) {}
      if (!d.items.length) { er.textContent = 'Кошик порожній.'; er.hidden = false; return; }
      bt.disabled = true; bt.textContent = 'Оформлюємо…'; busy = true;
      api('/api/order', d, function (j) {
        bt.disabled = false; bt.textContent = 'Підтвердити замовлення'; if (!(j && j.ok)) busy = false;
        if (j && j.ok) {
          try { localStorage.setItem('mlds_me', JSON.stringify({ name: d.name, phone: d.phone })); localStorage.removeItem(KEY); } catch (x) {}
          try { sessionStorage.setItem('mshop_pur', String(j.number)); } catch (x) {}
          if (j.parts_error) alert(j.parts_error);
          location.href = j.pay_url || j.done_url;
        } else { er.textContent = (j && j.msg) || 'Не вдалося оформити. Зателефонуйте нам, будь ласка.'; er.hidden = false; }
      });
    });
  }

  /* ---------- кабінет ---------- */
  function post(path, body, cb) {
    fetch(B + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return { ok: false, msg: 'Помилка. Спробуйте ще раз.' }; }); }).then(cb).catch(function () { cb({ ok: false, msg: 'Немає звʼязку. Спробуйте ще раз.' }); });
  }
  function chips(form, setRef) {
    function sync() {                                            /* підсвічено лише місто, яке реально вибране в полі */
      var v = (form.city.value || '').toLowerCase();
      $$('[data-city]', form).forEach(function (b) { var n = 'м. ' + b.getAttribute('data-city').toLowerCase(); b.classList.toggle('on', !!v && (v === n.slice(3) || v.indexOf(n + ',') === 0 || v === n)); });
    }
    form.city.addEventListener('input', sync); form.city.addEventListener('change', sync); setTimeout(sync, 300);
    form.city.closest('form').addEventListener('click', function () { setTimeout(sync, 400); });
    $$('[data-city]', form).forEach(function (b) {
      b.addEventListener('click', function () {
        var name = b.getAttribute('data-city');
        fetch(B + '/api/np/cities?q=' + encodeURIComponent(name)).then(function (r) { return r.json(); }).then(function (j) {
          var rows = j.rows || [], hit = rows.filter(function (x) { return x.name.indexOf('м. ' + name) === 0; })[0] || rows[0];
          form.city.value = hit ? hit.name : name; setRef(hit ? hit.ref : ''); if (form.city_ref) form.city_ref.value = hit ? hit.ref : '';
          form.warehouse.value = ''; form.warehouse.focus(); sync();
        });
      });
    });
  }
  function npBind(form) {
    var tm, ref = form.city_ref ? form.city_ref.value : '';
    function bind(input, url, pick) {
      var box = input.parentNode.querySelector('.ac-l');
      input.addEventListener('input', function () {
        clearTimeout(tm);
        tm = setTimeout(function () {
          var u = url(input.value.trim()); if (!u) { box.hidden = true; return; }
          fetch(B + u).then(function (r) { return r.json(); }).then(function (j) {
            box.innerHTML = (j.rows || []).map(function (x) { return '<button type="button" data-ref="' + esc(x.ref) + '">' + esc(x.name) + '</button>'; }).join('');
            box.hidden = !box.innerHTML;
          });
        }, 250);
      });
      box.addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; input.value = b.textContent; box.hidden = true; pick(b.getAttribute('data-ref')); });
      document.addEventListener('click', function (e) { if (!input.parentNode.contains(e.target)) box.hidden = true; });
    }
    bind(form.city, function (q) { return q.length >= 2 ? '/api/np/cities?q=' + encodeURIComponent(q) : ''; }, function (r) { ref = r; if (form.city_ref) form.city_ref.value = r; form.warehouse.value = ''; form.warehouse.focus(); });
    bind(form.warehouse, function (q) { return ref ? '/api/np/wh?city=' + encodeURIComponent(ref) + '&q=' + encodeURIComponent(q) : ''; }, function () {});
    form.warehouse.addEventListener('focus', function () { if (ref && !form.warehouse.value) form.warehouse.dispatchEvent(new Event('input')); });
    chips(form, function (r) { ref = r; });
  }
  var nt = $('#ntf'), ntId = 0;
  function ntSend(phone, cb) { post('/api/notify', { id: ntId, phone: phone, website: nt ? $('[name=website]', nt).value : '' }, cb); }
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-notify],[data-ntf-close]') : null;
    if (!b || !nt) return;
    e.preventDefault();
    if (b.hasAttribute('data-ntf-close')) { nt.hidden = true; return; }
    ntId = parseInt(b.getAttribute('data-notify'), 10);
    if (S.me) return ntSend('', function (j) { toast(j.ok ? 'Повідомимо, щойно товар зʼявиться' : j.msg); });
    $('.err', nt).hidden = true; nt.hidden = false; var ph = $('[name=phone]', nt); if (!ph.value) ph.value = '+380'; ph.focus();
  });
  if (nt) $('form', nt).addEventListener('submit', function (e) {
    e.preventDefault();
    var er = $('.err', nt);
    ntSend($('[name=phone]', nt).value, function (j) { if (!j.ok) { er.textContent = j.msg; er.hidden = false; return; } nt.hidden = true; toast('Повідомимо на ' + j.to); });
  });
  var lf = $('#cab-login');
  if (lf) {
    var er = $('.err', lf), s1 = $('[data-step="1"]', lf), s2 = $('[data-step="2"]', lf);
    function fail(m) { er.textContent = m; er.hidden = false; }
    function send() {
      er.hidden = true;
      post('/api/cab/start', { phone: lf.phone.value, website: lf.website.value }, function (j) {
        if (!j.ok) return fail(j.msg);
        s1.hidden = true; s2.hidden = false; $$('[data-new]', lf).forEach(function (x) { x.hidden = !j.new; });
        $('[data-sent]', lf).textContent = 'Код надіслано ' + (j.channel === 'telegram' ? 'у Telegram (бот турботи MOLODIST)' : 'в SMS на ' + j.to) + '. Діє 5 хвилин.';
        lf.code.value = ''; lf.code.focus();
      });
    }
    lf.phone.addEventListener('focus', function () { if (!lf.phone.value) lf.phone.value = '+380'; });
    lf.addEventListener('submit', function (e) {
      e.preventDefault();
      if (s2.hidden) return send();
      er.hidden = true;
      var nw = !$('[data-new]', lf).hidden, ln = lf.last_name.value.trim(), fn = lf.first_name.value.trim();
      if (nw && (ln.length < 2 || fn.length < 2)) return fail('Вкажіть прізвище та імʼя.');
      post('/api/cab/verify', { phone: lf.phone.value, code: lf.code.value, name: (ln + ' ' + fn).trim(), ref: lf.ref.value.trim(), remember: lf.remember.checked, wish: favs() }, function (j) {
        if (!j.ok) return fail(j.msg);
        try { localStorage.setItem(FK, JSON.stringify(j.wish || [])); } catch (x) {}
        location.reload();
      });
    });
    $('[data-again]', lf).addEventListener('click', send);
    $('[data-back]', lf).addEventListener('click', function () { s2.hidden = true; s1.hidden = false; er.hidden = true; });
  }
  $$('form[data-cab]').forEach(function (f) {
    if (f.dataset.np) npBind(f);
    f.addEventListener('submit', function (e) {
      e.preventDefault();
      var d = {}, er = $('.err', f);
      $$('input[name],textarea[name]', f).forEach(function (i) { d[i.name] = i.type === 'checkbox' ? i.checked : i.value; });
      post('/api/cab/' + f.dataset.cab, d, function (j) { if (!j.ok) { er.textContent = j.msg; er.hidden = false; return; } toast('Збережено'); setTimeout(function () { location.reload(); }, 500); });
    });
  });
  document.addEventListener('click', function (e) {
    var mb = e.target.closest ? e.target.closest('[data-cab-menu]') : null;
    if (mb) { mb.parentNode.classList.toggle('open'); return; }
    var t = e.target.closest ? e.target.closest('[data-cab-logout],[data-addr-del],[data-addr-main],[data-copy]') : null;
    if (!t) return;
    if (t.hasAttribute('data-copy')) {
      var v = t.getAttribute('data-copy');
      (navigator.clipboard ? navigator.clipboard.writeText(v) : Promise.reject()).then(function () { toast(t.getAttribute('data-copied') || 'Скопійовано'); }, function () { window.prompt('Скопіюйте:', v); });
    } else if (t.hasAttribute('data-cab-logout')) post('/api/cab/logout', {}, function () { location.href = B + '/'; });
    else if (t.hasAttribute('data-addr-del')) { if (window.confirm('Видалити цю адресу?')) post('/api/cab/address', { delete: t.getAttribute('data-addr-del') }, function () { location.reload(); }); }
    else post('/api/cab/address', { main: t.getAttribute('data-addr-main') }, function () { location.reload(); });
  });

  /* ---------- пошук з підказками ---------- */
  var sf = $('.hd-search');
  if (sf) {
    var si = sf.q, sr = $('.sr', sf), st;
    si.addEventListener('input', function () {
      clearTimeout(st);
      var q = si.value.trim();
      if (q.length < 3) { sr.hidden = true; return; }
      st = setTimeout(function () {
        fetch(B + '/api/search?q=' + encodeURIComponent(q)).then(function (r) { return r.json(); }).then(function (j) {
          if (si.value.trim() !== q) return;
          sr.innerHTML = j.rows.length ? j.rows.map(function (i) {
            return '<a href="' + B + '/shop/' + esc(i.slug) + '/">' + (i.img ? '<img src="' + B + '/img/' + i.img + '?w=120" alt="">' : '<i class="noimg">M</i>') +
              '<span><small>' + esc(i.brand || '') + '</small>' + esc(i.name) + '</span><b>' + money(i.price) + (i.old ? '<s>' + money(i.old) + '</s>' : '') + '</b></a>';
          }).join('') + (j.n > j.rows.length ? '<button class="sr-all">Усі результати: ' + j.n + '</button>' : '') : '<p class="mut">Нічого не знайдено</p>';
          sr.hidden = false;
        });
      }, 220);
    });
    document.addEventListener('click', function (e) { if (!sf.contains(e.target)) sr.hidden = true; });
  }
})();
