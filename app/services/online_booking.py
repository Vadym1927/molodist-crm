"""Онлайн-запис на сайті клініки (сторінка /onlain-zapys): послуга → спеціаліст → день → час → контакти.

• Послуги — з прайсу CRM (CbService): позначені «Доступна для онлайн-запису» (картка послуги або масово в
  «Налаштування → Онлайн-запис»), активні, з ціною. Назва — «Коротка назва для онлайн-запису» або назва послуги.
• Хто надає послугу — «Співробітники» в картці послуги (doctor_ids); якщо не вказано — ті, хто вже проводив її (історія записів).
• Вільний час — зі змін графіка (StaffShift) мінус записи CRM, записи CleverBox цього спеціаліста, неробочі дні (DayOff)
  і зайнятий кабінет зміни. Крок сітки, мінімальний запас до візиту й горизонт — у налаштуваннях.
• Запис створює Appointment (source=online, статус «Записаний») + звернення з мітками реклами + сповіщення в Telegram.
  Пацієнт — за номером телефону (існуючий) або нова картка. Посилання з токеном — переглянути / скасувати запис.
• Сертифікати й абонементи — заявка (звернення) і, якщо підключено платіжну систему, посилання на оплату.
• Гайди, тексти, магазин — у налаштуваннях (SyncState online_booking).
"""
from __future__ import annotations

import json
import re
import secrets
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Appointment, CbService, DayOff, Doctor, Patient, StaffShift, SyncState, Visit

KEY = "online_booking"
CARE_BOT = "https://t.me/molodist_assistant_bot"
SLUG = "onlain-zapys"          # molodist.com.ua/onlain-zapys (стара адреса /zapys — переадресація 301)
GUIDES_DEFAULT = [
    {"title": "Тарілка здорового харчування 360", "tag": "Харчування", "on": True,
     "url": "/static/site/guides/tarilka-360.pdf", "img": "/static/site/guides/tarilka-360.webp",
     "text": "Проста інфографіка за матеріалами Гарвардської школи громадського здоровʼя: як скласти збалансовану тарілку — "
             "½ овочі й фрукти, ¼ цільнозернові продукти, ¼ білок і трохи корисних рослинних олій. "
             "Збалансоване харчування — основа здорової шкіри й профілактики передчасного старіння. "
             "Роздрукуйте памʼятку й прикріпіть на холодильник — вона щодня нагадуватиме про правильний вибір."},
    {"title": "Самодіагностика молочних залоз", "tag": "Профілактика", "on": True,
     "url": "/static/site/guides/samodiahnostyka-molochnykh-zaloz.pdf", "img": "/static/site/guides/samodiahnostyka-molochnykh-zaloz.webp",
     "text": "Рак молочної залози, виявлений на ранній стадії, добре лікується: пʼятирічна виживаність перевищує 90%. "
             "У гайді — покроковий самоогляд: перед дзеркалом, стоячи й лежачи, з ілюстраціями до кожного кроку. "
             "Оглядайте груди щомісяця на 5–10 день циклу, у менопаузі — в одну й ту саму дату. "
             "Самоогляд не замінює УЗД і мамографію: будь-яку зміну покажіть лікарю."},
    {"title": "Як зрозуміти, що родимку потрібно перевірити", "tag": "Профілактика", "on": True,
     "url": "/static/site/guides/kontrol-rodymok.pdf", "img": "/static/site/guides/kontrol-rodymok.webp",
     "text": "Більшість родимок доброякісні, але деякі з часом змінюються — і важливо помітити це вчасно. "
             "Гайд пояснює правило ABCDE: асиметрія, нерівні краї, неоднорідний колір, діаметр понад 6 мм і зміни з часом. "
             "Окремо — чого не робити з новоутвореннями і коли показати родимку лікарю найближчим часом. "
             "Регулярний самоогляд і профілактична дерматоскопія допомагають виявити небезпечні зміни на ранній стадії."},

    {"title": "Перший візит у MOLODIST", "tag": "Знайомство", "on": False,
     "text": "Прийдіть за 5–10 хвилин до початку — адміністратор зустріне вас і допоможе заповнити анкету.\n"
             "Візьміть із собою результати аналізів і список засобів домашнього догляду, якщо вони є.\n"
             "Розкажіть лікарю про алергії, хронічні стани й препарати, які приймаєте, — це впливає на вибір методик.\n"
             "Консультація починається з огляду й діагностики: лікар пояснить варіанти й складе план, а рішення завжди за вами."},
    {"title": "Підготовка до лазерної епіляції", "tag": "Лазер", "on": False,
     "text": "За 2 тижні до процедури не засмагайте й не відвідуйте солярій.\n"
             "За 4 тижні не видаляйте волосся воском, шугарингом чи епілятором — лише гоління.\n"
             "За добу до візиту поголіть зону процедури.\n"
             "У день процедури не наносьте на шкіру креми, дезодоранти й олії.\n"
             "Попередьте лікаря про прийом ліків і загострення шкірних станів — процедуру, можливо, варто перенести."},
    {"title": "Після інʼєкційних процедур", "tag": "Інʼєкції", "on": False,
     "text": "У перші години не торкайтеся зони процедури й не наносьте декоративну косметику.\n"
             "1–2 тижні уникайте сауни, лазні, солярію та активної засмаги; кілька днів — інтенсивних тренувань.\n"
             "Легкий набряк чи синець у місці інʼєкції — можлива реакція, яка зазвичай минає за кілька днів.\n"
             "Дотримуйтеся індивідуальних рекомендацій лікаря — вони мають пріоритет над загальними.\n"
             "Якщо щось турбує — напишіть або зателефонуйте нам, ми на звʼязку."},
    {"title": "Домашній догляд між візитами", "tag": "Догляд", "on": False,
     "text": "Базовий догляд — це очищення, зволоження й сонцезахист щодня, незалежно від сезону.\n"
             "Активні засоби (кислоти, ретиноїди) підбирає лікар — не поєднуйте їх самостійно.\n"
             "SPF наносьте вранці як останній етап догляду й оновлюйте протягом дня на відкритому сонці.\n"
             "Засоби, призначені лікарем, можна придбати в нашому магазині косметики з доставкою."},
]
FAQ_SEO = [
    ["Як записатися до косметолога чи дерматолога в Києві онлайн?",
     "Оберіть послугу на цій сторінці, лікаря та вільний час — запис займає хвилину і не потребує дзвінка. Підтвердження надійде одразу, "
     "а напередодні візиту адміністратор нагадає про запис. Записатися можна на консультацію дерматокосметолога, ботулінотерапію, "
     "контурну пластику, біоревіталізацію, лазерну епіляцію, IPL-терапію, RF-ліфтинг, апаратний масаж STRATOSPHERE, пілінги й очищення шкіри."],
    ["З чого почати, якщо я вперше в MOLODIST?",
     "З консультації Beauty Plan: лікар проводить діагностику шкіри, визначає тип старіння й складає персональний план процедур і домашнього догляду. "
     "Так ви отримуєте результат курсом, а не випадковими процедурами, і точно знаєте, що, коли і навіщо робити."],
    ["Де знаходиться MOLODIST і як доїхати?",
     "Медичний простір MOLODIST — у Києві на лівому березі: вул. Князя Романа Мстиславича, 28А, ЖК «Автограф», поруч зі станціями метро "
     "«Дарниця» та «Чернігівська». Для входу на територію — домофон 15#. Працюємо з вівторка по суботу, 10:00–20:00."],
    ["Скільки коштують послуги?",
     "Актуальні ціни на всі послуги — у розділі «Прайс» на цій сторінці. Остаточну вартість лікар підтверджує після огляду, "
     "бо обсяг процедури залежить від стану шкіри та зони. Курси процедур вигідніше брати абонементом."],
    ["Чи діють знижки й акції?",
     "Так: −50% на перший візит з лазерної епіляції, перший апаратний масаж STRATOSPHERE за 1 500 ₴, −15% для військовослужбовців ЗСУ "
     "та −10% до дня народження. Програми лояльності між собою не сумуються."],
]
FAQ_DEFAULT = [
    ["Чи потрібна передоплата?", "Ні, запис безкоштовний. Оплата — після візиту: карткою або готівкою."],
    ["Як перенести або скасувати запис?", "За посиланням «Мій запис», яке ви отримаєте після запису, або зателефонуйте чи напишіть нам. Будь ласка, попереджайте щонайменше за 3 години."],
    ["Не знаю, яку процедуру обрати. Що робити?", "Запишіться на консультацію лікаря — він огляне шкіру, пояснить варіанти й складе план. Або залиште номер, і адміністратор допоможе з вибором."],
    ["Чи підтверджують запис?", "Так, адміністратор звʼяжеться з вами напередодні візиту, щоб підтвердити час."],
] + FAQ_SEO
DEFAULT = {
    "on": False,                 # сторінка опублікована на сайті (поки вимкнено — лише перегляд у CRM)
    "step": 30,                  # крок сітки часу, хв
    "lead_h": 3,                 # найближчий запис — не раніше ніж через N годин
    "days": 30,                  # на скільки днів уперед відкрито запис
    "cancel_h": 3,               # скасувати онлайн можна не пізніше ніж за N годин
    "max_active": 2,             # майбутніх онлайн-записів з одного номера
    "doctors_off": [],           # спеціалісти, до яких немає онлайн-запису
    "shop_url": "https://molodist.store/shop/",
    "shop_text": "Професійна косметика, яку призначають наші лікарі, — з доставкою по Україні.",
    "hero_img": "",              # файл із «Сайт → Фото»
    "note": "Адміністратор підтвердить запис напередодні візиту.",
    "guides": GUIDES_DEFAULT,
    "faq": FAQ_DEFAULT,
    "certs_on": True,
    "subs_on": True,
    "care_bot": CARE_BOT,       # посилання на Бот турботи (Telegram)
    # правила програми лояльності (як в онлайн-записі CleverBox, 16.11.2026)
    "promos": "Лазерна епіляція: −50% на перший візит від основної вартості, комплекси зон — зі знижкою 15%\n"
              "Апаратний масаж STRATOSPHERE: перший візит — 1 500 ₴, щоб ви могли оцінити результат на оригінальному апараті\n"
              "−15% для військовослужбовців ЗСУ — постійно, за наявності документа\n"
              "−10% до дня народження — одноразово, за 7 днів до і 7 днів після",
    "cert_rules": "Номінал — 2 000, 3 000, 5 000 або 10 000 ₴\n"
                  "Використати можна за один візит, не пізніше ніж через 2 місяці з дати покупки\n"
                  "На будь-які послуги й товари MOLODIST\n"
                  "Сертифікат іменний: активується на отримувача в момент покупки, не передається іншим і не ділиться на кількох осіб",
    "sub_rules": "Абонемент — для процедур, які дають найкращий результат курсом: оплата курсу одразу вигідніша, ніж поодинці.\n"
                 "Термін дії — 180 днів з дати покупки.",
    "loyalty_note": "Програми лояльності між собою не сумуються.",
    "preview_key": "",           # посилання для перегляду до публікації (без входу в CRM)
    "og_title": "Онлайн-запис у MOLODIST — оберіть послугу, лікаря й час",
    "og_desc": "Медичний простір естетики та профілактики в Києві. Запис онлайн за хвилину, подарункові сертифікати, гайди з догляду.",
}
# іконки плиток сторінки онлайн-запису (набір MOLODIST booking icons, SVG 24×24, лінія 1.5)
HUB_ICONS = {"cal": "<rect x=\"3\" y=\"5\" width=\"18\" height=\"17\" rx=\"3\"/><path d=\"M7 2v6M17 2v6M3 11h18m-13 5 3 3 5-5\"/>", "prais": "<path d=\"M9 6h12M9 12h12M9 18h12\"/><circle cx=\"4\" cy=\"6\" r=\"1\"/><circle cx=\"4\" cy=\"12\" r=\"1\"/><circle cx=\"4\" cy=\"18\" r=\"1\"/>", "sert": "<rect x=\"3\" y=\"6\" width=\"18\" height=\"15\" rx=\"2\"/><path d=\"M3 11h18M10 6v15M10 6C5 6 5 2 7 2c2 0 3 4 3 4Zm0 0c5 0 5-4 3-4-2 0-3 4-3 4Z\"/>", "abon": "<path d=\"m5 7 13-4 2 6a2 2 0 0 0 1 4l-1 2M3 10h15v3a2 2 0 0 0 0 4v3H3v-3a2 2 0 0 0 0-4v-3Z\"/>", "akc": "<path d=\"m5 20 14-16\"/><circle cx=\"6\" cy=\"6\" r=\"3\"/><circle cx=\"18\" cy=\"18\" r=\"3\"/>", "likari": "<path d=\"M5 3H3v5a5 5 0 0 0 10 0V3h-2M8 13v3a5 5 0 0 0 10 0v-3\"/><circle cx=\"18\" cy=\"10\" r=\"3\"/>", "shop": "<path d=\"M5 7h14l1 13H4L5 7Z\"/><path d=\"M9 8V5a3 3 0 0 1 6 0v3\"/>", "gaid": "<path d=\"M12 5v16M12 5C9 3 5 3 2 4v15c3-1 7-1 10 2 3-3 7-3 10-2V4c-3-1-7-1-10 1Z\"/>", "chat": "<path d=\"M16 12v-6a3 3 0 0 0-3-3H5a3 3 0 0 0-3 3v5a3 3 0 0 0 3 3v4l4-4h4a3 3 0 0 0 3-2Z\"/><path d=\"M16 8h3a3 3 0 0 1 3 3v5a3 3 0 0 1-3 3v3l-4-3h-4a3 3 0 0 1-3-3v-2\"/>", "pin": "<path d=\"M19 10c0 6-7 12-7 12S5 16 5 10a7 7 0 1 1 14 0Z\"/><circle cx=\"12\" cy=\"10\" r=\"2.5\"/>", "star": "<path d=\"M20 18a9 9 0 1 0-15 0l-1 4 5-2a9 9 0 0 0 11-2Z\"/><path d=\"m12 6 1.7 3.4 3.8.6-2.8 2.7.7 3.8-3.4-1.8-3.4 1.8.7-3.8L6.5 10l3.8-.6L12 6Z\"/>", "faq": "<circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M9.5 8.5a2.5 2.5 0 1 1 4 2c-1 .8-1.5 1.1-1.5 2.5\"/><circle cx=\"12\" cy=\"17\" r=\".5\" fill=\"#5C4B82\"/>", "tel": "<path d=\"M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z\"/>", "site": "<circle cx=\"12\" cy=\"12\" r=\"9\"/><ellipse cx=\"12\" cy=\"12\" rx=\"4\" ry=\"9\"/><path d=\"M3 12h18M5 6.5c4 2 10 2 14 0M5 17.5c4-2 10-2 14 0\"/>"}

WD = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
MON = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]


class BookingError(ValueError):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


def og_src(c: dict):
    """Фото для картки в соцмережах: фото шапки з налаштувань або стандартне."""
    from pathlib import Path
    from . import site_pages as sp
    n = sp.media_name(c.get("hero_img") or "") if c.get("hero_img") else ""
    f = sp.media_dir() / n if n else None
    return f if f and f.is_file() else Path(__file__).resolve().parent.parent / "static" / "site" / "img" / "zapys-hero.webp"


def preview_key(db: Session, c: dict | None = None) -> str:
    """Ключ посилання для перегляду неопублікованої сторінки без входу в CRM (створюється один раз; можна оновити)."""
    c = c or cfg(db)
    if not c.get("preview_key"):
        c["preview_key"] = secrets.token_urlsafe(9)
        save_cfg(db, c)
    return c["preview_key"]


def new_preview_key(db: Session) -> str:
    c = cfg(db)
    c["preview_key"] = secrets.token_urlsafe(9)
    save_cfg(db, c)
    return c["preview_key"]


def now_local() -> datetime:
    return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None)


# ---------------- налаштування ----------------
def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    c = json.loads(json.dumps(DEFAULT))
    if st and st.value:
        try:
            c.update(json.loads(st.value))
        except ValueError:
            pass
    return c


def save_cfg(db: Session, c: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps({k: c[k] for k in DEFAULT if k in c}, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def _int(v, lo: int, hi: int, dflt: int) -> int:
    try:
        return max(lo, min(hi, int(float(str(v).replace(",", ".")))))
    except (TypeError, ValueError):
        return dflt


def save_form(db: Session, f) -> dict:
    """Форма «Налаштування → Онлайн-запис» (без послуг — вони окремо)."""
    c = cfg(db)
    c["on"] = bool(f.get("on"))
    c["step"] = _int(f.get("step"), 5, 120, 30)
    c["lead_h"] = _int(f.get("lead_h"), 0, 168, 3)
    c["days"] = _int(f.get("days"), 1, 120, 30)
    c["cancel_h"] = _int(f.get("cancel_h"), 0, 168, 3)
    c["max_active"] = _int(f.get("max_active"), 1, 10, 2)
    c["certs_on"], c["subs_on"] = bool(f.get("certs_on")), bool(f.get("subs_on"))
    for k, n in (("care_bot", 200), ("shop_url", 300), ("shop_text", 300), ("note", 300), ("hero_img", 120), ("og_title", 120), ("og_desc", 300)):
        c[k] = str(f.get(k) or "").strip()[:n]
    for k in ("promos", "cert_rules", "sub_rules", "loyalty_note"):
        if f.get(k) is not None:
            c[k] = "\n".join(text_lines(str(f.get(k))))[:2000]
    on_ids = {int(x) for x in f.getlist("doctor") if str(x).isdigit()}
    c["doctors_off"] = sorted(d.id for d in _staff(db) if d.id not in on_ids)
    c["faq"] = [[a.strip()[:200], b.strip()[:800]] for a, _, b in (ln.partition("|") for ln in str(f.get("faq") or "").splitlines()) if a.strip() and b.strip()][:20]
    guides = []
    for i in range(20):
        t = str(f.get(f"g_title_{i}") or "").strip()
        if not t:
            continue
        guides.append({"title": t[:120], "tag": str(f.get(f"g_tag_{i}") or "").strip()[:40], "on": bool(f.get(f"g_on_{i}")),
                       "text": str(f.get(f"g_text_{i}") or "").strip()[:4000], "url": str(f.get(f"g_url_{i}") or "").strip()[:500],
                       "img": str(f.get(f"g_img_{i}") or "").strip()[:500]})
    c["guides"] = guides
    save_cfg(db, c)
    return c


# ---------------- каталог ----------------
def _staff(db: Session) -> list[Doctor]:
    return [d for d in db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name))
            if getattr(d, "in_calendar", True) is not False]


def online_doctors(db: Session, c: dict | None = None) -> list[Doctor]:
    off = set((c or cfg(db)).get("doctors_off") or [])
    return [d for d in _staff(db) if d.id not in off]


_HIST: dict = {"at": 0.0, "map": {}}


def performers(db: Session) -> dict[int, set[int]]:
    """Послуга → лікарі, які її вже проводили (записи CRM, зокрема перенесена історія). Кеш 10 хв."""
    if time.time() - _HIST["at"] < 600 and _HIST["map"]:
        return _HIST["map"]
    m: dict[int, set[int]] = {}
    for did, svcs in db.execute(select(Appointment.doctor_id, Appointment.services).where(Appointment.doctor_id.is_not(None))
                                .order_by(Appointment.start.desc()).limit(20000)):
        for s in svcs or []:
            try:
                m.setdefault(int(s.get("id")), set()).add(did)
            except (TypeError, ValueError):
                continue
    _HIST.update(at=time.time(), map=m)
    return m


def reset_cache() -> None:
    _HIST.update(at=0.0, map={})


def service_doctors(db: Session, sv: CbService, docs: list[Doctor], hist: dict | None = None) -> list[int]:
    ids = {d.id for d in docs}
    own = [i for i in (sv.doctor_ids or []) if i in ids]
    if sv.doctor_ids:
        return own
    h = (hist if hist is not None else performers(db)).get(sv.id, set())
    return sorted(i for i in h if i in ids)


GROUP_ORDER = ["Консультації", "Масаж", "Очищення шкіри", "Доглядові процедури", "Пілінги", "Ботулінотерапія", "Біоревіталізація й мезотерапія",
               "Контурна пластика", "Лазерна епіляція", "Лазерна епіляція для чоловіків"]


def site_group(sv: CbService) -> str:
    """Розділ послуги на сторінці запису: споріднені категорії прайсу — в одному місці
    (усі консультації разом, увесь масаж разом, ботулінотерапія з кількох категорій — разом)."""
    name, cat = (sv.online_name or sv.name or "").lower(), (sv.category or "").strip()
    low = cat.lower()
    men = " для чоловіків" if "чолов" in low else ""
    if "консультац" in name or "консультац" in low:
        return "Консультації"
    if "масаж" in name or "масаж" in low:
        return "Масаж"
    if any(k in low for k in ("ботул", "ботокс", "диспорт", "нейронокс")):
        return "Ботулінотерапія" + men
    if "лазерна епіляція" in low:
        return "Лазерна епіляція" + men
    if "біоревіт" in low or "мезотерап" in low:
        return "Біоревіталізація й мезотерапія"
    if low.startswith("ipl") or name.startswith("ipl"):
        return IPL_GROUP
    title = re.sub(r"\s*\(.*\)\s*$", "", cat).strip() or "Інші послуги"
    return title[:1].upper() + title[1:]


# прибрано з прайсу й запису на сайті (рішення власника 16.11.2026)
HIDE_NAMES = ("масаж обличчя мануальний", "масаж обличчя міофасціальний", "масаж обличчя класичний + карбокс",
              "міофасціальний масаж обличчя + ферментотерап")
HIDE_CATS = ("molodist reset", "корекції/повторний огляд")
PRICE_ONLY_HIDE_CATS = ("корекц", "повторний огляд")
HEAD_DOCTOR = "Дарʼя Колган"
IPL_GROUP = "IPL-терапія (судини, пігментація, акне)"


def _low(t: str | None) -> str:
    return " ".join((t or "").lower().replace("’", "ʼ").replace("'", "ʼ").split())


def hidden(sv: CbService, price: bool = False) -> bool:
    """Послуга не показується на сайті: окремі масажі обличчя й MOLODIST RESET; корекції — лише в прайсі."""
    name, cat = _low(sv.online_name or sv.name), _low(sv.category)
    if any(k in name for k in HIDE_NAMES) or any(cat.startswith(k) for k in HIDE_CATS):
        return True
    return price and any(k in cat for k in PRICE_ONLY_HIDE_CATS)


def disp_name(sv: CbService) -> str:
    """Назва для пацієнта: без службових позначок «(ВИТРАТНІ)»; консультації головного лікаря — з іменем лікаря."""
    n = re.sub(r"\s*\(\s*витратн[іи]\s*\)", "", (sv.online_name or sv.name or ""), flags=re.I)
    n = " ".join(n.split())
    if "головного лікаря" in n.lower() and HEAD_DOCTOR.split()[1] not in n:
        n += f" — {HEAD_DOCTOR}"
    return n


def group_sex(name: str) -> str:
    """Позначка для іконки розділу: m — для чоловіків, f — лазерна епіляція (жіноча)."""
    low = (name or "").lower()
    return "m" if "чоловік" in low else "f" if "лазерна епіляція" in low else ""


def _mins(sv: CbService) -> int:
    return int(sv.duration or sv.time_min or 30)


def catalog(db: Session, c: dict | None = None) -> dict:
    """Дані для сторінки: категорії з послугами (лише ті, де є кому надати) і спеціалісти."""
    from . import price_cats
    c = c or cfg(db)
    docs = online_doctors(db, c)
    hist = performers(db)
    dprices = price_cats.doctor_prices(db)
    cats: dict[str, list] = {}
    used: set[int] = set()
    for sv in db.scalars(select(CbService).where(CbService.online.is_(True)).order_by(CbService.category, CbService.name)):
        if sv.active is False or not sv.price or hidden(sv):
            continue
        dids = service_doctors(db, sv, docs, hist)
        if not dids:
            continue
        used.update(dids)
        dp = {}
        for i in dids:
            v = (dprices.get(i) or {}).get(str(sv.id), (dprices.get(i) or {}).get(sv.id))
            if v not in (None, ""):
                dp[i] = float(v)
        cats.setdefault(site_group(sv), []).append(
            {"id": sv.id, "name": disp_name(sv), "price": float(sv.price), "min": _mins(sv),
             "desc": (sv.description or "").strip()[:600], "docs": dids, "dp": dp})
    order = {n: i for i, n in enumerate(GROUP_ORDER)}
    return {"cats": [{"name": k, "items": sorted(v, key=lambda x: (x["price"], x["name"]))}
                     for k, v in sorted(cats.items(), key=lambda kv: (order.get(kv[0], 100), kv[0]))], "doctor_ids": sorted(used)}


PRICE_HIDE = ("співробітник", "модель", "тест")


def pricelist(db: Session, online_ids: set[int] | None = None) -> list[dict]:
    """Прайс для сторінки: усі активні послуги з ціною за розділами сайту (службові категорії приховано)."""
    cats: dict[str, list] = {}
    for sv in db.scalars(select(CbService).order_by(CbService.name)):
        low = f"{sv.category or ''} {sv.name or ''}".lower()
        if sv.active is False or not sv.price or any(k in low for k in PRICE_HIDE) or hidden(sv, price=True):
            continue
        cats.setdefault(site_group(sv), []).append({"id": sv.id, "name": disp_name(sv), "price": float(sv.price), "min": _mins(sv),
                                                    "online": bool(online_ids and sv.id in online_ids)})
    order = {n: i for i, n in enumerate(GROUP_ORDER)}
    out = []
    for k, v in sorted(cats.items(), key=lambda kv: (order.get(kv[0], 100), kv[0])):
        v = _dedupe(k, v)
        out.append({"name": GROUP_FIX.get(k, k), "items": sorted(v, key=lambda x: (x["price"], x["name"])), "lo": min(x["price"] for x in v),
                    "sex": group_sex(k)})
    return out


# Брендбук: назви препаратів ботулотоксину на сайті не пишемо — у прайсі лише зона; та сама зона різними препаратами — один рядок «від»
BOTOX_BRANDS = (r"\b(botox|ботокс|xeomin|ксеомін|dysport|диспорт|neuronox|нейронокс|relatox|релатокс|botulax|ботулакс|nabota|набота|"
                r"azzalure|bocouture)\b")
GROUP_FIX = {"Лабараторія CSD": "Лабораторія CSD"}


def clean_name(name: str, group: str = "") -> str:
    """Назва послуги для прайсу на сайті: без брендів ботулотоксину; у розділі ботулінотерапії — лише зона."""
    n = name or ""
    bot = (group or "").lower().startswith("ботулінотерапія")
    if bot:
        n = re.sub(r"лікування\s+підвищеної\s+пітливості", "Зменшення пітливості", n, flags=re.I)
        n = re.sub(BOTOX_BRANDS + r"|ботулінотерапі[яюїє]", " ", n, flags=re.I)
        n = re.sub(r"\bчолов\w*", " ", n, flags=re.I)
    else:
        n = re.sub(BOTOX_BRANDS, "ботулінотерапія", n, flags=re.I)
    n = re.sub(r"\(\s*\)", " ", n)
    n = re.sub(r"(\S)\(", r"\1 (", n)
    n = re.sub(r"\s*\+\s*", " + ", n)
    n = re.sub(r"\s{2,}", " ", n).strip(" ·-,+")
    if bot:                                           # «Лоб + Міжбрів'я + Очі» → «Лоб + міжбрів'я + очі» (абревіатури й латиницю не чіпаємо)
        w = n.split(" ")
        n = " ".join([w[0]] + [x.lower() if x.lower() not in ("ніфертітті", "нефертіті") and re.fullmatch(r"\(?[А-ЯІЇЄҐ][а-яіїєґʼ'’]+\)?", x) else x for x in w[1:]]) if w else n
    return (n[:1].upper() + n[1:]) if n else (name or "")


def _dedupe(group: str, items: list[dict]) -> list[dict]:
    by: dict = {}
    for i in items:
        nm = clean_name(i["name"], group)
        k = re.sub(r"[ʼ'’`\s]", "", nm.lower())
        if k in by:
            b = by[k]
            b["from"] = b["from"] or b["price"] != i["price"]
            if i["price"] < b["price"]:
                b.update(id=i["id"], price=i["price"], min=i["min"])
            b["online"] = b["online"] or i["online"]
        else:
            by[k] = {**i, "name": nm, "from": False}
    return list(by.values())


def team_cards(db: Session, base: str = "/_site", c: dict | None = None) -> list[dict]:
    """Блок «Наші лікарі»: головна лікарка, далі лікарі в порядку сайту (Колган, Сирова, Чугунова) — зокрема ті,
    до кого немає онлайн-запису; потім інші спеціалісти з онлайн-записом."""
    from . import site_pages as sp
    keys = list(sp.DOCTORS)
    staff = {d.id: d for d in _staff(db)}
    extra = [d.id for d in staff.values() if any(v["match"] in (d.name or "").lower() for v in sp.DOCTORS.values())]
    cards = doctor_cards(db, None, base, c, include=set(extra))

    def rank(x):
        low = (staff[x["id"]].name or "").lower() if x["id"] in staff else ""
        return next((i for i, k in enumerate(keys) if sp.DOCTORS[k]["match"] in low), len(keys))
    return sorted(cards, key=rank)


def doctor_cards(db: Session, ids: list[int] | None = None, base: str = "/_site", c: dict | None = None, include: set | None = None) -> list[dict]:
    """Короткі картки лікарів: текст сайту (site_pages.DOCTORS) або посада й біо з картки спеціаліста CRM."""
    from . import site_pages as sp
    out = []
    on = {d.id for d in online_doctors(db, c)} | set(include or ())
    for d in _staff(db):
        if d.id not in on or (ids is not None and d.id not in ids):
            continue
        low = (d.name or "").lower()
        key, sd = next(((k, v) for k, v in sp.DOCTORS.items() if v["match"] in low), (None, None))
        photo = (sp.media_url(f"doc-{key}.webp", base) if key else "") or (d.photo_url if (d.photo_url or "").startswith(("https://", "/")) else "")
        name = sd["name"] if sd else " ".join(d.name.split()[:2][::-1]) if len(d.name.split()) >= 2 else d.name
        out.append({"id": d.id, "name": name, "role": (sd["role"] if sd else (d.position or "Спеціаліст")),
                    "bio": (sd["bio"] if sd else (d.bio or "")).strip()[:500], "photo": photo,
                    "initials": "".join(w[0] for w in name.split()[:2]).upper()})
    return out


# ---------------- вільний час ----------------
def _hm(day: date, s: str) -> datetime:
    return datetime.combine(day, datetime.strptime(s[:5], "%H:%M").time())


def _busy(db: Session, d_from: date, d_to: date, docs: list[Doctor]) -> tuple[dict, dict]:
    """Зайняті інтервали: (лікар, день) → [(start, end)], (кабінет, день) → [(start, end, лікар)]."""
    from ..integrations.cleverbox import same_person
    a0, a1 = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    by_doc: dict = {}
    by_place: dict = {}
    recs: set[int] = set()
    for a in db.scalars(select(Appointment).where(Appointment.start >= a0, Appointment.start <= a1,
                                                  Appointment.status.notin_(("no_show", "cancelled")))):
        if a.doctor_id:
            by_doc.setdefault((a.doctor_id, a.start.date()), []).append((a.start, a.end))
        if a.place:
            by_place.setdefault((a.place, a.start.date()), []).append((a.start, a.end, a.doctor_id))
        if a.cb_record_id:
            recs.add(a.cb_record_id)
    for v in db.scalars(select(Visit).where(Visit.starts_at >= a0, Visit.starts_at <= a1, Visit.status == "booked")):
        if any(str(v.external_id or "").startswith(f"{r}-") for r in recs):      # уже є записом CRM
            continue
        d = next((x for x in docs if same_person(x.name, v.master_name)), None)
        if d:
            by_doc.setdefault((d.id, v.starts_at.date()), []).append((v.starts_at, v.ends_at or v.starts_at + timedelta(minutes=60)))
    return by_doc, by_place


def _free(start: datetime, end: datetime, spans: list) -> bool:
    return not any(start < b[1] and b[0] < end for b in spans)


MAX_SERVICES = 5


def service_ids(v) -> list[int]:
    """«9701,9702», [9701, 9702] або 9701 → список id послуг без повторів (не більше MAX_SERVICES)."""
    raw = v if isinstance(v, (list, tuple)) else re.split(r"[,\s;]+", str(v if v is not None else ""))
    out: list[int] = []
    for x in raw:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in out:
            out.append(i)
    return out[:MAX_SERVICES]


def pick(db: Session, ids, docs: list[Doctor]) -> tuple[list[CbService], list[int]]:
    """Послуги візиту й спеціалісти, які надають їх усі (один візит — один спеціаліст)."""
    svs = [db.get(CbService, i) for i in service_ids(ids)]
    if not svs or any(not sv or not sv.online or sv.active is False or not sv.price for sv in svs):
        return [], []
    hist = performers(db)
    common: set[int] | None = None
    for sv in svs:
        d = set(service_doctors(db, sv, docs, hist))
        common = d if common is None else common & d
    return svs, sorted(common or [])


def availability(db: Session, service_id, doctor_id: int | None, d_from: date | None = None, d_to: date | None = None,
                 c: dict | None = None, now: datetime | None = None) -> dict[date, list[dict]]:
    """День → [{t: «10:30», docs: [id, …]}] — вільні початки для послуги або кількох послуг поспіль (усі спеціалісти або один)."""
    c = c or cfg(db)
    now = now or now_local()
    docs = online_doctors(db, c)
    svs, dids = pick(db, service_id, docs)
    if not svs:
        return {}
    if doctor_id:
        dids = [i for i in dids if i == doctor_id]
    if not dids:
        return {}
    first = (now + timedelta(hours=int(c["lead_h"])))
    d_from = max(d_from or now.date(), first.date())
    d_to = min(d_to or (now.date() + timedelta(days=int(c["days"]))), now.date() + timedelta(days=int(c["days"])))
    if d_to < d_from:
        return {}
    shifts = {(s.doctor_id, s.day): s for s in db.scalars(select(StaffShift).where(StaffShift.day >= d_from, StaffShift.day <= d_to,
                                                                                    StaffShift.doctor_id.in_(dids)))}
    offs = {(o.doctor_id, o.day) for o in db.scalars(select(DayOff).where(DayOff.day >= d_from, DayOff.day <= d_to, DayOff.doctor_id.in_(dids)))}
    by_doc, by_place = _busy(db, d_from, d_to, docs)
    dur, step = timedelta(minutes=sum(_mins(sv) for sv in svs)), timedelta(minutes=int(c["step"]))
    out: dict[date, dict[str, list[int]]] = {}
    for (did, day), sh in shifts.items():
        if (did, day) in offs:
            continue
        try:
            t, end = _hm(day, sh.start), _hm(day, sh.end)
        except ValueError:
            continue
        place = [b for b in by_place.get((sh.place, day), []) if b[2] != did] if sh.place else []
        busy = by_doc.get((did, day), [])
        while t + dur <= end:
            if t >= first and _free(t, t + dur, busy) and _free(t, t + dur, place):
                out.setdefault(day, {}).setdefault(t.strftime("%H:%M"), []).append(did)
            t += step
    return {d: [{"t": k, "docs": sorted(v)} for k, v in sorted(x.items())] for d, x in sorted(out.items())}


def day_label(d: date, today: date | None = None) -> str:
    today = today or now_local().date()
    pre = "сьогодні, " if d == today else "завтра, " if d == today + timedelta(days=1) else ""
    return f"{pre}{WD[d.weekday()]} {d.day} {MON[d.month - 1]}"


# ---------------- запис ----------------
def norm_phone(v: str | None) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) == 9:
        d = "380" + d
    if len(d) == 10 and d.startswith("0"):
        d = "38" + d
    return "+" + d if len(d) == 12 and d.startswith("380") else None


def _token(db: Session) -> str:
    return secrets.token_urlsafe(12)


def by_token(db: Session, token: str | None) -> Appointment | None:
    t = str(token or "").strip()
    if not (10 <= len(t) <= 40) or not re.fullmatch(r"[A-Za-z0-9_\-]+", t):
        return None
    return db.scalar(select(Appointment).where(Appointment.online_token == t))


def book(db: Session, data: dict, c: dict | None = None, now: datetime | None = None) -> Appointment:
    """Створити онлайн-запис. data: service, doctor (0 — будь-який), day (РРРР-ММ-ДД), time (ГГ:ХХ), name, phone, comment + мітки реклами."""
    from . import appeals as ap, appointments as aps
    c = c or cfg(db)
    now = now or now_local()
    phone = norm_phone(data.get("phone"))
    if not phone:
        raise BookingError("phone", "Перевірте номер телефону: +380 і 9 цифр.")
    name = " ".join(str(data.get("name") or "").split())[:120]
    if len(name) < 2:
        raise BookingError("name", "Вкажіть, будь ласка, імʼя.")
    try:
        sids, did = service_ids(data.get("services") or data.get("service")), int(data.get("doctor") or 0)
        day = date.fromisoformat(str(data.get("day") or ""))
        tm = datetime.strptime(str(data.get("time") or ""), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise BookingError("slot", "Оберіть послугу, день і час.") from None
    svs = [x for x in (db.get(CbService, i) for i in sids) if x]
    total_min = sum(_mins(x) for x in svs)
    slots = availability(db, sids, did or None, day, day, c, now).get(day, [])
    slot = next((s for s in slots if s["t"] == tm), None)
    if not svs or not slot:
        raise BookingError("taken", "На жаль, цей час щойно зайняли. Оберіть, будь ласка, інший.")
    pat = ap.find_patient(db, phone)
    if pat:
        n_act = sum(1 for a in db.scalars(select(Appointment).where(Appointment.patient_id == pat.id, Appointment.source == "online",
                                                                    Appointment.start >= now, Appointment.status.notin_(("cancelled", "no_show")))))
        if n_act >= int(c["max_active"]):
            raise BookingError("limit", "У вас уже є активні онлайн-записи. Щоб додати ще один, зателефонуйте або напишіть нам.")
        start = _hm(day, tm)
        if any(a.start < start + timedelta(minutes=total_min) and start < a.end for a in db.scalars(
                select(Appointment).where(Appointment.patient_id == pat.id, Appointment.start >= datetime.combine(day, datetime.min.time()),
                                          Appointment.start <= datetime.combine(day, datetime.max.time()),
                                          Appointment.status.notin_(("cancelled", "no_show"))))):
            raise BookingError("dup", "Ви вже записані на цей час. Якщо хочете змінити запис — напишіть або зателефонуйте нам.")
    else:
        pat = Patient(full_name=name, phone=phone, notes="Картку створено з онлайн-запису на сайті", source="Сайт / онлайн-запис")
        db.add(pat)
        db.flush()
    # «будь-який спеціаліст» — той, у кого цього дня менше записів
    if did:
        doc_id = did
    else:
        load = {i: 0 for i in slot["docs"]}
        for a in db.scalars(select(Appointment).where(Appointment.start >= datetime.combine(day, datetime.min.time()),
                                                      Appointment.start <= datetime.combine(day, datetime.max.time()),
                                                      Appointment.doctor_id.in_(slot["docs"]), Appointment.status.notin_(("cancelled", "no_show")))):
            load[a.doctor_id] = load.get(a.doctor_id, 0) + 1
        doc_id = min(load, key=lambda i: (load[i], i))
    from . import price_cats
    dp = (price_cats.doctor_prices(db).get(doc_id) or {})

    def _price(sv):
        v = dp.get(str(sv.id), dp.get(sv.id))
        return float(v if v not in (None, "") else (sv.price or 0))
    sh = db.scalar(select(StaffShift).where(StaffShift.doctor_id == doc_id, StaffShift.day == day))
    comment = " ".join(str(data.get("comment") or "").split())[:500]
    a = aps.save(db, {"patient_id": pat.id, "doctor_id": doc_id, "start": _hm(day, tm), "source": "online",
                      "services": [{"id": sv.id, "name": sv.name, "price": _price(sv), "minutes": _mins(sv), "category": sv.category} for sv in svs],
                      "place": sh.place if sh else None, "booking_source": "Сайт / онлайн-запис",
                      "comment": ("Онлайн-запис із сайту" + (f" · {name}" if name and name.lower() not in (pat.full_name or "").lower() else "")
                                  + (f". Коментар пацієнта: {comment}" if comment else ""))})
    a.online_token = _token(db)
    db.commit()
    reset_cache()
    return a


def svc_names(a: Appointment) -> str:
    return ", ".join(str(x.get("name") or "") for x in a.services or [] if x.get("name"))


def can_cancel(a: Appointment, c: dict, now: datetime | None = None) -> bool:
    now = now or now_local()
    return a.status in ("booked", "confirmed") and a.start - now >= timedelta(hours=int(c["cancel_h"]))


def cancel(db: Session, a: Appointment, c: dict | None = None, now: datetime | None = None) -> None:
    from . import appointments as aps
    c = c or cfg(db)
    if a.status == "cancelled":
        return
    if not can_cancel(a, c, now):
        raise BookingError("late", "Онлайн скасувати вже не можна — зателефонуйте або напишіть нам, будь ласка.")
    aps.set_status(db, a, "cancelled", "Скасовано пацієнтом онлайн")


def view(db: Session, a: Appointment, c: dict | None = None) -> dict:
    """Запис для сторінки «Мій запис» і відповіді API (без медичних даних)."""
    c = c or cfg(db)
    d = db.get(Doctor, a.doctor_id) if a.doctor_id else None
    card = next(iter(doctor_cards(db, [d.id], c=c)), None) if d else None
    svc = {"name": svc_names(a), "price": sum(float(x.get("price") or 0) for x in a.services or [])}
    return {"token": a.online_token, "when": f"{day_label(a.start.date())}, {a.start:%H:%M}", "date": a.start.strftime("%d.%m.%Y"),
            "time": a.start.strftime("%H:%M"), "end": a.end.strftime("%H:%M"), "service": svc.get("name") or "", "price": svc.get("price") or 0,
            "doctor": (card or {}).get("name") or (d.name if d else ""), "status": a.status, "can_cancel": can_cancel(a, c),
            "past": a.start < now_local(), "gcal": gcal_url(a, (card or {}).get("name") or (d.name if d else ""), svc.get("name") or "")}


def gcal_url(a: Appointment, doctor: str, service: str) -> str:
    """Посилання «Додати в Google Календар» — подія відкривається одразу, без завантаження файлу."""
    from urllib.parse import urlencode
    from . import site_pages as sp
    tz = ZoneInfo(settings.timezone)
    f = lambda x: x.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    site = sp.DEFAULT if hasattr(sp, "DEFAULT") else {}
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": f"MOLODIST: {service}", "dates": f"{f(a.start)}/{f(a.end)}",
        "details": (f"Спеціаліст: {doctor}. " if doctor else "") + f"Телефон: {site.get('phone_text') or ''}",
        "location": site.get("address") or "м. Київ, вул. Князя Романа Мстиславича, 28А", "ctz": settings.timezone})


def ics(a: Appointment, v: dict, site: dict) -> str:
    """Файл календаря (.ics) для запису."""
    tz = ZoneInfo(settings.timezone)
    f = lambda x: x.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")  # noqa: E731
    esc = lambda s: str(s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")  # noqa: E731
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MOLODIST//Online booking//UK", "BEGIN:VEVENT",
                        f"UID:molodist-{a.id}@molodist.com.ua", f"DTSTAMP:{datetime.utcnow():%Y%m%dT%H%M%SZ}", f"DTSTART:{f(a.start)}", f"DTEND:{f(a.end)}",
                        f"SUMMARY:{esc('MOLODIST: ' + v['service'])}", f"LOCATION:{esc(site.get('address'))}",
                        f"DESCRIPTION:{esc('Спеціаліст: ' + v['doctor'] + '. Телефон: ' + str(site.get('phone_text') or ''))}",
                        "BEGIN:VALARM", "TRIGGER:-PT3H", "ACTION:DISPLAY", "DESCRIPTION:Візит у MOLODIST", "END:VALARM",
                        "END:VEVENT", "END:VCALENDAR", ""])


def alert_text(db: Session, a: Appointment, base: str, cancelled: bool = False) -> str:
    from html import escape as e
    d = db.get(Doctor, a.doctor_id) if a.doctor_id else None
    svc = {"name": svc_names(a)}
    p = a.patient
    return "\n".join([("❌ Онлайн-запис скасовано пацієнтом" if cancelled else "🗓 Новий онлайн-запис із сайту"),
                      f"👤 {e(p.full_name if p else '—')}", f"📞 {e(p.phone or '—') if p else '—'}",
                      f"💜 {e(str(svc.get('name') or ''))}", f"👩‍⚕️ {e(d.name if d else '—')}",
                      f"🕐 {a.start:%d.%m.%Y %H:%M}–{a.end:%H:%M}",
                      f"{(base or '').rstrip('/')}/calendar?start={a.start:%Y-%m-%d}&open={a.id}"])


def notify(db: Session, a: Appointment, base: str, cancelled: bool = False) -> bool:
    """Сповіщення в чат «Заявки Сайти» (якщо призначено) або власнику."""
    from . import alerts, site_leads as sl
    text = alert_text(db, a, base, cancelled)
    chat = str(sl.cfg(db).get("tg_chat") or "").strip()
    if chat:
        return bool(alerts._api("sendMessage", data={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"}))
    return bool(alerts.send(text))


def link_appeal(db: Session, a: Appointment, data: dict) -> int | None:
    """Звернення з мітками реклами (utm / gclid) для онлайн-запису — щоб запис було видно у воронці реклами."""
    from . import appeals as ap, site_leads as sl
    from ..models import Appeal
    svc = {"name": svc_names(a)}
    p = a.patient
    d = {**{k: v for k, v in data.items() if k.startswith("utm_") or k in ("gclid", "cid", "page")},
         "name": p.full_name if p else "", "phone": p.phone if p else "", "interest": str(svc.get("name") or "")[:200],
         "formname": f"{SLUG}:booking", "comment": f"Онлайн-запис: {a.start:%d.%m.%Y %H:%M}"}
    r = sl.ingest(db, d)
    aid = r.get("appeal_id")
    if aid:
        a.appeal_id = aid
        db.commit()
        apl = db.get(Appeal, aid)
        ok = next((s["name"] for s in ap.dirs(db)["statuses"] if s.get("type") == "success"), None)
        if apl and ok and not r.get("duplicate"):
            try:
                ap.set_status(db, apl, ok)
            except Exception:  # noqa: BLE001
                db.rollback()
    return aid


# ---------------- сертифікати й абонементи ----------------
SUB_GROUPS = ["Масаж", "IPL", "RF-ліфтинг", "Лазерна епіляція", "Очищення шкіри", "Пілінги", "Доглядові процедури", "Інші абонементи"]
_SUB_KEYS = [("Масаж", ("масаж", "stratosphere")), ("IPL", ("ipl",)), ("RF-ліфтинг", ("rf", "ліфтинг", "endymed")),
             ("Лазерна епіляція", ("лазер", "епіляц")), ("Очищення шкіри", ("чистк", "очищен")), ("Пілінги", ("пілінг",)),
             ("Доглядові процедури", ("догляд", "reset", "карбокс", "мікростр"))]


def sub_group(text: str) -> str:
    """Розділ абонемента на сторінці: за назвою (Масаж, IPL, RF-ліфтинг …)."""
    low = (text or "").lower()
    for name, keys in _SUB_KEYS:
        if any(re.search(rf"(?<![a-zа-яіїєґ]){re.escape(k)}", low) for k in keys):
            return name
    return "Інші абонементи"


def _visits_word(n: int) -> str:
    a, b = n % 10, n % 100
    return "процедура" if a == 1 and b != 11 else "процедури" if 2 <= a <= 4 and not 12 <= b <= 14 else "процедур"


def _toks(t: str) -> set[str]:
    t = _low(t).replace("2 насадки", "дві насадки").replace("-", " ")
    return {w for w in re.findall(r"[a-zа-яіїєґʼ0-9]+", t) if len(w) > 1 and w not in ("до", "та", "ресурс", "імпульсів", "1000", "2000")}


def _closest(base: str, by_name: dict):
    """Послуга прайсу для абонемента без точної назви: усі слова абонемента є в назві послуги, найменше зайвих."""
    want = _toks(base)
    best = None
    for n, sv in by_name.items():
        have = _toks(n)
        if want and want <= have and ("екзосом" in n) == ("екзосом" in _low(base)):
            extra = len(have - want)
            if best is None or extra < best[0]:
                best = (extra, sv)
    return best[1] if best else None


def sub_view(db: Session, x: dict, by_name: dict | None = None) -> dict:
    """Абонемент для пацієнта: зрозуміла назва («… — курс 6 процедур»), ціна однієї процедури й вигода проти разових візитів."""
    if by_name is None:
        by_name = {_low(sv.name): sv for sv in db.scalars(select(CbService)) if sv.price}
    visits = int(x.get("visits") or 1)
    m = re.search(r"курс\s*(\d+)", x["name"], flags=re.I)
    if visits <= 1 and m:
        visits = int(m.group(1))
    base = (x.get("desc") or "").strip()
    sv = by_name.get(_low(base)) or by_name.get(_low(base.split(",")[0]))
    if not base or not sv:
        base = re.sub(r"(?i)\bабонемент\b|курс\s*\d+", "", x["name"]).strip(" -·")
        if base.isupper():
            base = re.sub(r"(?i)\b(rf|ipl|led)\b", lambda m: m.group(1).upper(), base[:1].upper() + base[1:].lower())
        sv = sv or by_name.get(_low(base)) or _closest(base, by_name)
    one = float(sv.price) if sv and sv.price else 0.0
    full = one * visits
    price = float(x["price"])
    save = full - price if one and visits > 1 and full > price else 0.0
    return {**x, "visits": visits, "title": f"{base} — курс {visits} {_visits_word(visits)}" if visits > 1 else base,
            "per": round(price / visits) if visits > 1 else 0, "one": one if save else 0,
            "save": round(save), "pct": round(save / full * 100) if save else 0}


def text_lines(t: str | None) -> list[str]:
    return [ln.strip(" •-–") for ln in (t or "").splitlines() if ln.strip(" •-–")]


def loyalty(c: dict) -> dict:
    """Тексти програми лояльності для сторінки: постійні акції, правила сертифікатів і абонементів, загальне правило."""
    return {k: text_lines(c.get(k) if c.get(k) is not None else DEFAULT[k]) for k in ("promos", "cert_rules", "sub_rules", "loyalty_note")}


def offers(db: Session, c: dict | None = None) -> dict:
    from . import certificates as cert, gift_types as gt
    c = c or cfg(db)
    certs = [{"id": x["id"], "name": x["name"], "price": float(x.get("price") or x.get("nominal") or 0), "nominal": float(x.get("nominal") or 0),
              "days": int(x.get("days") or 0), "desc": (x.get("services") or "").strip()[:200]} for x in gt.active(db)
             if float(x.get("price") or 0) > 0] if c.get("certs_on") else []
    subs = [{"id": x["id"], "name": x["name"], "price": float(x.get("price") or 0), "visits": int(x.get("visits") or 1),
             "days": int(x.get("days") or 0), "desc": (x.get("services") or "").strip()[:200]} for x in cert.package_items(db)
            if float(x.get("price") or 0) > 0] if c.get("subs_on") else []
    by_name = {_low(sv.name): sv for sv in db.scalars(select(CbService)) if sv.price}
    subs = [sub_view(db, x, by_name) for x in subs]
    subs.sort(key=lambda x: (x["price"], x["name"]))
    groups: dict[str, list] = {}
    for x in subs:
        groups.setdefault(sub_group(x["name"] + " " + x["desc"]), []).append(x)
    order = {n: i for i, n in enumerate(SUB_GROUPS)}
    return {"certs": sorted(certs, key=lambda x: x["price"]), "subs": subs, "loyalty": loyalty(c),
            "sub_groups": [{"name": k, "items": v} for k, v in sorted(groups.items(), key=lambda kv: (order.get(kv[0], 100), kv[0]))]}


def order(db: Session, data: dict, base_url: str, c: dict | None = None) -> dict:
    """Заявка на сертифікат / абонемент: звернення + (якщо підключено платіжну систему) посилання на оплату."""
    from . import paylinks as pl, site_leads as sl
    c = c or cfg(db)
    phone = norm_phone(data.get("phone"))
    if not phone:
        raise BookingError("phone", "Перевірте номер телефону: +380 і 9 цифр.")
    name = " ".join(str(data.get("name") or "").split())[:120]
    kind = "sub" if str(data.get("kind")) == "sub" else "cert"
    of = offers(db, c)
    it = next((x for x in of["subs" if kind == "sub" else "certs"] if str(x["id"]) == str(data.get("id"))), None)
    if not it:
        raise BookingError("item", "Оберіть сертифікат або абонемент зі списку.")
    title = ("Абонемент" if kind == "sub" else "Подарунковий сертифікат") + f": {it['name']}"
    to = " ".join(str(data.get("to") or "").split())[:120]
    to_phone = norm_phone(data.get("to_phone")) if data.get("to_phone") else ""
    if kind == "cert":
        if len(to) < 2:
            raise BookingError("to", "Вкажіть імʼя отримувача сертифіката.")
        if not to_phone:
            raise BookingError("to_phone", "Перевірте телефон отримувача: +380 і 9 цифр.")
    d = {**{k: v for k, v in data.items() if k.startswith("utm_") or k in ("gclid", "cid", "page")},
         "name": name, "phone": phone, "interest": title, "formname": f"{SLUG}:{kind}",
         "comment": f"{title} · {it['price']:.0f} ₴" + (f" · для кого: {to}" if to else "") + (f", {to_phone}" if to_phone else "")
                    + (f" · {' '.join(str(data.get('note')).split())[:300]}" if data.get("note") else "")}
    if kind == "cert":
        purpose, basket = "Послуги краси", [{"name": "Послуги краси", "qty": 1, "total": it["price"]}]
    else:                                   # у кошику monobank: «Апаратний масаж STRATOSPHERE 50 хв» × 6
        base = re.sub(r"\s*—\s*курс.*$", "", it.get("title") or it["name"]).strip()
        purpose, basket = base, [{"name": base, "qty": int(it.get("visits") or 1), "total": it["price"]}]
    r = sl.ingest(db, d)
    out = {"ok": bool(r.get("ok")), "title": title, "price": it["price"], "appeal_id": r.get("appeal_id"), "lead": d, "res": r}
    try:
        if pl.public(db).get("ready"):
            pat = __import__("app.services.appeals", fromlist=["x"]).find_patient(db, phone)
            link = pl.create(db, it["price"], purpose, base_url, patient_id=pat.id if pat else None, basket=basket)
            link.info = {**(link.info or {}), "order": {"kind": kind, "id": it["id"], "name": name, "phone": phone, "to": to,
                                                       "to_phone": to_phone, "appeal_id": r.get("appeal_id"), "title": title}}
            db.commit()
            out["pay_url"] = link.url
    except Exception:  # noqa: BLE001 — оплата недоступна: лишається заявка, адміністратор звʼяжеться
        db.rollback()
    return out


# ---------------- масове ввімкнення послуг ----------------
def service_rows(db: Session, c: dict | None = None) -> list[dict]:
    """Для налаштувань: категорії з послугами, позначка онлайн і хто надає."""
    docs = online_doctors(db, c)
    hist = performers(db)
    names = {d.id: d.name for d in docs}
    cats: dict[str, list] = {}
    for sv in db.scalars(select(CbService).order_by(CbService.category, CbService.name)):
        if sv.active is False:
            continue
        dids = service_doctors(db, sv, docs, hist)
        cats.setdefault(sv.category or "Без категорії", []).append(
            {"id": sv.id, "name": sv.name, "price": sv.price or 0, "min": _mins(sv), "online": bool(sv.online),
             "docs": [names[i] for i in dids], "manual": bool(sv.doctor_ids)})
    return [{"name": k, "items": v, "on": sum(1 for x in v if x["online"]), "bookable": sum(1 for x in v if x["docs"] and x["price"])} for k, v in cats.items()]


def set_services(db: Session, on_ids: set[int], shown_ids: set[int]) -> int:
    """Позначити «онлайн» для показаних у формі послуг: увімкнено ті, що в on_ids."""
    n = 0
    for sv in db.scalars(select(CbService).where(CbService.id.in_(shown_ids or {0}))):
        want = sv.id in on_ids
        if bool(sv.online) != want:
            sv.online, n = want, n + 1
    db.commit()
    return n


def apply_cb_seed(db: Session, force: bool = False) -> dict:
    """Один раз: послуги й спеціалісти — як в онлайн-записі CleverBox (online_cb_seed.json, 16.11.2026, рішення власника).
    Послуги зі списку — «онлайн», решта — ні; «хто надає» — зі списку (лікар за імʼям); без лікаря — за історією записів.
    Спеціалісти, яких немає в онлайн-записі CleverBox, — вимкнені з онлайн-запису."""
    from pathlib import Path
    flag = "online_cb_seed_20261116"
    if db.get(SyncState, flag) and not force:
        return {"skipped": True}
    data = json.loads((Path(__file__).with_name("online_cb_seed.json")).read_text(encoding="utf-8"))["services"]
    have = {str(i) for (i,) in db.execute(select(CbService.id))}
    if not force and len(have & set(data)) < len(data) // 2:
        return {"skipped": True, "reason": "прайс CleverBox ще не завантажено"}       # інша база (порожня / тестова) — не чіпаємо
    docs = {" ".join((d.name or "").lower().split()): d for d in db.scalars(select(Doctor))}

    def doc(name: str):
        k = " ".join(name.lower().split())
        if k in docs:
            return docs[k]
        a = set(k.split())
        return next((d for n, d in docs.items() if a == set(n.split())), None)
    on = missing = 0
    used: set[int] = set()
    for sv in db.scalars(select(CbService)):
        names = data.get(str(sv.id))
        if names is None:
            sv.online = False
            continue
        sv.online = True
        on += 1
        ids = [d.id for d in (doc(n) for n in names) if d]
        sv.doctor_ids = ids or None
        used |= set(ids)
    missing = len(set(data) - {str(i) for (i,) in db.execute(select(CbService.id))})
    c = cfg(db)
    if used:
        c["doctors_off"] = sorted(d.id for d in _staff(db) if d.id not in used)
        st = db.get(SyncState, KEY) or SyncState(key=KEY)
        st.value, st.updated_at = json.dumps(c, ensure_ascii=False), datetime.utcnow()
        db.merge(st)
    db.merge(SyncState(key=flag, value=datetime.now().isoformat(timespec="minutes"), updated_at=datetime.utcnow()))
    db.commit()
    reset_cache()
    return {"online": on, "not_in_crm": missing, "doctors": sorted(used)}


def apply_guides_20261116(db: Session) -> dict:
    """Один раз (рішення власника 16.11.2026): гайди — три PDF (тарілка 360, молочні залози, родимки); старі чотири — вимкнено."""
    flag = "online_guides_20261116"
    if db.get(SyncState, flag):
        return {"skipped": True}
    c = cfg(db)
    c["guides"] = json.loads(json.dumps(GUIDES_DEFAULT))
    have = {q for q, _ in c.get("faq") or []}
    c["faq"] = list(c.get("faq") or []) + [x for x in FAQ_SEO if x[0] not in have]
    save_cfg(db, c)
    db.merge(SyncState(key=flag, value="1", updated_at=datetime.utcnow()))
    db.commit()
    return {"ok": True, "guides": len(c["guides"])}


def jsonld(site_c: dict, c: dict, url: str, prices: list[dict]) -> str:
    """Розмітка schema.org для сторінки запису: медичний заклад (адреса, графік, геопозиція) + FAQ."""
    clinic = {"@type": "MedicalClinic", "name": "MOLODIST", "url": site_c.get("main_url") or url, "telephone": site_c.get("phone"),
              "description": "Медичний простір естетики та профілактики в Києві: дерматокосметологія, ботулінотерапія, контурна пластика, "
                             "біоревіталізація, лазерна епіляція, IPL-терапія, RF-ліфтинг.",
              "address": {"@type": "PostalAddress", "streetAddress": "вул. Князя Романа Мстиславича, 28А", "addressLocality": "Київ",
                          "addressCountry": "UA"},
              "openingHoursSpecification": [{"@type": "OpeningHoursSpecification", "dayOfWeek": ["Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
                                             "opens": "10:00", "closes": "20:00"}],
              "potentialAction": {"@type": "ReserveAction", "target": url}}
    lo = min((g["lo"] for g in prices), default=0)
    if lo:
        clinic["priceRange"] = f"від {lo:,.0f} ₴".replace(",", " ")
    faq = {"@type": "FAQPage", "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                                              for q, a in (c.get("faq") or [])]}
    return json.dumps({"@context": "https://schema.org", "@graph": [clinic, faq]}, ensure_ascii=False)


def _patient_for(db: Session, name: str, phone: str, note: str):
    from . import appeals as ap
    pat = ap.find_patient(db, phone)
    if not pat:
        pat = Patient(full_name=name or phone, phone=phone, notes=note, source="Сайт / онлайн-покупка")
        db.add(pat)
        db.flush()
    return pat


def fulfil(db: Session, pl) -> int | None:
    """Оплачена онлайн-покупка → продаж у CRM без участі адміністратора: сертифікат — на отримувача (його номер),
    абонемент — на номер покупця; чек «оплачено» посиланням, фіскальний чек — від monobank (CRM сама не фіскалізує)."""
    from . import sales as sl, paylinks as pls
    o = (pl.info or {}).get("order") or {}
    if not o or pl.sale_id:
        return pl.sale_id
    kind = o.get("kind")
    buyer = f"{o.get('name') or ''} {o.get('phone') or ''}".strip()
    if kind == "cert":
        pat = _patient_for(db, o.get("to") or "", o.get("to_phone") or o.get("phone"), f"Картку створено: подарунковий сертифікат від {buyer}")
    else:
        pat = _patient_for(db, o.get("name") or "", o.get("phone"), "Картку створено: онлайн-покупка абонемента на сайті")
    acc = pls._acc_of(pls._raw(db), pl) or {}
    paid = float(pl.paid_amount or pl.amount)
    sale = sl.create(db, {"patient_id": pat.id, "no_discounts": True, "cert_for": o.get("to") if kind == "cert" else None,
                          "items": [{"kind": "certificate" if kind == "cert" else "subscription", "ref_id": o.get("id"),
                                     "name": o.get("title") or pl.purpose, "qty": 1, "price": paid}],
                          "payments": [{"method": sl.paylink_method(acc.get("name") or "monobank"), "amount": paid}],
                          "comment": f"Онлайн-покупка на сайті, посилання №{pl.id}" + (f" · купив(ла): {buyer}" if kind == "cert" else "")})
    pl.sale_id = sale.id
    pl.patient_id = pl.patient_id or pat.id
    pl.info = {**(pl.info or {}), "order": {**o, "sale_id": sale.id, "patient_id": pat.id}}
    db.flush()
    return sale.id


def attach_fiscal(db: Session, http=None) -> int:
    """Фіскальні чеки monobank для онлайн-покупок за 3 дні — у чек CRM (щоб не фіскалізувати вдруге)."""
    from ..models import PayLink, Sale
    from . import paylinks as pls
    n = 0
    since = datetime.now() - timedelta(days=3)
    for p in db.scalars(select(PayLink).where(PayLink.status == "paid", PayLink.provider == "mono", PayLink.sale_id.isnot(None),
                                              PayLink.paid_at >= since)):
        if not ((p.info or {}).get("order") or (p.info or {}).get("shop_order")):
            continue
        s = db.get(Sale, p.sale_id)
        if s and not (s.fiscal or {}).get("fiscal_code"):
            try:
                pls.mono_fiscal(db, p, http)
                n += 1
            except Exception:  # noqa: BLE001
                db.rollback()
    return n


def apply_care_bot_20261116(db: Session) -> dict:
    """Один раз: посилання на Бот турботи у «Звʼязку» (від власника 16.11.2026), якщо поле ще порожнє."""
    flag = "online_care_bot_20261116"
    if db.get(SyncState, flag):
        return {"skipped": True}
    c = cfg(db)
    if not c.get("care_bot"):
        c["care_bot"] = CARE_BOT
        save_cfg(db, c)
    db.merge(SyncState(key=flag, value="1", updated_at=datetime.utcnow()))
    db.commit()
    return {"ok": True}
