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

  var up = d.querySelector('.h-up'), hd = d.querySelector('.h-hd');
  function onScroll() {
    var y = window.scrollY || 0;
    if (up) up.hidden = y < 700;
    if (hd) hd.classList.toggle('stuck', y > 40);
  }
  window.addEventListener('scroll', onScroll, { passive: true }); onScroll();
})();
