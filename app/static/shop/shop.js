/* Магазин MOLODIST: кошик (localStorage), бічна панель, пошук, товар, оформлення замовлення */
(function () {
  var S = window.MSHOP || {}, B = S.base || '', KEY = 'mshop_cart';
  function $(q, r) { return (r || document).querySelector(q); }
  function $$(q, r) { return Array.prototype.slice.call((r || document).querySelectorAll(q)); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function money(v) { return Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' ₴'; }
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '[]') || []; } catch (e) { return []; } }
  function save(c) { try { localStorage.setItem(KEY, JSON.stringify(c)); } catch (e) {} badge(c); }
  function badge(c) { var n = (c || load()).reduce(function (a, x) { return a + x.qty; }, 0); $$('[data-cart-n]').forEach(function (e) { e.textContent = n; e.hidden = !n; }); }
  function api(path, body, cb) {
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
    api('/api/cart', { items: load(), coins: uc && uc.checked ? null : 0 }, function (j) {
      if (!j) return;
      last = j;
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
      window.dataLayer = window.dataLayer || []; window.dataLayer.push({ event: 'add_to_cart', item_name: t.getAttribute('data-name') });
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

  /* ---------- оформлення ---------- */
  var co = $('#co');
  function renderCo(j) {
    j = j || last; if (!j) return;
    $('#co-items').innerHTML = j.items.length ? j.items.map(function (i) { return line(i, true); }).join('') : '<p class="mut">Кошик порожній. <a href="' + B + '/shop/">До каталогу</a></p>';
    var cn = j.coins || {}, useC = cn.use || 0, parts = ($('[name=pay]:checked') || {}).value === 'parts';
    if (parts) useC = 0;
    $('#co-sum').textContent = money(j.total); $('#co-tot').textContent = money(j.total - useC);
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
    $('#co-del').textContent = pick && pick.value === 'pickup' ? 'самовивіз — безкоштовно' : (j.free_delivery ? 'безкоштовно' : 'за тарифами Нової пошти');
    var g = $('#co-gift'); g.hidden = !j.gifts.length;
    if (j.gifts.length && !$('#co-gifts').children.length) $('#co-gifts').innerHTML = j.gifts.map(function (x, n) { return '<label><input type="radio" name="gift" value="' + esc(x) + '"' + (n ? '' : ' checked') + '><span><b>' + esc(x) + '</b></span></label>'; }).join('');
  }
  if (co) {
    var form = $('#co-form'), cityRef = '';
    render(renderCo);
    if (form.use_coins) form.use_coins.addEventListener('change', function () { render(renderCo); });
    $$('[name=pay]', form).forEach(function (r) { r.addEventListener('change', function () { renderCo(); }); });
    $$('[name=delivery]', form).forEach(function (r) { r.addEventListener('change', function () { $('#co-np').hidden = r.value === 'pickup' && r.checked; renderCo(); }); });
    form.phone.addEventListener('focus', function () { if (!form.phone.value) form.phone.value = '+380'; });
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
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var er = $('.err', form), bt = $('button.btn', form); er.hidden = true;
      var d = { items: load(), name: form.name.value.trim(), phone: form.phone.value, email: form.email.value.trim(), city: form.city.value.trim(),
        warehouse: form.warehouse.value.trim(), comment: form.comment.value.trim(), website: form.website.value,
        delivery: ($('[name=delivery]:checked', form) || {}).value, pay: ($('[name=pay]:checked', form) || {}).value, gift: ($('[name=gift]:checked', form) || {}).value || '', coins: form.use_coins && form.use_coins.checked && last && last.coins ? last.coins.use : 0, parts: form.parts ? form.parts.value : '' };
      try { var m = JSON.parse(localStorage.getItem('mlds_utm') || 'null'); if (m && m.m) for (var k in m.m) d[k] = m.m[k]; } catch (x) {}
      if (!d.items.length) { er.textContent = 'Кошик порожній.'; er.hidden = false; return; }
      bt.disabled = true; bt.textContent = 'Оформлюємо…';
      api('/api/order', d, function (j) {
        bt.disabled = false; bt.textContent = 'Підтвердити замовлення';
        if (j && j.ok) {
          try { localStorage.setItem('mlds_me', JSON.stringify({ name: d.name, phone: d.phone })); localStorage.removeItem(KEY); } catch (x) {}
          window.dataLayer = window.dataLayer || []; window.dataLayer.push({ event: 'purchase', transaction_id: j.number, value: j.total, currency: 'UAH' });
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
    $$('[data-city]', form).forEach(function (b) {
      b.addEventListener('click', function () {
        var name = b.getAttribute('data-city');
        fetch(B + '/api/np/cities?q=' + encodeURIComponent(name)).then(function (r) { return r.json(); }).then(function (j) {
          var rows = j.rows || [], hit = rows.filter(function (x) { return x.name.indexOf('м. ' + name) === 0; })[0] || rows[0];
          form.city.value = hit ? hit.name : name; setRef(hit ? hit.ref : ''); if (form.city_ref) form.city_ref.value = hit ? hit.ref : '';
          form.warehouse.value = ''; form.warehouse.focus();
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
})();
