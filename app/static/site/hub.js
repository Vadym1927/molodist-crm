/* MOLODIST — сторінка онлайн-запису (/onlain-zapys): кнопки відкривають вікна поверх сторінки, нікуди не переходимо.
   Кнопка «Назад» телефона закриває вікно (історія браузера), посилання з #розділом відкривають потрібне вікно. */
(function () {
  var root = document.documentElement, cur = null;
  function sh(id) { return id ? document.getElementById('sh-' + id) : null; }
  function ev(id) {
    (window.dataLayer = window.dataLayer || []).push({ event: 'hub_open', hub_section: id });
    if (window.mldsEv) window.mldsEv('hub_open', { hub_section: id });
  }
  function show(id, push) {
    var d = sh(id); if (!d) return false;
    if (cur && cur !== d) { try { cur.close(); } catch (x) { cur.removeAttribute('open'); } }
    if (!d.open) { if (d.showModal) d.showModal(); else d.setAttribute('open', ''); }
    cur = d; root.classList.add('hb-lock');
    var bd = d.querySelector('.hb-sh-bd'); if (bd && push !== false) bd.scrollTop = 0;
    if (push !== false) { try { history.pushState({ sh: id }, '', '#' + id); } catch (x) {} }
    ev(id);
    return true;
  }
  function hide(back) {
    if (!cur) return;
    var d = cur; cur = null;
    try { d.close(); } catch (x) { d.removeAttribute('open'); }
    root.classList.remove('hb-lock');
    if (back !== false && history.state && history.state.sh) { try { history.back(); } catch (x) {} }
  }
  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target : null; if (!t) return;
    var b = t.closest('[data-sheet]'); if (b) { e.preventDefault(); show(b.getAttribute('data-sheet')); return; }
    if (t.closest('[data-sheet-close]')) { e.preventDefault(); hide(); return; }
    var a = t.closest('[data-ob-svc],[data-ob-doc]');              // з прайсу чи картки лікаря — одразу у вікно запису
    if (a) { e.preventDefault(); setTimeout(function () { show('zapys'); }, 0); return; }
    var h = t.closest('a[href^="#"]');
    if (h) { var id = h.getAttribute('href').slice(1); if (sh(id)) { e.preventDefault(); show(id); } }
  });
  Array.prototype.forEach.call(document.querySelectorAll('.hb-sh'), function (d) {
    d.addEventListener('click', function (e) { if (e.target === d) hide(); });          // клік поза вікном (ПК)
    d.addEventListener('cancel', function (e) { e.preventDefault(); hide(); });         // Esc
  });
  window.addEventListener('popstate', function () {
    if (cur) { var d = cur; cur = null; try { d.close(); } catch (x) {} root.classList.remove('hb-lock'); }
    var id = location.hash.slice(1); if (id && sh(id) && history.state && history.state.sh) show(id, false);
  });
  // відкрити потрібне вікно одразу: /onlain-zapys#prais, ?service=…, ?doctor=…, ?g=…
  var q = {}; try { q = Object.fromEntries(new URLSearchParams(location.search)); } catch (x) {}
  var h0 = location.hash.slice(1);
  if (h0 === 'sertyfikaty' && !sh(h0) && sh('abonementy')) h0 = 'abonementy';
  if (h0 && sh(h0)) { try { history.replaceState(null, '', location.pathname + location.search); } catch (x) {} show(h0); }
  else if (q.service || q.doctor || q.g) show('zapys');
})();
