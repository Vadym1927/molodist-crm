/* MOLODIST — головна: бургер-меню, вкладки входу в каталог, кнопка звʼязку, стрілка вгору. */
(function () {
  var d = document, menu = d.getElementById('h-menu'), burger = d.querySelector('[data-menu-open]');
  function setMenu(on) {
    if (!menu) return;
    menu.hidden = !on; d.documentElement.classList.toggle('h-lock', on);
    if (burger) burger.setAttribute('aria-expanded', on ? 'true' : 'false');
  }
  if (burger) burger.addEventListener('click', function () { setMenu(menu.hidden); });
  d.querySelectorAll('[data-menu-close]').forEach(function (x) { x.addEventListener('click', function () { setMenu(false); }); });
  if (menu) menu.addEventListener('click', function (e) { if (e.target === menu) setMenu(false); });
  d.addEventListener('keydown', function (e) { if (e.key === 'Escape') { setMenu(false); setFab(false); } });

  d.querySelectorAll('[data-tabs]').forEach(function (box) {             // «Що вас турбує» / «Я знаю процедуру»
    var tabs = box.querySelectorAll('[role=tab]');
    tabs.forEach(function (t) {
      t.addEventListener('click', function () {
        tabs.forEach(function (x) {
          var on = x === t; x.setAttribute('aria-selected', on ? 'true' : 'false');
          var p = d.getElementById(x.getAttribute('data-tab')); if (p) p.hidden = !on;
        });
        (window.dataLayer = window.dataLayer || []).push({ event: 'home_entry_tab', tab: t.textContent.trim() });
      });
    });
  });

  var fab = d.querySelector('[data-fab]'), fabBtn = fab && fab.querySelector('.h-fab-btn'), fabList = fab && fab.querySelector('.h-fab-list');
  function setFab(on) { if (!fab) return; fabList.hidden = !on; fabBtn.setAttribute('aria-expanded', on ? 'true' : 'false'); fab.classList.toggle('open', on); }
  if (fabBtn) fabBtn.addEventListener('click', function () { setFab(fabList.hidden); });
  d.addEventListener('click', function (e) { if (fab && !fab.contains(e.target)) setFab(false); });

  /* форма запису: «що цікавить» і «як звʼязатися» → поле interest заявки (site.js бере його з window.MLDS.interest) */
  var pick = { topic: '', channel: 'Дзвінок' };
  function syncInterest() {
    var M = window.MLDS = window.MLDS || {};
    M.interest = [pick.topic, pick.channel ? 'звʼязок: ' + pick.channel : ''].filter(Boolean).join(' · ');
  }
  function setPick(kind, val) {
    pick[kind] = val;
    d.querySelectorAll('[data-pick="' + kind + '"] button').forEach(function (b) { b.setAttribute('aria-pressed', b.textContent.trim() === val ? 'true' : 'false'); });
    syncInterest();
  }
  d.querySelectorAll('[data-pick]').forEach(function (box) {
    var kind = box.getAttribute('data-pick');
    box.querySelectorAll('button').forEach(function (b) {
      b.addEventListener('click', function () {
        var v = b.textContent.trim();
        setPick(kind, (kind === 'topic' && pick.topic === v) ? '' : v);
      });
    });
  });
  syncInterest();
  d.addEventListener('click', function (e) {                             // кнопки й картки з data-interest → тема у формі
    var a = e.target.closest ? e.target.closest('[data-interest]') : null; if (!a) return;
    var v = a.getAttribute('data-interest'), known = false;
    d.querySelectorAll('[data-pick="topic"] button').forEach(function (b) { if (b.textContent.trim() === v) known = true; });
    if (known) setPick('topic', v); else { pick.topic = v; setPick('topic', v); pick.topic = v; syncInterest(); }
    (window.dataLayer = window.dataLayer || []).push({ event: 'home_interest', interest: v });
  });

  d.addEventListener('submit', function (e) {                            // імʼя — обовʼязкове (телефон перевіряє site.js)
    var f = e.target; if (!f.matches || !f.matches('form[data-need-name]')) return;
    var n = f.querySelector('[name=name]'), er = f.querySelector('.err');
    if (n && n.value.trim().length < 2) {
      e.preventDefault(); e.stopImmediatePropagation();
      if (er) { er.textContent = 'Вкажіть, будь ласка, імʼя.'; er.hidden = false; }
      n.focus();
    }
  }, true);
  d.querySelectorAll('[data-fab-close]').forEach(function (a) { a.addEventListener('click', function () { setFab(false); }); });

  d.querySelectorAll('[data-slide]').forEach(function (b) {              // відгуки: стрілки
    b.addEventListener('click', function () {
      var t = b.closest('.h-rwrap').querySelector('[data-slider]'); if (!t) return;
      t.scrollBy({ left: +b.getAttribute('data-slide') * Math.max(320, t.clientWidth * 0.8), behavior: 'smooth' });
    });
  });

  d.querySelectorAll('[data-car]').forEach(function (c) {                // простір: авторух лише на ПК
    var mq = window.matchMedia && window.matchMedia('(min-width:861px) and (prefers-reduced-motion:no-preference)');
    if (!mq || !mq.matches) return;
    var t = c.firstElementChild; if (!t || t.children.length < 4) return;
    Array.prototype.slice.call(t.children).forEach(function (n) { var k = n.cloneNode(true); k.alt = ''; k.setAttribute('aria-hidden', 'true'); k.loading = 'eager'; t.appendChild(k); });
    c.classList.add('auto');
  });

  /* спливаюча форма: усі «Записатися» відкривають її на місці, без перескоку вниз */
  var modal = d.getElementById('h-modal'), pop = d.getElementById('h-pop'), lastFocus = null;
  function setModal(on, mode) {
    if (!modal) return;
    if (on) {
      modal.querySelectorAll('[data-mode]').forEach(function (x) { x.hidden = x.getAttribute('data-mode') !== mode; });
      lastFocus = d.activeElement; modal.hidden = false; d.documentElement.classList.add('h-lock');
      setTimeout(function () { var n = modal.querySelector('[name=name]'); if (n && window.innerWidth > 860) n.focus(); }, 60);
      (window.dataLayer = window.dataLayer || []).push({ event: 'booking_popup_open', popup_mode: mode });
    } else {
      modal.hidden = true; d.documentElement.classList.remove('h-lock'); if (lastFocus && lastFocus.focus) lastFocus.focus();
    }
  }
  d.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a[href="#zapys"]') : null;
    if (!a || !modal || modal.contains(a)) return;
    e.preventDefault(); setFab(false); setMenu(false); if (pop) pop.hidden = true;
    setModal(true, a.getAttribute('data-interest') === 'Beauty Plan' ? 'bp' : 'std');
  });
  if (modal) {
    modal.addEventListener('click', function (e) { if (e.target === modal || (e.target.closest && e.target.closest('[data-modal-close]'))) setModal(false); });
    d.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !modal.hidden) setModal(false); });
  }
  d.addEventListener('submit', function (e) {                            // заявку залишили — банер більше не показуємо
    var f = e.target; if (!f.matches || !f.matches('form[data-lead]')) return;
    setTimeout(function () { var ok = f.querySelector('.ok'); if (ok && !ok.hidden) { try { localStorage.setItem('mlds_lead', String(Date.now())); } catch (x) {} } }, 2500);
  });

  /* банер: через N секунд або коли курсор іде закривати вкладку (ПК); раз на 3 дні, не після заявки */
  if (pop) {
    var force = /[?&]pop(=|&|$)/.test(location.search), T0 = Date.now(), shown = false;
    var can = function () {
      if (force) return true;
      try { if (localStorage.getItem('mlds_lead')) return false; return Date.now() - (+localStorage.getItem('mlds_pop') || 0) > 3 * 864e5; } catch (x) { return false; }
    };
    var show = function (why) {
      if (shown || !can() || (modal && !modal.hidden) || (menu && !menu.hidden)) return;
      shown = true; pop.hidden = false; requestAnimationFrame(function () { pop.classList.add('on'); });
      try { localStorage.setItem('mlds_pop', String(Date.now())); } catch (x) {}
      (window.dataLayer = window.dataLayer || []).push({ event: 'promo_popup_show', popup_reason: why });
    };
    setTimeout(function () { show('timer'); }, force ? 1200 : (+pop.getAttribute('data-delay') || 25) * 1000);
    d.addEventListener('mouseout', function (e) { if (!e.relatedTarget && e.clientY <= 0 && Date.now() - T0 > 6000) show('exit'); });
    pop.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('[data-pop-close]')) { pop.hidden = true; pop.classList.remove('on'); }
    });
  }

  var up = d.querySelector('.h-up'), hd = d.querySelector('.h-hd');
  if (up) up.addEventListener('click', function (e) {                   // плавно вгору; якщо браузер не прокрутив — одразу
    e.preventDefault(); var y0 = window.scrollY;
    try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch (x) { window.scrollTo(0, 0); }
    setTimeout(function () { if (window.scrollY >= y0 - 5) { d.documentElement.style.scrollBehavior = 'auto'; window.scrollTo(0, 0); d.documentElement.style.scrollBehavior = ''; } }, 250);
  });
  function onScroll() {
    var y = window.scrollY || 0;
    if (up) up.hidden = y < 700;
    if (hd) hd.classList.toggle('stuck', y > 40);
  }
  window.addEventListener('scroll', onScroll, { passive: true }); onScroll();
})();
