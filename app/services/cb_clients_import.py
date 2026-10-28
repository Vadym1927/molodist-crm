"""Імпорт вивантаження CleverBox «Клієнти» (Excel, Clients_*.xls) у картки пацієнтів.

Зіставлення: id CleverBox → телефон → новий пацієнт. Показники CleverBox (візити, суми, баланс, бонуси, статус,
замітка, напрямки…) оновлюються завжди; контактні поля, які могли правити в CRM (телефон, email, стать, адреса,
джерело, канал, ДН), — лише якщо в CRM порожньо. Групи — об'єднуються.
«Медичний анамнез» пишеться в окреме поле, яке бачать лише ролі з доступом medical і яке нікуди не відправляється."""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Patient

DIRECTION_COLS = ("ЛЕ", "Дерматохірургія", "Косметолог", "RESET", "Стратосфера", "beauty plan", "Трихолог",
                  "лікування дерматологічне")
CHANNELS = {"вайбер": "Viber", "viber": "Viber", "телеграм": "Telegram", "telegram": "Telegram",
            "інстаграм": "Instagram", "instagram": "Instagram", "дзвінок": "Телефон", "телефон": "Телефон",
            "смс": "SMS", "sms": "SMS", "email": "Email", "пошта": "Email"}
CLINIC_EMAILS = ("molodost", "molodist")


def is_clients_export(header: list) -> bool:
    h = {str(x or "").strip().lower() for x in header}
    return "id" in h and ("ім'я клієнта" in h or "імʼя клієнта" in h) and "завершених візитів" in h


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    t = re.sub(r"\s+", " ", str(v)).strip()
    return None if t in ("", "-", "—", "0", "0.0", "0.00") else t


def _n(v) -> float | None:
    t = _s(v)
    try:
        return float(t.replace(" ", "").replace(",", ".")) if t else None
    except ValueError:
        return None


def _d(v) -> datetime | None:
    t = _s(v)
    if not t:
        return None
    for cut, fmt in ((16, "%d.%m.%Y %H:%M"), (10, "%d.%m.%Y"), (19, "%Y-%m-%d %H:%M:%S"), (10, "%Y-%m-%d")):
        try:
            return datetime.strptime(t[:cut], fmt)
        except ValueError:
            continue
    return None


def _digits(p) -> str:
    return re.sub(r"\D", "", str(p or ""))[-9:]


def import_clients(db: Session, rows: list[list], header: list) -> dict:
    col = {str(h or "").strip(): i for i, h in enumerate(header)}

    def g(row, name):
        i = col.get(name)
        if i is None:
            i = col.get(name.replace("'", "ʼ"))
        return row[i] if i is not None and i < len(row) else None

    patients = db.scalars(select(Patient)).all()
    by_cb = {p.cleverbox_id: p for p in patients if p.cleverbox_id}
    by_phone = {}
    for p in patients:
        if p.phone and len(_digits(p.phone)) == 9:
            by_phone.setdefault(_digits(p.phone), p)
    rep = {"type": "CleverBox: клієнти", "rows": 0, "updated": 0, "patients_created": 0, "linked_by_phone": 0,
           "skipped": 0, "errors": []}
    now = datetime.now()
    for row in rows:
        cid = _n(g(row, "id"))
        name = _s(g(row, "Ім'я клієнта"))
        if not cid or not name:
            rep["skipped"] += 1
            continue
        rep["rows"] += 1
        cid = int(cid)
        phone = _s(g(row, "Телефон"))
        p = by_cb.get(cid)
        if not p and phone and len(_digits(phone)) == 9:
            p = by_phone.get(_digits(phone))
            if p and p.cleverbox_id and p.cleverbox_id != cid:
                p = None                                  # той самий номер у іншого клієнта CleverBox (родина)
            if p:
                p.cleverbox_id = cid
                rep["linked_by_phone"] += 1
        if not p:
            p = Patient(full_name=name, phone=phone, cleverbox_id=cid)
            db.add(p)
            rep["patients_created"] += 1
        else:
            rep["updated"] += 1
        by_cb[cid] = p
        # контакти — лише якщо порожньо в CRM
        if not p.phone and phone:
            p.phone = phone[:64]
        em = _s(g(row, "Email")) or _s(g(row, "e-mail"))
        if em and "@" in em and not any(x in em.lower() for x in CLINIC_EMAILS) and not p.email:
            p.email = em[:200]
        bd = _d(g(row, "Дата народження")) or _d(g(row, "День народження"))
        if bd and not p.birthday and 1920 < bd.year <= now.year:
            p.birthday = bd.date()
        sex = (_s(g(row, "Стать")) or "").lower()
        if not p.gender and sex in ("жінка", "чоловік"):
            p.gender = "жіноча" if sex == "жінка" else "чоловіча"
        if not p.address and _s(g(row, "Адреса проживання")):
            p.address = _s(g(row, "Адреса проживання"))[:300]
        if not p.source and _s(g(row, "Джерело звернення")):
            p.source = _s(g(row, "Джерело звернення"))[:100]
        ch = (_s(g(row, "Основний канал комунікації")) or "").lower()
        if ch and not p.communication_channel:
            p.communication_channel = CHANNELS.get(ch, ch.capitalize())[:30]
        # показники CleverBox — завжди
        reg = _d(g(row, "Дата створення клієнта"))
        if reg and reg.year > 2000:
            p.cb_registered_at = reg
        v = _n(g(row, "Завершених візитів"))
        p.cb_visits = int(v) if v is not None else (p.cb_visits or 0)
        p.cb_last_visit = _d(g(row, "Останній візит")) or p.cb_last_visit
        p.cb_first_visit = _d(g(row, "Перший візит")) or p.cb_first_visit
        for attr, c in (("cb_average", "Середній чек"), ("cb_total", "Заплачено грошей"),
                        ("cb_balance", "Баланс"), ("cb_bonus", "Бонус")):
            val = _n(g(row, c))
            setattr(p, attr, val if val is not None else (0.0 if attr in ("cb_balance", "cb_bonus") else getattr(p, attr)))
        st = _s(g(row, "Статус"))
        p.cb_status = st[:100] if st else p.cb_status
        grp = [x.strip() for x in (_s(g(row, "Групи")) or "").split(",") if x.strip()]
        if grp:
            p.client_groups = list(dict.fromkeys((p.client_groups or []) + grp))[:30]
        p.cb_note = _s(g(row, "Замітка"))
        p.cb_anamnesis = _s(g(row, "Медичний анамнез"))
        p.instagram = (_s(g(row, "Інстаграм")) or "")[:100] or None
        p.viber = (_s(g(row, "Телефон вайбер")) or "")[:64] or None
        p.telegram_contact = (_s(g(row, "Телефон телеграм")) or "")[:100] or None
        p.preferred_doctor = (_s(g(row, "Надає перевагу спеціалісту")) or "")[:200] or None
        p.drink = (_s(g(row, "Напій якому надає перевагу")) or "")[:200] or None
        p.delivery = (_s(g(row, "Дані для відправки НП")) or "")[:300] or None
        p.no_repeat_call = bool(_s(g(row, "Не дзвонити при повторному візиті"))) or None
        dirs = [c for c in DIRECTION_COLS if (_s(g(row, c)) or "").lower() in ("так", "1", "yes", "+")]
        p.directions = dirs or None
        fam = _s(g(row, "Сімейний стан/ діти"))
        extra = dict(p.cb_extra or {})
        if fam:
            extra["Сімейний стан / діти"] = fam
        p.cb_extra = extra or None
        p.cb_synced_at = now
    db.commit()
    return rep
