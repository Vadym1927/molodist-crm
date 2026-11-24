/* Безкоштовні тести на сайті: HADS (тривога й депресія) і індекс маси тіла — відкриваються у вікні на тій самій сторінці.
   Усе рахується в браузері: відповіді нікуди не надсилаються. Кнопки: [data-tool="hads"] / [data-tool="bmi"]. */
(function () {
  var d = document;
  var Q = [
    ['Я відчуваю напруженість, мені не по собі', ['Увесь час', 'Часто', 'Час від часу, іноді', 'Зовсім не відчуваю']],
    ['Те, що приносило мені задоволення, і зараз викликає таке ж відчуття', ['Це так', 'Напевно, це так', 'В дуже малій мірі це так', 'Це зовсім не так']],
    ['Я відчуваю страх, здається, що ось-ось щось жахливе може статись', ['Так, і страх дуже сильний', 'Так, але страх не дуже сильний', 'Іноді, але це мене не турбує', 'Зовсім не відчуваю']],
    ['Я здатен розсміятися й побачити смішне в подіях', ['Це так', 'Напевно, це так', 'В дуже малій мірі це так', 'Це зовсім не так']],
    ['Метушливі думки крутяться в мене в голові', ['Постійно', 'Більшу частину часу', 'Час від часу', 'Тільки іноді']],
    ['Я відчуваю бадьорість', ['Зовсім не відчуваю', 'Дуже рідко', 'Іноді', 'Практично весь час']],
    ['Я можу легко сісти й розслабитися', ['Це так', 'Напевно, це так', 'Зрідка це так', 'Зовсім не можу']],
    ['Мені здається, що я став робити все дуже повільно', ['Практично весь час', 'Часто', 'Іноді', 'Зовсім ні']],
    ['Я відчуваю внутрішню напругу чи тремтіння', ['Зовсім не відчуваю', 'Іноді', 'Часто', 'Дуже часто']],
    ['Я не слідкую за своєю зовнішністю', ['Це так', 'Не приділяю стільки часу, скільки потрібно', 'Менше приділяю цьому уваги', 'Слідкую за собою так, як і раніше']],
    ['Я відчуваю непосидючість, мені постійно треба рухатися', ['Це так', 'Напевно, це так', 'В деякій мірі це так', 'Зовсім не відчуваю']],
    ['Мої справи можуть принести мені відчуття задоволення', ['Точно так, як і звичайно', 'Так, але не так, як раніше', 'Значно менше, ніж звичайно', 'Зовсім так не вважаю']],
    ['У мене буває раптове відчуття паніки', ['Дуже часто', 'Досить часто', 'Не так часто', 'Зовсім не буває']],
    ['Я можу отримати задоволення від цікавої книги, радіо чи телепрограми', ['Часто', 'Іноді', 'Зрідка', 'Дуже рідко']]
  ];
  // бали як у боті турботи й на сайті Дарʼї Колган: [шкала, бали за варіанти 1–4]
  var S = [['T', [3, 2, 1, 0]], ['D', [0, 1, 2, 3]], ['T', [3, 2, 1, 0]], ['D', [0, 1, 2, 3]], ['T', [3, 2, 1, 0]], ['D', [3, 2, 1, 0]],
           ['T', [0, 1, 2, 3]], ['D', [3, 2, 1, 0]], ['T', [0, 1, 2, 3]], ['D', [3, 2, 1, 0]], ['T', [3, 2, 1, 0]], ['D', [0, 1, 2, 3]],
           ['T', [3, 2, 1, 0]], ['D', [0, 1, 2, 3]]];
  var CSS = '.mt-dlg{border:0;padding:0;border-radius:22px;width:min(560px,calc(100vw - 24px));max-height:calc(100vh - 24px);color:#2A2430;' +
    'background:#fff;box-shadow:0 30px 80px rgba(42,36,48,.28);font-family:"Open Sans",system-ui,sans-serif}' +
    '.mt-dlg::backdrop{background:rgba(42,36,48,.55)}.mt-in{padding:26px 26px 22px;position:relative}' +
    '.mt-x{position:absolute;top:14px;right:14px;width:38px;height:38px;border-radius:50%;border:0;background:#F7F4F0;font-size:22px;line-height:1;cursor:pointer;color:#2A2430}' +
    '.mt-eb{font:600 11px/1 Montserrat,sans-serif;letter-spacing:.16em;text-transform:uppercase;color:#5C4B82;margin:0 0 10px}.mt-body>.mt-eb:first-child{margin-right:48px;line-height:1.4}' +
    '.mt-h{font:600 21px/1.25 Montserrat,sans-serif;text-transform:uppercase;letter-spacing:.02em;margin:0 44px 10px 0}' +
    '.mt-q{font-size:18px;line-height:1.4;margin:6px 0 14px}.mt-bar{height:4px;background:#E9E5DF;border-radius:4px;overflow:hidden;margin:4px 0 14px}' +
    '.mt-bar i{display:block;height:100%;background:#5C4B82;transition:width .25s}' +
    '.mt-opt{display:block;width:100%;text-align:left;margin:8px 0;padding:13px 16px;border-radius:14px;border:1px solid #E9E5DF;background:#fff;font:inherit;font-size:15px;color:#2A2430;cursor:pointer}' +
    '.mt-opt:hover{border-color:#C9BFE6;background:#F7F4F0}.mt-back{border:0;background:none;color:#5C4B82;font:600 12px/1 Montserrat,sans-serif;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;padding:8px 0}' +
    '.mt-f{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:8px 0 14px}.mt-f label{display:grid;gap:6px;font:600 11px/1 Montserrat,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:#3B3050}' +
    '.mt-f input{height:50px;border:1px solid #E9E5DF;border-radius:14px;padding:0 14px;font:inherit;font-size:17px;color:#2A2430;background:#F7F4F0}' +
    '.mt-f input:focus{outline:0;border-color:#5C4B82;background:#fff}' +
    '.mt-btn{display:inline-flex;align-items:center;justify-content:center;height:50px;padding:0 26px;border-radius:999px;border:0;background:#5C4B82;color:#fff;' +
    'font:600 13px/1 Montserrat,sans-serif;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;text-decoration:none}.mt-btn:hover{background:#3B3050}' +
    '.mt-res{display:grid;gap:10px;margin:6px 0 14px}.mt-r{padding:14px 16px;border-radius:16px;background:#F7F4F0}' +
    '.mt-r b{font:600 30px/1 Montserrat,sans-serif;color:#3B3050}.mt-r small{display:block;margin-top:6px;font-size:14px}' +
    '.mt-r.hi{background:#E4DEEF}.mt-note{font-size:13px;color:#6b6472;margin:0 0 14px}.mt-err{color:#a33;font-size:14px;margin:0 0 10px}';

  var dlg, body;
  function el() {
    if (dlg) return;
    var st = d.createElement('style'); st.textContent = CSS; d.head.appendChild(st);
    dlg = d.createElement('dialog'); dlg.className = 'mt-dlg';
    dlg.innerHTML = '<div class="mt-in"><button type="button" class="mt-x" aria-label="Закрити">×</button><div class="mt-body"></div></div>';
    d.body.appendChild(dlg); body = dlg.querySelector('.mt-body');
    dlg.querySelector('.mt-x').onclick = function () { dlg.close(); };
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
  }
  function ev(name, p) { try { (window.dataLayer = window.dataLayer || []).push(Object.assign({ event: name }, p || {})); } catch (x) {} }
  function cta(topic) {
    return '<p style="display:flex;gap:12px;flex-wrap:wrap;align-items:center"><a class="mt-btn" href="#zapys" data-mt-book="1" data-interest="' + topic + '">Записатися до лікаря</a>' +
      '<a class="mt-back" href="/molodist-360/">Тест «Молодість 360» →</a></p>';
  }
  function bindBook() {
    body.querySelectorAll('[data-mt-book]').forEach(function (a) { a.addEventListener('click', function () { dlg.close(); }); });
  }

  function hads() {
    var i = 0, ans = [];
    function lvl(s) { return s <= 7 ? 'норма' : s <= 10 ? 'субклінічно виражена' : 'клінічно виражена'; }
    function render() {
      var q = Q[i];
      body.innerHTML = '<p class="mt-eb">Тест на тривогу й депресію · HADS</p><div class="mt-bar"><i style="width:' + Math.round(100 * i / Q.length) + '%"></i></div>' +
        '<p class="mt-eb" style="color:#6b6472;letter-spacing:.08em">Питання ' + (i + 1) + ' з ' + Q.length + '</p><p class="mt-q">' + q[0] + '</p>' +
        q[1].map(function (o, k) { return '<button type="button" class="mt-opt" data-k="' + k + '">' + o + '</button>'; }).join('') +
        (i ? '<button type="button" class="mt-back" data-back>← Назад</button>' : '');
      body.querySelectorAll('[data-k]').forEach(function (b) {
        b.onclick = function () { ans[i] = +b.getAttribute('data-k'); i++; i < Q.length ? render() : done(); };
      });
      var bk = body.querySelector('[data-back]'); if (bk) bk.onclick = function () { i--; render(); };
    }
    function done() {
      var t = 0, dd = 0;
      S.forEach(function (s, k) { var v = s[1][ans[k]]; if (s[0] === 'T') t += v; else dd += v; });
      ev('free_test_done', { test: 'hads' });
      body.innerHTML = '<p class="mt-eb">Ваш результат · HADS</p><h3 class="mt-h">Тривога й депресія</h3><div class="mt-res">' +
        '<div class="mt-r' + (t > 7 ? ' hi' : '') + '">Тривога: <b>' + t + '</b>/21<small>' + lvl(t) + '</small></div>' +
        '<div class="mt-r' + (dd > 7 ? ' hi' : '') + '">Депресія: <b>' + dd + '</b>/21<small>' + lvl(dd) + '</small></div></div>' +
        '<p class="mt-note">0–7 — норма, 8–10 — субклінічно виражена, 11 і більше — клінічно виражена. Це скринінг, а не діагноз: ' +
        'якщо показник 8 або більше, обговоріть свій стан з лікарем або фахівцем з психічного здоровʼя. Відповіді нікуди не надсилаються.</p>' + cta('Тест HADS: консультація');
      bindBook();
    }
    render();
  }

  function bmi() {
    body.innerHTML = '<p class="mt-eb">Калькулятор</p><h3 class="mt-h">Індекс маси тіла</h3>' +
      '<form class="mt-form" novalidate><div class="mt-f"><label>Зріст, см<input name="h" type="number" inputmode="decimal" min="80" max="250" placeholder="165" required></label>' +
      '<label>Вага, кг<input name="w" type="number" inputmode="decimal" min="20" max="400" placeholder="60" required></label></div>' +
      '<p class="mt-err" hidden></p><button class="mt-btn" type="submit">Розрахувати</button></form><div class="mt-out"></div>';
    var f = body.querySelector('form'), err = body.querySelector('.mt-err'), out = body.querySelector('.mt-out');
    f.onsubmit = function (e) {
      e.preventDefault();
      var h = parseFloat(String(f.h.value).replace(',', '.')), w = parseFloat(String(f.w.value).replace(',', '.'));
      err.hidden = true;
      if (!(h >= 80 && h <= 250)) { err.textContent = 'Введіть зріст від 80 до 250 см.'; err.hidden = false; return; }
      if (!(w >= 20 && w <= 400)) { err.textContent = 'Введіть вагу від 20 до 400 кг.'; err.hidden = false; return; }
      var v = Math.round(w / Math.pow(h / 100, 2) * 10) / 10;
      var c = v < 18.5 ? 'Недостатня маса тіла' : v < 25 ? 'Нормальна маса тіла' : v < 30 ? 'Надмірна маса тіла' : 'Ожиріння';
      ev('free_test_done', { test: 'bmi' });
      out.innerHTML = '<div class="mt-res" style="margin-top:16px"><div class="mt-r' + (v < 18.5 || v >= 25 ? ' hi' : '') + '">ІМТ: <b>' + String(v).replace('.', ',') +
        '</b><small>' + c + '</small></div></div><p class="mt-note">Норма — 18,5–24,9. ІМТ не враховує мʼязову масу й склад тіла, тому це орієнтир, а не діагноз. ' +
        'Нічого не зберігаємо й не надсилаємо.</p>' + cta('ІМТ: консультація');
      bindBook();
    };
  }

  d.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('[data-tool]');
    if (!a) return;
    e.preventDefault(); el();
    var t = a.getAttribute('data-tool');
    ev('free_test_open', { test: t });
    (t === 'bmi' ? bmi : hads)();
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
  });
})();
