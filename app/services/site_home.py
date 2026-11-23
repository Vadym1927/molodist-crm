"""Прототип нової головної сторінки сайту MOLODIST (заміна Tilda) — кілька варіантів подачі на вибір власника.

• Відкривається лише як попередній перегляд: /_site/proto (усі варіанти) і /_site/proto/<a|b|c|d> — завжди noindex.
• Спільна «обвʼязка»: верхня шапка, бургер-меню, кнопка звʼязку, стрілка вгору, відгуки, часті запитання, контакти, карта, футер.
• Варіанти відрізняються першим екраном, порядком блоків і акцентом; тексти — за брендбуком
  (MOLODIST, «медичний простір», «пацієнт», «лікар», на «ви»; слова «клініка» в текстах немає).
• Живі дані з CRM: розділи послуг і ціни «від» (онлайн-запис), лікарі, відгуки Google, контакти.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import site_pages as sp

# ---------- варіанти ----------
VARIANTS: dict[str, dict] = {
    "a": {
        "name": "Редакційний",
        "ref": "бренд-подача: великий заголовок, мінімум тексту, сильне фото",
        "idea": "Основний варіант. Перший екран продає принцип «спершу план», далі сторінка відповідає на всі запити: проблеми, процедури, лікарі, ціни, відгуки.",
        "eyebrow": "MOLODIST · медичний простір естетики та профілактики",
        "h1": "Естетична медицина й косметологія в Києві",
        "tagline": "Шкіра, якій не потрібні фільтри",
        "lead": "У MOLODIST усе починається з діагностики й плану лікаря. Ви знаєте, що робимо, навіщо і скільки це коштує — ще до першої процедури.",
        "cta": "Записатися", "cta2": "Що таке BEAUTY PLAN",
        "order": ["hero", "facts", "entry", "plan", "doctors", "space", "reviews", "booking", "shop", "insta", "faq", "contacts"],
        "rich": True,
    },
    "b": {
        "name": "Подвійний вхід",
        "ref": "воронка: «Що вас турбує» поруч із «Я знаю процедуру»",
        "idea": "Пацієнт за 5 секунд обирає свій шлях просто на першому екрані. Найкоротша дорога до запису.",
        "eyebrow": "MOLODIST · медичний простір · Київ",
        "h1": "З чого почнемо?",
        "lead": "Оберіть, що вас турбує, або одразу знайдіть процедуру. Ціни актуальні, запис онлайн.",
        "cta": "Онлайн-запис", "cta2": "Не знаю, що обрати",
        "order": ["hero", "facts", "plan", "booking", "doctors", "reviews", "space", "shop", "faq", "contacts"],
    },
    "c": {
        "name": "BEAUTY PLAN у центрі",
        "ref": "членство / флагманський продукт на першому екрані",
        "idea": "Головна продає один вхідний продукт — консультацію з письмовим планом. Усе інше — як його продовження.",
        "eyebrow": "BEAUTY PLAN · авторська методика MOLODIST",
        "h1": "Спершу план. Потім процедури",
        "lead": "Одна консультація лікаря — і у вас письмовий план: які процедури, в якому порядку, який догляд удома. І чого робити не варто.",
        "cta": "Записатися на BEAUTY PLAN", "cta2": "Як це відбувається",
        "order": ["hero", "plan", "facts", "entry", "doctors", "reviews", "booking", "space", "shop", "faq", "contacts"],
    },
    "d": {
        "name": "Лікар як бренд",
        "ref": "довіра через людей: засновниця й команда на першому екрані",
        "idea": "Обличчя MOLODIST — лікарі. Перший екран знайомить із засновницею, далі команда й запис до конкретного лікаря.",
        "eyebrow": "Дарʼя Колган · лікар-дерматолог, засновниця MOLODIST",
        "h1": "Естетична медицина, де рішення приймає лікар",
        "lead": "Ми не продаємо процедури. Ми оглядаємо, пояснюємо й складаємо план — а ви вирішуєте, що з нього робити.",
        "cta": "Записатися до лікаря", "cta2": "Познайомитися з командою",
        "order": ["hero", "doctors", "facts", "plan", "entry", "reviews", "booking", "space", "shop", "faq", "contacts"],
    },
}
DEFAULT_VARIANT = "a"

# ---------- меню (шапка й бургер) ----------
MENU = [("Послуги", "#poslugy"), ("BEAUTY PLAN", "#beauty-plan"), ("Лікарі", "#likari"), ("Простір", "#prostir"),
        ("Відгуки", "#vidhuky"), ("Запитання", "#zapytannia"), ("Контакти", "#kontakty")]

# ---------- принципи (смуга фактів) ----------
FACTS = [("Лише лікарі", "Процедури проводять фахівці з медичною освітою", "f-doctors.webp"),
         ("Спершу діагностика", "Аналіз шкіри на OBSERV 520x і огляд лікаря", "f-diag.webp"),
         ("Письмовий план", "Процедури й домашній догляд — за пріоритетами", "f-plan.webp"),
         ("Ліцензія МОЗ", "Медичні протоколи, стерильність, закриті кабінети", "f-med.webp")]

# ---------- вхід «Що вас турбує» → сторінка напрямку (slug) або онлайн-запис ----------
# ba — пара «до / після» (файли в static/site/home), img — одне фото; interest — що підставити у форму запису
CONCERNS = [
    {"t": "Зморшки й вікові зміни", "d": "Мімічні зморшки, втрата обʼєму й чіткості овалу", "slug": "inyekczijna-kosmetologiya",
     "how": "Ботулінотерапія, контурна пластика, біоревіталізація", "ba": ["zm-c1-1.webp", "zm-c1-2.webp"], "interest": "Зморшки й вікові зміни",
     "page": "zmorshky-ta-vikovi-zminy"},
    {"t": "Акне й постакне", "d": "Висипання, сліди й рубці після них", "slug": "beautyplan",
     "how": "BEAUTY PLAN Anti Acne: лікування, догляд, процедури", "ba": ["ba-acne-1.webp", "ba-acne-2.webp"], "interest": "Акне й постакне",
     "page": "likuvannya-akne"},
    {"t": "Пігментація й судини", "d": "Плями, почервоніння, нерівний тон", "slug": "",
     "how": "IPL-терапія, пілінги, домашній догляд", "ba": ["ba-pigment-1.webp", "ba-pigment-2.webp"], "ba_v": True, "interest": "Пігментація й судини",
     "crop": "pg1", "page": "likuvannya-pigmentatsii"},
    {"t": "Тьмяна, зневоднена шкіра", "d": "Сухість, втома, розширені пори", "slug": "",
     "how": "Очищення, доглядові процедури, біоревіталізація", "ba": ["sk-glow-1.webp", "sk-glow-2.webp"], "interest": "Тьмяна, зневоднена шкіра",
     "page": "tmyana-znevodnena-shkira"},
    {"t": "Небажане волосся", "d": "Подразнення й вросле волосся після гоління", "slug": "laserepilation",
     "how": "Лазерна епіляція гібридним лазером", "img": "c-laser.webp", "interest": "Лазерна епіляція", "page": "laserepilation"},
    {"t": "Родимки й новоутворення", "d": "Хочете перевірити або видалити", "slug": "vydalennia-rodymok",
     "how": "Дерматоскопія й видалення доброякісних новоутворень", "media": ["home-moles.webp", "derma-1.webp"], "interest": "Видалення новоутворень"},
    {"t": "Напруга, набряки, втома", "d": "Хочеться розслабити мʼязи обличчя й тіла", "slug": "",
     "how": "Масаж обличчя й тіла, апаратні методики", "img": "c-massage.webp", "interest": "Масаж"},
    {"t": "Не знаю, з чого почати", "d": "Запитів кілька або процедури не дали результату", "slug": "beautyplan",
     "how": "BEAUTY PLAN: діагностика й письмовий план", "img": "bp-poster.webp", "interest": "Beauty Plan"},
]

# усі напрямки (вкладка «Я знаю процедуру»): ціна «від» — з прайсу CRM за назвою розділу; slug — сторінка напрямку, якщо вже є
DIRECTIONS = [
    {"t": "Консультація BEAUTY PLAN", "d": "Діагностика шкіри й письмовий план процедур і догляду", "slug": "beautyplan", "cat": ["консультац"]},
    {"t": "Ботулінотерапія", "d": "Мімічні зморшки, гіпергідроз, бруксизм", "slug": "inyekczijna-kosmetologiya", "cat": ["ботулінотерап"]},
    {"t": "Контурна пластика", "d": "Губи, вилиці, підборіддя, носослізна борозна", "slug": "inyekczijna-kosmetologiya", "cat": ["контурна"]},
    {"t": "Біоревіталізація й мезотерапія", "d": "Зволоження, тонус і якість шкіри", "slug": "inyekczijna-kosmetologiya", "cat": ["біоревіт"]},
    {"t": "Лазерна епіляція", "d": "Гібридний лазер з охолодженням, для жінок і чоловіків", "slug": "laserepilation", "cat": ["лазерна епіляція"]},
    {"t": "IPL-терапія", "d": "Судини, пігментація, акне", "slug": "", "cat": ["ipl"]},
    {"t": "Видалення новоутворень", "d": "Дерматоскопія, видалення родимок, папілом, бородавок", "slug": "vydalennia-rodymok", "cat": ["новоутвор", "видалення"]},
    {"t": "Очищення шкіри", "d": "Чистки обличчя й спини за медичними протоколами", "slug": "", "cat": ["очищення", "чистк"]},
    {"t": "Пілінги", "d": "Поверхневі й серединні — за типом шкіри й сезоном", "slug": "", "cat": ["пілінг"]},
    {"t": "Доглядові процедури", "d": "Догляд на професійній косметиці під ваш запит", "slug": "", "cat": ["догляд"]},
    {"t": "Масаж", "d": "Масаж обличчя й тіла, апаратні методики", "slug": "", "cat": ["масаж"]},
]

# ---------- BEAUTY PLAN ----------
PLAN = {
    "title": "BEAUTY PLAN — стратегія для вашої шкіри",
    "text": "Це не разова процедура, а план, з яким зрозуміло, що, коли і навіщо робити. Його складає лікар після діагностики, "
            "і він залишається у вас.",
    "steps": [("Діагностика", "Огляд лікаря й аналіз шкіри на OBSERV 520x: те, чого не видно в дзеркалі"),
              ("План", "Процедури за пріоритетами й бюджетом. Окремо — те, що вам не потрібно"),
              ("Домашній догляд", "Підбір засобів і корекція того, чим ви вже користуєтеся"),
              ("Супровід", "Лікар бачить, як виконується план, і коригує його на наступних візитах")],
    "gets": ["Консультація лікаря — до 2 годин", "Письмовий план процедур і догляду", "Без навʼязування: лише те, що справді потрібно"],
    "offer": {"title": "Після консультації у вас на руках",
              "items": ["План процедур за пріоритетами — з цінами", "Схема домашнього догляду", "Список того, що вам не потрібно"],
              "note": "План залишається у вас і в картці пацієнта: до нього легко повертатися на кожному візиті."},
    "slug": "beautyplan", "service_id": 387,
}

# ---------- простір ----------
SPACE = {"title": "Простір, у якому спокійно",
         "text": "Закриті кабінети, одноразові матеріали, сертифіковане обладнання. Без загальних залів і поспіху: на кожного пацієнта — стільки часу, скільки потрібно.",
         "imgs": ["clinic-1.webp", "clinic-2.webp", "bp-1.webp", "derma-1.webp"],
         # підписи до фото каруселі — у порядку SPACE_ORDER
         "alts": ["Рецепція MOLODIST у Києві — вітрина професійної косметики", "Лікар MOLODIST на консультації BEAUTY PLAN",
                  "Зона очікування медичного простору MOLODIST", "Діагностика шкіри на апараті OBSERV у MOLODIST",
                  "Кава для пацієнтів MOLODIST", "Апаратна процедура в кабінеті MOLODIST",
                  "Рецепція медичного простору MOLODIST", "Пацієнтка MOLODIST перед консультацією лікаря",
                  "Фірмові пакети MOLODIST з доглядом після процедури", "Вхід у MOLODIST, вул. Князя Романа Мстиславича, 28А, Київ"]}

# ---------- магазин ----------
SHOP = {"title": "Домашній догляд, який призначає лікар",
        "text": "Результат процедур тримається на щоденному догляді. Засоби з рекомендацій наших лікарів — в інтернет-магазині MOLODIST: з підбором, бонусами й доставкою по Україні.",
        "points": ["Професійні бренди, які лікарі призначають на консультаціях", "Підбір догляду за типом шкіри", "Доставка Новою поштою, оплата частинами"],
        "url": "https://molodist.store/shop/", "btn": "Перейти до магазину", "imgs": ["shop-1.webp", "shop-2.webp", "shop-3.webp"]}

INSTA = {"title": "MOLODIST в Instagram", "text": "Результати пацієнтів, розбори від лікарів і життя простору — щодня."}

# реквізити (підвал і контакти); код ЄДРПОУ / РНОКПП — у «Налаштування → Сайт» (поле «Код ЄДРПОУ»)
LEGAL_NAME = "ФОП Бондар Вадим Миколайович"

# ---------- часті запитання (закривають і пошукові запити) ----------
FAQ = [
    ("З чого почати, якщо я у вас уперше?", "З консультації BEAUTY PLAN. Лікар огляне шкіру, проведе діагностику на OBSERV 520x, розпитає про ваш запит "
     "і попередній досвід та складе письмовий план процедур і домашнього догляду. Якщо ви вже точно знаєте, яка процедура потрібна, — залиште заявку одразу на неї."),
    ("Що таке BEAUTY PLAN і чим він відрізняється від звичайної консультації косметолога?", "BEAUTY PLAN — авторська методика MOLODIST: повноцінна консультація лікаря до 2 годин "
     "з апаратною діагностикою шкіри. Ви отримуєте письмовий план: які процедури потрібні, в якому порядку, скільки вони коштують і який догляд потрібен удома. "
     "Окремо лікар зазначає, чого робити не варто."),
    ("Хто проводить процедури?", "Лише фахівці з медичною освітою. Інʼєкційні методики, лікування акне й видалення новоутворень виконують лікарі-дерматологи. "
     "MOLODIST працює за ліцензією МОЗ України."),
    ("Які процедури ви робите?", "Ботулінотерапію, контурну пластику, біоревіталізацію й мезотерапію, лазерну епіляцію для жінок і чоловіків, IPL-терапію судин і пігментації, "
     "видалення родимок, папілом і бородавок, чистки, пілінги, доглядові процедури та масаж обличчя й тіла."),
    ("Скільки коштують процедури?", "Ціни на сайті актуальні. Вартість курсу лікар порахує на консультації, "
     "щоб ви бачили повну суму до початку."),
    ("Чи запропонують мені зайві процедури?", "Ні. Завдання лікаря — розставити пріоритети під ваш запит і бюджет. У плані окремо зазначено, без чого можна обійтися."),
    ("Чи боляче робити інʼєкційні процедури?", "Більшість процедур проходить комфортно: лікар використовує аплікаційну анестезію й тонкі голки. "
     "Відчуття залежать від зони та індивідуальної чутливості — про це лікар попередить заздалегідь."),
    ("Через скільки буде видно результат і скільки він тримається?", "Залежить від методики. Ефект ботулінотерапії розвивається протягом 3–14 днів і тримається в середньому 3–6 місяців, "
     "контурна пластика помітна одразу. Для лазерної епіляції, IPL і лікування акне потрібен курс. Точні терміни для вас лікар назве на консультації."),
    ("Як лікують акне й постакне?", "Починаємо з діагностики й пошуку причини, а не з чистки. Лікар-дерматолог складає план: домашній догляд, за потреби медикаментозна терапія, "
     "далі пілінги, IPL та інші процедури. Для запитів з акне є окремий формат консультації — BEAUTY PLAN Anti Acne, зокрема онлайн."),
    ("Чи можна прибрати пігментацію й судинні зірочки?", "Так, у більшості випадків. Працюємо IPL-терапією, пілінгами й домашнім доглядом із SPF. "
     "Кількість процедур залежить від глибини пігменту й типу шкіри — це визначає лікар після огляду."),
    ("Чи безпечно видаляти родимки?", "Перед видаленням лікар-дерматолог обовʼязково оглядає новоутворення дерматоскопом. "
     "Видаляємо лише доброякісні утворення; якщо є сумніви, лікар скерує на додаткове обстеження."),
    ("Чи є протипоказання?", "Так, у кожної методики свої: вагітність і лактація, загострення хронічних захворювань, запалення в зоні процедури тощо. "
     "Тому перед будь-якою процедурою лікар збирає анамнез і оглядає шкіру."),
    ("Як підготуватися до візиту?", "Для консультації візьміть засоби, якими користуєтеся (або їх фото), результати аналізів, якщо є, і список процедур, які робили раніше. "
     "Перед лазерною епіляцією зону потрібно поголити напередодні."),
    ("Як записатися й чи можна перенести візит?", "Залиште заявку на сайті — адміністратор звʼяжеться з вами й підбере лікаря та час. "
     "Також можна зателефонувати або написати в Telegram чи Viber. Перенести або скасувати візит можна через адміністратора."),
    ("Де ви знаходитеся і як працюєте?", "{address}. Працюємо {hours}; неділя й понеділок — вихідні."),
]

SEO = {"title": "MOLODIST — естетична медицина й косметологія в Києві",
       "desc": "Медичний простір естетики та профілактики в Києві: діагностика шкіри, BEAUTY PLAN, інʼєкційна косметологія, "
               "лазерна епіляція, видалення новоутворень.",
       "og_title": "MOLODIST — медичний простір естетики та профілактики",
       "og_desc": "Шкіра, якій не потрібні фільтри. Естетична медицина й косметологія в Києві: спершу діагностика й план, потім процедури."}
SPACE_ORDER = [1, 12, 2, 5, 8, 3, 6, 4, 7, 9]          # чергуємо: атмосфера → лікар із пацієнтом → атмосфера…          # карусель «Простір»: інтерʼєр упереміш із деталями


def og_src():
    """Фото для картки в месенджерах і соцмережах: og-home з медіатеки або фото першого екрана."""
    from pathlib import Path
    for n in ("og-home.jpg", "og-home.webp"):
        f = sp.media_dir() / n
        if f.is_file():
            return f
    return Path(__file__).resolve().parent.parent / "static" / "site" / "home" / "og-home.webp"


def _first_media(names: list[str], base: str) -> str:
    return next((u for u in (sp.media_url(n, base) for n in names) if u), "")


def shop_hits(db: Session, n: int = 8) -> tuple[list, str]:
    """Хіти магазину: n товарів у наявності з фото, по одному від бренду (як вітрина) + адреса магазину."""
    products, shop_base = [], ""
    try:
        from . import shop_front as sf
        shop_base = (sf.cfg(db).get("canonical") or "").rstrip("/")
        if shop_base:
            seen: set = set()
            pool = sorted((i for i in sf.catalog(db)["items"] if i.get("stock") and i.get("imgs")), key=lambda i: (-(i.get("sold") or 0), -i["new"]))
            for i in pool:
                b = (i.get("brand") or "").strip().lower()
                if b and b in seen:
                    continue
                seen.add(b)
                products.append(i)
                if len(products) == n:
                    break
            if len(products) < n:                       # брендів менше — добираємо хітами
                products += [i for i in pool if i not in products][:n - len(products)]
    except Exception:  # noqa: BLE001
        products = []
    return products, shop_base


def data(db: Session, base: str, ver: str) -> dict:
    """Живі дані для головної: розділи послуг з цінами «від», лікарі, відгуки Google, фото."""
    from . import online_booking as ob
    c = ob.cfg(db)
    try:
        cats = [{"name": x["name"], "n": len(x["items"]), "lo": x["lo"]} for x in ob.pricelist(db)]
    except Exception:  # noqa: BLE001
        cats = []
    try:
        team = ob.team_cards(db, base, c)
    except Exception:  # noqa: BLE001
        team = []
    if not team:                                    # немає спеціалістів у CRM (демо) — запасні картки з текстів сайту
        team = [dict(x, id=0) for x in sp.doctors(db, list(sp.DOCTORS), base)]
    try:
        from . import google_reviews as gr
        rev = gr.public(db, c)
    except Exception:  # noqa: BLE001
        rev = None
    pages = [{"slug": s, "nav": sp.PAGES[s]["nav"], "lead": sp.PAGES[s]["lead"], "lo": sp.min_price(sp.prices(db, sp.PAGES[s]))} for s in sp.ORDER]
    plan_price = next((r["price"] for g in sp.prices(db, sp.PAGES["beautyplan"]) for r in g["rows"] if r["id"] == PLAN["service_id"]), None)
    static_hero = f"/static/site/img/zapys-hero.webp?v={ver}"

    def st(name: str) -> str:
        return f"/static/site/home/{name}?v={ver}"
    directions = []
    for d in DIRECTIONS:
        los = [x["lo"] for x in cats if any(k in x["name"].lower() for k in d["cat"])]
        directions.append({**d, "lo": min(los) if los else None})
    try:
        from . import insta_feed
        insta, insta_p = insta_feed.items(db)[:8], insta_feed.state(db).get("profile") or {}
    except Exception:  # noqa: BLE001
        insta, insta_p = [], {}
    products, shop_base = shop_hits(db)
    concerns = [{**x, "img_url": (_first_media(x["media"], base) if x.get("media") else "") or (st(x["img"]) if x.get("img") else "")} for x in CONCERNS]
    for d_ in team:                                 # квадратні портрети в картці 4:5 — обличчя на одній висоті
        low = (d_.get("name") or "").lower()
        d_["fx"] = "ch" if "чугунова" in low else ("sy" if "сирова" in low else "")
    from . import site_crops as sc
    for x in concerns:                              # фото «до / після» з картки архіву Tilda (якщо оригінал є на сервері)
        if x.get("crop"):
            src = [sc.url(f"{x['crop']}-{i}.webp", base) for i in (1, 2)]
            x["ba_src"] = src if all(src) else None
    lps = [{"slug": "likuvannya-akne", "nav": "Лікування акне"}, {"slug": "likuvannya-pigmentatsii", "nav": "Пігментація й судини"},
           {"slug": "tmyana-znevodnena-shkira", "nav": "Тьмяна, зневоднена шкіра"}, {"slug": "zmorshky-ta-vikovi-zminy", "nav": "Зморшки й вікові зміни"}]          # сторінки болей (site_landing)
    return {"lps": lps, "cats": cats, "team": team, "rev": rev, "dirs": pages, "plan_price": plan_price, "ob": c, "directions": directions, "concerns": concerns,
            "insta": insta, "insta_p": insta_p, "products": products, "shop_base": shop_base, "st": st,
            "legal": {"name": LEGAL_NAME, "code": (sp.cfg(db).get("legal_code") or sp.DEFAULT["legal_code"]).strip()},
            "img": {"hero": _first_media(["home-hero.webp"], base) or st("hero.webp"),
                    "plan": _first_media(["bp-hero.webp", "bp-1.webp"], base) or static_hero,
                    "founder": _first_media(["home-founder.webp", "doc-kolgan.webp"], base),
                    "space": [u for u in (sp.media_url(n, base) for n in SPACE["imgs"]) if u],
                    "space_a": [st(f"s-{i}.webp") for i in SPACE_ORDER]}}


def faq_jsonld(c: dict) -> str:
    import json as _json
    return _json.dumps({"@context": "https://schema.org", "@type": "FAQPage",
                        "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faq(c)]},
                       ensure_ascii=False)


def faq(c: dict) -> list[tuple[str, str]]:
    return [(q, a.replace("{address}", c["address"]).replace("{hours}", c["hours"])) for q, a in FAQ]


def org_jsonld(c: dict, url: str, og_img: str = "", team: list | None = None) -> str:
    """Розмітка schema.org для головної: медичний заклад (адреса, графік, контакти), сайт і сторінка."""
    import json
    org = {"@type": "MedicalClinic", "@id": url.rstrip("/") + "/#org", "name": "MOLODIST", "url": url,
           "description": "Медичний простір естетики та профілактики в Києві: дерматологія, косметологія, лазерні процедури.",
           "telephone": c["phone"], "email": c["email"], "medicalSpecialty": "Dermatology",
           "address": {"@type": "PostalAddress", "streetAddress": "вул. Князя Романа Мстиславича, 28А", "addressLocality": "Київ",
                       "addressCountry": "UA"},
           "openingHoursSpecification": [{"@type": "OpeningHoursSpecification", "opens": "10:00", "closes": "20:00",
                                          "dayOfWeek": ["Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]}],
           "hasMap": c.get("maps") or "", "sameAs": [x for x in (c.get("instagram"), c.get("telegram")) if x], "currenciesAccepted": "UAH"}
    if og_img:
        org["image"] = og_img
    org["logo"] = url.rstrip("/") + "/static/site/brand/mono-violet-180.png"
    org["areaServed"] = {"@type": "City", "name": "Київ"}
    if team:
        org["employee"] = [{"@type": "Physician", "name": d["name"], **({"description": d["role"]} if d.get("role") else {})}
                           for d in team if d.get("name")]
    site = {"@type": "WebSite", "@id": url.rstrip("/") + "/#site", "url": url, "name": "MOLODIST", "inLanguage": "uk",
            "publisher": {"@id": org["@id"]}}
    page = {"@type": "WebPage", "url": url, "name": SEO["title"], "description": SEO["desc"], "inLanguage": "uk",
            "isPartOf": {"@id": site["@id"]}, "about": {"@id": org["@id"]}}
    return json.dumps({"@context": "https://schema.org", "@graph": [org, site, page]}, ensure_ascii=False)
