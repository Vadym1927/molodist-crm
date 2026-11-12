"""Онлайн-запис на сайті клініки (сторінка /zapys): послуга → спеціаліст → день → час → контакти.

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
SLUG = "zapys"
GUIDES_DEFAULT = [
    {"title": "Перший візит у MOLODIST", "tag": "Знайомство", "on": True,
     "text": "Прийдіть за 5–10 хвилин до початку — адміністратор зустріне вас і допоможе заповнити анкету.\n"
             "Візьміть із собою результати аналізів і список засобів домашнього догляду, якщо вони є.\n"
             "Розкажіть лікарю про алергії, хронічні стани й препарати, які приймаєте, — це впливає на вибір методик.\n"
             "Консультація починається з огляду й діагностики: лікар пояснить варіанти й складе план, а рішення завжди за вами."},
    {"title": "Підготовка до лазерної епіляції", "tag": "Лазер", "on": True,
     "text": "За 2 тижні до процедури не засмагайте й не відвідуйте солярій.\n"
             "За 4 тижні не видаляйте волосся воском, шугарингом чи епілятором — лише гоління.\n"
             "За добу до візиту поголіть зону процедури.\n"
             "У день процедури не наносьте на шкіру креми, дезодоранти й олії.\n"
             "Попередьте лікаря про прийом ліків і загострення шкірних станів — процедуру, можливо, варто перенести."},
    {"title": "Після інʼєкційних процедур", "tag": "Інʼєкції", "on": True,
     "text": "У перші години не торкайтеся зони процедури й не наносьте декоративну косметику.\n"
             "1–2 тижні уникайте сауни, лазні, солярію та активної засмаги; кілька днів — інтенсивних тренувань.\n"
             "Легкий набряк чи синець у місці інʼєкції — можлива реакція, яка зазвичай минає за кілька днів.\n"
             "Дотримуйтеся індивідуальних рекомендацій лікаря — вони мають пріоритет над загальними.\n"
             "Якщо щось турбує — напишіть або зателефонуйте нам, ми на звʼязку."},
    {"title": "Домашній догляд між візитами", "tag": "Догляд", "on": True,
     "text": "Базовий догляд — це очищення, зволоження й сонцезахист щодня, незалежно від сезону.\n"
             "Активні засоби (кислоти, ретиноїди) підбирає лікар — не поєднуйте їх самостійно.\n"
             "SPF наносьте вранці як останній етап догляду й оновлюйте протягом дня на відкритому сонці.\n"
             "Засоби, призначені лікарем, можна придбати в нашому магазині косметики з доставкою."},
]
FAQ_DEFAULT = [
    ["Чи потрібна передоплата?", "Ні, запис безкоштовний. Оплата — після візиту: карткою або готівкою."],
    ["Як перенести або скасувати запис?", "За посиланням «Мій запис», яке ви отримаєте після запису, або зателефонуйте чи напишіть нам. Будь ласка, попереджайте щонайменше за 3 години."],
    ["Не знаю, яку процедуру обрати. Що робити?", "Запишіться на консультацію лікаря — він огляне шкіру, пояснить варіанти й складе план. Або залиште номер, і адміністратор допоможе з вибором."],
    ["Чи підтверджують запис?", "Так, адміністратор звʼяжеться з вами напередодні візиту, щоб підтвердити час."],
]
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
}
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]
MON = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]


class BookingError(ValueError):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


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
    for k, n in (("shop_url", 300), ("shop_text", 300), ("note", 300), ("hero_img", 120)):
        c[k] = str(f.get(k) or "").strip()[:n]
    on_ids = {int(x) for x in f.getlist("doctor") if str(x).isdigit()}
    c["doctors_off"] = sorted(d.id for d in _staff(db) if d.id not in on_ids)
    c["faq"] = [[a.strip()[:200], b.strip()[:800]] for a, _, b in (ln.partition("|") for ln in str(f.get("faq") or "").splitlines()) if a.strip() and b.strip()][:20]
    guides = []
    for i in range(20):
        t = str(f.get(f"g_title_{i}") or "").strip()
        if not t:
            continue
        guides.append({"title": t[:120], "tag": str(f.get(f"g_tag_{i}") or "").strip()[:40], "on": bool(f.get(f"g_on_{i}")),
                       "text": str(f.get(f"g_text_{i}") or "").strip()[:4000], "url": str(f.get(f"g_url_{i}") or "").strip()[:500]})
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
        if sv.active is False or not sv.price:
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
        cats.setdefault(sv.category or "Інші послуги", []).append(
            {"id": sv.id, "name": (sv.online_name or sv.name).strip(), "price": float(sv.price), "min": _mins(sv),
             "desc": (sv.description or "").strip()[:600], "docs": dids, "dp": dp})
    return {"cats": [{"name": k, "items": v} for k, v in cats.items()], "doctor_ids": sorted(used)}


def doctor_cards(db: Session, ids: list[int] | None = None, base: str = "/_site", c: dict | None = None) -> list[dict]:
    """Короткі картки лікарів: текст сайту (site_pages.DOCTORS) або посада й біо з картки спеціаліста CRM."""
    from . import site_pages as sp
    out = []
    for d in online_doctors(db, c):
        if ids is not None and d.id not in ids:
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


def availability(db: Session, service_id: int, doctor_id: int | None, d_from: date | None = None, d_to: date | None = None,
                 c: dict | None = None, now: datetime | None = None) -> dict[date, list[dict]]:
    """День → [{t: «10:30», docs: [id, …]}] — вільні початки для послуги (усі її спеціалісти або один)."""
    c = c or cfg(db)
    now = now or now_local()
    sv = db.get(CbService, service_id)
    if not sv or not sv.online or sv.active is False:
        return {}
    docs = online_doctors(db, c)
    dids = service_doctors(db, sv, docs)
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
    dur, step = timedelta(minutes=_mins(sv)), timedelta(minutes=int(c["step"]))
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
        sid, did = int(data.get("service") or 0), int(data.get("doctor") or 0)
        day = date.fromisoformat(str(data.get("day") or ""))
        tm = datetime.strptime(str(data.get("time") or ""), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise BookingError("slot", "Оберіть послугу, день і час.") from None
    sv = db.get(CbService, sid)
    slots = availability(db, sid, did or None, day, day, c, now).get(day, [])
    slot = next((s for s in slots if s["t"] == tm), None)
    if not sv or not slot:
        raise BookingError("taken", "На жаль, цей час щойно зайняли. Оберіть, будь ласка, інший.")
    pat = ap.find_patient(db, phone)
    if pat:
        n_act = sum(1 for a in db.scalars(select(Appointment).where(Appointment.patient_id == pat.id, Appointment.source == "online",
                                                                    Appointment.start >= now, Appointment.status.notin_(("cancelled", "no_show")))))
        if n_act >= int(c["max_active"]):
            raise BookingError("limit", "У вас уже є активні онлайн-записи. Щоб додати ще один, зателефонуйте або напишіть нам.")
        start = _hm(day, tm)
        if any(a.start < start + timedelta(minutes=_mins(sv)) and start < a.end for a in db.scalars(
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
    price = dp.get(str(sv.id), dp.get(sv.id))
    sh = db.scalar(select(StaffShift).where(StaffShift.doctor_id == doc_id, StaffShift.day == day))
    comment = " ".join(str(data.get("comment") or "").split())[:500]
    a = aps.save(db, {"patient_id": pat.id, "doctor_id": doc_id, "start": _hm(day, tm), "source": "online",
                      "services": [{"id": sv.id, "name": sv.name, "price": float(price if price not in (None, "") else (sv.price or 0)),
                                    "minutes": _mins(sv), "category": sv.category}],
                      "place": sh.place if sh else None, "booking_source": "Сайт / онлайн-запис",
                      "comment": ("Онлайн-запис із сайту" + (f" · {name}" if name and name.lower() not in (pat.full_name or "").lower() else "")
                                  + (f". Коментар пацієнта: {comment}" if comment else ""))})
    a.online_token = _token(db)
    db.commit()
    reset_cache()
    return a


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
    svc = (a.services or [{}])[0]
    return {"token": a.online_token, "when": f"{day_label(a.start.date())}, {a.start:%H:%M}", "date": a.start.strftime("%d.%m.%Y"),
            "time": a.start.strftime("%H:%M"), "end": a.end.strftime("%H:%M"), "service": svc.get("name") or "", "price": svc.get("price") or 0,
            "doctor": (card or {}).get("name") or (d.name if d else ""), "status": a.status, "can_cancel": can_cancel(a, c),
            "past": a.start < now_local()}


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
    svc = (a.services or [{}])[0]
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
    svc = (a.services or [{}])[0]
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
def offers(db: Session, c: dict | None = None) -> dict:
    from . import certificates as cert, gift_types as gt
    c = c or cfg(db)
    certs = [{"id": x["id"], "name": x["name"], "price": float(x.get("price") or x.get("nominal") or 0), "nominal": float(x.get("nominal") or 0),
              "days": int(x.get("days") or 0), "desc": (x.get("services") or "").strip()[:200]} for x in gt.active(db)
             if float(x.get("price") or 0) > 0] if c.get("certs_on") else []
    subs = [{"id": x["id"], "name": x["name"], "price": float(x.get("price") or 0), "visits": int(x.get("visits") or 1),
             "days": int(x.get("days") or 0), "desc": (x.get("services") or "").strip()[:200]} for x in cert.package_items(db)
            if float(x.get("price") or 0) > 0] if c.get("subs_on") else []
    return {"certs": sorted(certs, key=lambda x: x["price"]), "subs": sorted(subs, key=lambda x: x["price"])}


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
    d = {**{k: v for k, v in data.items() if k.startswith("utm_") or k in ("gclid", "cid", "page")},
         "name": name, "phone": phone, "interest": title, "formname": f"{SLUG}:{kind}",
         "comment": f"{title} · {it['price']:.0f} ₴" + (f" · для кого: {to}" if to else "")}
    r = sl.ingest(db, d)
    out = {"ok": bool(r.get("ok")), "title": title, "price": it["price"], "appeal_id": r.get("appeal_id"), "lead": d, "res": r}
    try:
        if pl.public(db).get("ready"):
            pat = __import__("app.services.appeals", fromlist=["x"]).find_patient(db, phone)
            link = pl.create(db, it["price"], f"{title} (MOLODIST)", base_url, patient_id=pat.id if pat else None)
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
