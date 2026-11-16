"""Власні сторінки магазину MOLODIST поверх перенесених із Shop-Express: оплата й доставка, обмін і повернення, блог про косметику.

Адреси старих сторінок зберігаються (SEO). Старі статті блогу про послуги клініки лишаються доступними за своїми адресами,
але в списках блогу магазину не показуються (unlisted). У html «{base}» підставляється при показі (/_shop у перегляді)."""
from __future__ import annotations

SELLER = "ФОП Бондар Вадим Миколайович"
LICENSE = "Ліцензія МОЗ України: наказ №1769 від 17.10.2024"

DELIVERY = """
<div class="info-cards">
 <div class="ic"><b>Оплата</b><ul>
  <li><b>Карткою онлайн на сайті</b> — Visa, Mastercard, Apple Pay, Google Pay (monobank).</li>
  <li><b>Покупка Частинами monobank</b> — розділіть покупку на 3 або 4 платежі.</li>
  <li><b>Карткою або готівкою в MOLODIST</b> — під час самовивозу з медичного простору.</li></ul>
  <p class="mut">Оплати при отриманні у відділенні (післяплати) немає: замовлення відправляємо після оплати.</p></div>
 <div class="ic np"><b>Доставка «Новою поштою»</b><ul>
  <li><b>У відділення або поштомат</b> по всій Україні.</li>
  <li><b>Адресна доставка курʼєром</b> «Нової пошти» по Україні.</li>
  <li>Строк доставки — <b>1–4 робочі дні</b>.</li></ul></div>
</div>
<h2>Коли відправимо</h2>
<ul class="steps"><li><b>До 18:00</b> — замовлення передаємо «Новій пошті» того ж дня.</li><li><b>Після 18:00</b> — передаємо наступного робочого дня.</li>
 <li>Щойно посилку прийме перевізник, ви отримаєте номер ТТН (у боті турботи або SMS) — він також буде в кабінеті, у «Моїх замовленнях».</li></ul>
<h2>Вартість доставки</h2>
<div class="free"><b>Від 3 000 ₴ — безкоштовно.</b> Враховується кінцева сума замовлення після всіх знижок і списання MOLODIST Coins.</div>
<p>Для замовлень до 3 000 ₴ доставку оплачує отримувач за тарифами «Нової пошти» під час отримання.</p>
<h2>Покупка Частинами monobank | Universal Bank</h2>
<p>Покупку від 3 000 ₴ можна розділити на 3 або 4 платежі. Потрібні картка monobank і доступний ліміт на Покупку Частинами — його видно в застосунку банку.</p>
<ol><li>Додайте товари в кошик.</li><li>В оформленні оберіть «Покупка Частинами monobank» і кількість платежів.</li><li>Підтвердіть покупку в застосунку monobank.</li></ol>
<h2>Самовивіз</h2>
<p>Забрати замовлення можна в медичному просторі MOLODIST: Київ, вул. Князя Романа Мстиславича, 28А (ЖК «Автограф»), вт–сб 10:00–20:00. Будь ласка, дочекайтеся повідомлення, що замовлення зібране.</p>
"""

RETURNS = """
<p>Звернення щодо обміну та повернення приймаємо в Telegram <a href="https://t.me/molodist_ua" target="_blank" rel="noopener">@molodist_ua</a>, за телефоном
<a href="tel:+380933756437">+38 (093) 375 64 37</a> або на пошту <a href="mailto:info.molodostua@gmail.com">info.molodostua@gmail.com</a>. Вкажіть номер замовлення.</p>
<h2>Що не підлягає поверненню</h2>
<p>Відповідно до Закону України «Про захист прав споживачів» і Переліку непродовольчих товарів належної якості, що не підлягають обміну
(Постанова КМУ №172 від 19.03.1994), <b>косметичні та парфумерні засоби належної якості обміну й поверненню не підлягають</b>.
Так само — засоби в аерозольному пакуванні, гребінці й масажні щітки, інструменти для манікюру й педикюру.</p>
<h2>Якщо щось пішло не так</h2>
<p>Ми уважно збираємо кожне замовлення, але людський фактор і виробничий брак трапляються навіть у найкращих брендів.
Переплутали позицію? Пошкоджена упаковка? Виявили дефект засобу? Ми допоможемо.</p>
<ul class="steps">
 <li><b>При отриманні у «Новій пошті».</b> Перевірте посилку у відділенні або при курʼєрі. Якщо вміст не відповідає замовленню чи пошкоджений — ви можете відмовитися від посилки цілком (не від окремих позицій) і складіть акт із працівником «Нової пошти».</li>
 <li><b>Пошкоджена упаковка.</b> Не відкривайте інші позиції замовлення: часткові повернення окремих товарів і товарів зі слідами використання ми прийняти не зможемо.</li>
 <li><b>Проблема виявилась пізніше.</b> Напишіть нам: номер замовлення, короткий опис, фото й відео товару та маркування партії. Неякісний товар ми замінимо або повернемо кошти відповідно до закону.</li>
</ul>
<h2>Повернення коштів</h2>
<p>Кошти за підтверджений неякісний або помилково надісланий товар повертаємо на картку, з якої була оплата, протягом 7 днів після погодження.
Для Покупки Частинами monobank повернення проходить через банк. Списані MOLODIST Coins повертаються на ваш рахунок.</p>
<p class="mut">Продавець: {seller}. {license}.</p>
""".replace("{seller}", SELLER).replace("{license}", LICENSE)

POSTS = [
    {"path": "/blog/medik8-vitamin-a-c-ta-spf/", "date": "2026-11-16", "brand": "/shop/brendi/medik8/",
     "h1": "Medik8: вітамін C зранку, ретиналь увечері — як побудувати догляд",
     "title": "Medik8: вітамін C, ретиналь і SPF — як побудувати догляд | Блог MOLODIST",
     "desc": "Як поєднувати засоби Medik8: антиоксидантна сироватка C-Tetra зранку, Crystal Retinal увечері, зволоження й SPF. Порядок нанесення і поради лікарів MOLODIST.",
     "cover": "medik8-c-tetra---antioksidantna-sirovatka-z-vitaminom-s-30ml",
     "products": ["medik8-c-tetra---antioksidantna-sirovatka-z-vitaminom-s-30ml", "medik8-crystal-retinal-3---nichniy-krem-z-retinalem-30ml",
                  "medik8-daily-radience-vitamin-c---denniy-krem-z-vitaminom-s-ta-spf30-50ml", "medik8-calmwise--soothing-cleanser-nizhna-pinka-z-hlorofilom-dlya-ochischennya-chutlivoi-shkiri"],
     "html": """
<p>Medik8 побудував свій підхід на простій формулі: <b>вітамін C удень, вітамін A вночі</b> і щоденний сонцезахист. Це базова схема, з якої лікарі MOLODIST часто починають домашній догляд — вона працює і для профілактики, і для шкіри з першими віковими змінами.</p>
<h2>Ранок: захист</h2>
<ol><li><b>Очищення.</b> Делікатна пінка <a href="{base}/shop/medik8-calmwise--soothing-cleanser-nizhna-pinka-z-hlorofilom-dlya-ochischennya-chutlivoi-shkiri/">Calmwise Soothing Cleanser</a> — для чутливої шкіри, або гель із AHA <a href="{base}/shop/medik8-surface-radiance-cleanse---ochischuyuchiy-gel-z-ana-kislotami-150ml/">Surface Radiance Cleanse</a>, якщо шкіра щільна й тьмяна.</li>
<li><b>Антиоксидант.</b> Сироватка <a href="{base}/shop/medik8-c-tetra---antioksidantna-sirovatka-z-vitaminom-s-30ml/">C-Tetra</a> з вітаміном C допомагає шкірі протистояти оксидативному стресу й вирівнює тон.</li>
<li><b>Зволоження та SPF.</b> <a href="{base}/shop/medik8-daily-radience-vitamin-c---denniy-krem-z-vitaminom-s-ta-spf30-50ml/">Daily Radiance Vitamin C SPF 30</a> поєднує обидва кроки. Сонцезахист — щодня, навіть узимку.</li></ol>
<h2>Вечір: відновлення</h2>
<ol><li><b>Очищення</b> — той самий засіб, що й зранку.</li>
<li><b>Ретиналь.</b> Лінійка Crystal Retinal має кілька концентрацій: починають із <a href="{base}/shop/medik8-crystal-retinal-3---nichniy-krem-z-retinalem-30ml/">Crystal Retinal 3</a> через вечір і поступово переходять до щоденного використання й вищих рівнів (6, 10).</li>
<li><b>Зволоження.</b> Сироватка <a href="{base}/shop/medik8-hydr8-b5-intense-intensivna--zvolozhuyucha-sirovotka-z-gialuronovoyu-kislotoyu-30-ml/">Hydr8 B5 Intense</a> з гіалуроновою кислотою або крем <a href="{base}/shop/medik8-total-moisture-daily-fasial-cream-schodenniy-zvolozhuyuchiy-krem-50ml/">Total Moisture</a>.</li></ol>
<h2>Кому варто проконсультуватися з лікарем</h2>
<p>Ретиноїди не поєднують із деякими процедурами, а під час вагітності й годування їх не використовують. Якщо шкіра реактивна, є розацеа чи активне акне — краще почати з консультації: лікар MOLODIST проведе діагностику на апараті OBSERV і підбере концентрації під вашу шкіру.</p>
"""},
    {"path": "/blog/ag-skin-azelorosa-ta-dogliad-pry-akne/", "date": "2026-11-16", "brand": "/shop/brendi/ag-skin/",
     "h1": "AG SKIN: догляд для схильної до акне й почервонінь шкіри",
     "title": "AG SKIN: азелаїнова кислота, ретинол, SPF — догляд при акне | Блог MOLODIST",
     "desc": "Лінійки AG SKIN 3-Step Acid System і Azelorosa: як очищувати, чим доглядати шкіру з висипаннями й почервоніннями, навіщо SPF. Поради лікарів MOLODIST.",
     "cover": "gel-krem-z-azeloglitsinom-i-retinolom-ag-skin-3-step-acid-system-for-acne-gel-cream-azeloglycine-retinol-50-ml",
     "products": ["ochischuvalniy-gel-z-migdalnoyu-kislotoyu-ag-skin-3-step-acid-system-for-acne-cleansing-gel-mandelic-acid-230-ml",
                  "sirovatka-z-azeloglitsinom-i-salitsilovoyu-kislotoyu-ag-skin-3-step-acid-system-for-acne-serum-azeloglycine-salicylic-acid-30-ml",
                  "gel-krem-z-azeloglitsinom-i-retinolom-ag-skin-3-step-acid-system-for-acne-gel-cream-azeloglycine-retinol-50-ml",
                  "zvolozhuyuchiy-sontsezahisniy-krem-ag-skin-dna-repair--spf-shield-moisturising-cream-spf-50-50-ml"],
     "html": """
<p>Шкіра з висипаннями потребує не «пересушування», а системного догляду: регулярного очищення пор, контролю запалення і відновлення барʼєру. У AG SKIN для цього є дві лінійки, з якими працюють лікарі MOLODIST.</p>
<h2>3-Step Acid System — для шкіри з висипаннями</h2>
<ol><li><b>Очищення:</b> <a href="{base}/shop/ochischuvalniy-gel-z-migdalnoyu-kislotoyu-ag-skin-3-step-acid-system-for-acne-cleansing-gel-mandelic-acid-230-ml/">гель із мигдальною кислотою</a> — мʼяко відлущує й не стягує шкіру.</li>
<li><b>Активна сироватка:</b> <a href="{base}/shop/sirovatka-z-azeloglitsinom-i-salitsilovoyu-kislotoyu-ag-skin-3-step-acid-system-for-acne-serum-azeloglycine-salicylic-acid-30-ml/">азелогліцин і саліцилова кислота</a> — для пор і запалених елементів.</li>
<li><b>Вечірній крем:</b> <a href="{base}/shop/gel-krem-z-azeloglitsinom-i-retinolom-ag-skin-3-step-acid-system-for-acne-gel-cream-azeloglycine-retinol-50-ml/">гель-крем з азелогліцином і ретинолом</a> — оновлення шкіри та профілактика постакне.</li></ol>
<h2>Azelorosa — для чутливої шкіри з почервоніннями</h2>
<p><a href="{base}/shop/krem-gel-dlya-oblichchya-ag-skin-azelorosa-rosy-cream-gel-50-ml/">Azelorosa Rosy Cream Gel</a> та <a href="{base}/shop/krem-gel-z-azelainovoyu-kislotoyu-ta-retinolom-ag-skin-azelorosa-cream-gel-azelaic-acid-50-ml/">крем-гель з азелаїновою кислотою</a> заспокоюють реактивну шкіру і вирівнюють тон. Для сяйва — <a href="{base}/shop/syayucha-antioksidantna-maska-dlya-oblichchya-ag-skin-azelorosa-glow-antioxidant-mask-60-ml/">антиоксидантна маска Glow</a>.</p>
<h2>SPF — обовʼязковий крок</h2>
<p>Кислоти й ретинол підвищують чутливість до сонця, а ультрафіолет посилює постзапальну пігментацію. Щоранку — <a href="{base}/shop/zvolozhuyuchiy-sontsezahisniy-krem-ag-skin-dna-repair--spf-shield-moisturising-cream-spf-50-50-ml/">DNA Repair SPF 50</a>.</p>
<p>Акне — це медичний стан. Якщо висипання глибокі, болючі чи залишають сліди, почніть із консультації лікаря: домашній догляд підбирається разом із лікуванням.</p>
"""},
    {"path": "/blog/innoaesthetics-zvolozhennia-i-spf-dlia-zhyrnoi-shkiry/", "date": "2026-11-16", "brand": "/shop/brendi/innoaesthetics/",
     "h1": "INNOAESTHETICS: зволоження й сонцезахист для жирної шкіри",
     "title": "INNOAESTHETICS: тонік NMF і SPF 50 для жирної шкіри | Блог MOLODIST",
     "desc": "Чому жирній шкірі потрібне зволоження, як працює тонік з NMF і який SPF не забиває пори. Засоби INNOAESTHETICS Inno-Derma в магазині MOLODIST.",
     "cover": "sontsezahisniy-krem-dlya-zhirnoi-shkiri-spf-50-innoaesthetics-inno-derma-sun-defense-oily-skin-spf-50-60-g",
     "products": ["zvolozhuvalniy-tonik-innoaesthetics-inno-derma-nmf-tonic-200-ml",
                  "sontsezahisniy-krem-dlya-zhirnoi-shkiri-spf-50-innoaesthetics-inno-derma-sun-defense-oily-skin-spf-50-60-g"],
     "html": """
<p>Поширена помилка жирної шкіри — відмовлятися від зволоження. Зневоднена шкіра часто виробляє ще більше себуму, а пори виглядають ширшими. Тому в догляді важливий баланс: легке зволоження і захист, які не обтяжують.</p>
<h2>Крок 1. Тонік з NMF</h2>
<p><a href="{base}/shop/zvolozhuvalniy-tonik-innoaesthetics-inno-derma-nmf-tonic-200-ml/">Inno-Derma NMF Tonic</a> містить компоненти натурального зволожувального фактора — вони допомагають шкірі утримувати воду. Наносьте після вмивання, перед сироваткою чи кремом.</p>
<h2>Крок 2. SPF 50 для жирної шкіри</h2>
<p><a href="{base}/shop/sontsezahisniy-krem-dlya-zhirnoi-shkiri-spf-50-innoaesthetics-inno-derma-sun-defense-oily-skin-spf-50-60-g/">Inno-Derma Sun Defense Oily Skin SPF 50</a> створений саме для жирної та комбінованої шкіри: легка текстура, без важкого фінішу. Сонцезахист щодня — найкраща профілактика пігментації й фотостаріння.</p>
<h2>Коли потрібен лікар</h2>
<p>Якщо жирність супроводжується висипаннями, почервоніннями чи лущенням, причина може бути глибшою за догляд. Лікар MOLODIST проведе діагностику шкіри й підбере домашній догляд разом із процедурами.</p>
"""},
    {"path": "/blog/pro-molodist-diagnostyka-ta-dogliad-vid-likaria/", "date": "2026-11-16", "brand": "",
     "h1": "MOLODIST: косметика, яку призначає лікар",
     "title": "MOLODIST — медичний простір естетики та профілактики в Києві | Блог MOLODIST",
     "desc": "Чому в магазині MOLODIST лише професійна косметика: діагностика шкіри на OBSERV, бюті-план від лікаря, домашній догляд, що підтримує результат процедур.",
     "cover_img": "/static/shop/hero/observ.webp",
     "products": [],
     "html": """
<p>MOLODIST — медичний простір естетики та профілактики в Києві. Ми віримо, що догляд за шкірою починається не з полиці магазину, а з діагностики.</p>
<h2>Діагностика на OBSERV</h2>
<p>На консультації лікар фотографує шкіру на апараті OBSERV у різних режимах світла. Так видно те, що не помітно в дзеркалі: пігментацію в глибших шарах, судинну сітку, стан пор і рівень зволоження.</p>
<h2>Бюті-план</h2>
<p>За результатами діагностики лікар складає бюті-план: процедури в медичному просторі й засоби домашнього догляду. Засоби з вашого плану зʼявляються в кабінеті магазину, у розділі «Рекомендації лікаря» — їх можна замовити в один клік.</p>
<h2>Чому лише професійна косметика</h2>
<p>У магазині — бренди, з якими щодня працюють наші лікарі: Medik8, AG SKIN, Medicare, Demax, Tebiskin, Image Skincare, Rejuran та інші. Ми купуємо їх лише в офіційних дистрибʼюторів і зберігаємо з дотриманням умов виробника.</p>
<h2>Записатися на консультацію</h2>
<p>Не впевнені, що підійде саме вам? Запишіться на консультацію онлайн — лікар підбере догляд під тип і стан вашої шкіри. Адреса: Київ, вул. Князя Романа Мстиславича, 28А (ЖК «Автограф»).</p>
"""},
]

OWN = {
    "/for-customers/dostavka-i-oplata/": {"h1": "Оплата і доставка", "title": "Оплата і доставка — магазин професійної косметики MOLODIST",
                                          "desc": "Оплата карткою онлайн, Покупка Частинами monobank. Доставка «Новою поштою» по Україні за 1–4 дні, безкоштовно від 3 000 ₴. Відправка в день замовлення до 18:00.",
                                          "html": DELIVERY},
    "/for-customers/povernennya-tovaru/": {"h1": "Обмін і повернення", "title": "Обмін і повернення товару — магазин MOLODIST",
                                           "desc": "Умови обміну та повернення косметики в магазині MOLODIST: що не підлягає поверненню, як діяти при пошкодженні чи браку, повернення коштів.",
                                           "html": RETURNS},
}


def pages(imported: dict) -> dict:
    """Перенесені сторінки + власні. Старі статті блогу про послуги — доступні за адресою, але без показу в списках."""
    out = {}
    for p, v in imported.items():
        v = dict(v)
        if p.startswith("/blog/") and p != "/blog/":
            v["unlisted"] = True
        out[p] = v
    for p, v in OWN.items():
        out[p] = {**out.get(p, {}), **v, "path": p, "own": True}
    blog = dict(out.get("/blog/") or {})
    out["/blog/"] = {**blog, "path": "/blog/", "h1": "Блог MOLODIST", "title": "Блог про догляд за шкірою — поради лікарів | MOLODIST",
                     "desc": "Як підібрати й поєднувати професійну косметику: Medik8, AG SKIN, INNOAESTHETICS. Поради лікарів медичного простору MOLODIST."}
    for post in POSTS:
        out[post["path"]] = {**post, "title": post["title"], "own": True, "post": True}
    return out


def render(html: str, base: str) -> str:
    return (html or "").replace("{base}", base)
