"""Запис у CRM: створення / зміна, перевірка накладок (спеціаліст, кабінет, апарат, графік), статуси, дубль у CleverBox."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, CbService, Doctor, Patient, StaffShift, Visit

STATUSES = {"booked": "Записаний", "confirmed": "Підтверджено", "arrived": "Прийшов", "done": "Візит завершено",
            "no_show": "Не прийшов", "cancelled": "Скасовано"}
ACTIVE = ("booked", "confirmed", "arrived", "done")
COLORS = {"booked": "#5c4b82", "confirmed": "#2f7a8a", "arrived": "#b07a3e", "done": "#8a8496", "no_show": "#b23a32", "cancelled": "#c3bfcc"}


def services_from_ids(db: Session, ids: list[int], prices: dict | None = None, minutes: dict | None = None) -> list[dict]:
    out = []
    for i in ids:
        s = db.get(CbService, i)
        if not s:
            continue
        out.append({"id": s.id, "name": s.name, "category": s.category,
                    "price": float((prices or {}).get(i, s.price or 0) or 0),
                    "minutes": int((minutes or {}).get(i, s.time_min or 30) or 30)})
    return out


def overlaps(a1: datetime, a2: datetime, b1: datetime, b2: datetime) -> bool:
    return a1 < b2 and b1 < a2


def conflicts(db: Session, doctor_id: int | None, start: datetime, end: datetime, place: str | None = None,
              resource: str | None = None, exclude_id: int | None = None, patient_id: int | None = None) -> list[str]:
    """Попередження: зайнятий спеціаліст / кабінет / апарат, поза зміною, пацієнт уже записаний на цей час."""
    warn = []
    day0, day1 = datetime.combine(start.date(), datetime.min.time()), datetime.combine(start.date(), datetime.max.time())
    same_day = [a for a in db.scalars(select(Appointment).where(Appointment.start >= day0, Appointment.start <= day1,
                                                                 Appointment.status.in_(ACTIVE)))
                if a.id != exclude_id and overlaps(start, end, a.start, a.end)]
    d = db.get(Doctor, doctor_id) if doctor_id else None
    for a in same_day:
        who = a.patient.full_name if a.patient else "пацієнт"
        if doctor_id and a.doctor_id == doctor_id:
            warn.append(f"{d.name if d else 'Спеціаліст'} уже має запис {a.start:%H:%M}–{a.end:%H:%M} ({who})")
        if place and a.place == place:
            warn.append(f"Кабінет «{place}» зайнятий {a.start:%H:%M}–{a.end:%H:%M} ({who})")
        if resource and a.resource == resource:
            warn.append(f"Апарат «{resource}» зайнятий {a.start:%H:%M}–{a.end:%H:%M} ({who})")
        if patient_id and a.patient_id == patient_id:
            warn.append(f"Пацієнт уже записаний на {a.start:%H:%M}–{a.end:%H:%M}")
    if d:                                           # записи CleverBox цього спеціаліста
        from ..integrations.cleverbox import same_person
        for v in db.scalars(select(Visit).where(Visit.starts_at >= day0, Visit.starts_at <= day1, Visit.status == "booked")):
            v_end = v.ends_at or v.starts_at + timedelta(minutes=60)
            if same_person(d.name, v.master_name) and overlaps(start, end, v.starts_at, v_end) and \
                    not any(x.cb_record_id and v.external_id.startswith(f"{x.cb_record_id}-") for x in same_day):
                warn.append(f"У CleverBox у {d.name} запис {v.starts_at:%H:%M} ({v.patient.full_name if v.patient else ''})")
        sh = db.scalar(select(StaffShift).where(StaffShift.doctor_id == d.id, StaffShift.day == start.date()))
        if sh:
            if start.strftime("%H:%M") < sh.start or end.strftime("%H:%M") > sh.end:
                warn.append(f"Поза зміною {d.name}: {sh.start}–{sh.end}")
        elif db.scalar(select(StaffShift.id).where(StaffShift.doctor_id == d.id).limit(1)):
            warn.append(f"{d.name} цього дня не в графіку")
    return list(dict.fromkeys(warn))


def save(db: Session, data: dict, user_id: int | None = None, appt: Appointment | None = None) -> Appointment:
    a = appt or Appointment(created_by=user_id, source=data.get("source") or "crm")
    a.patient_id = data["patient_id"]
    a.doctor_id = data.get("doctor_id")
    a.services = data.get("services") or []
    a.start = data["start"]
    mins = int(data.get("minutes") or sum(int(s.get("minutes") or 0) for s in a.services) or 30)
    a.end = a.start + timedelta(minutes=max(5, mins))
    a.place, a.resource = data.get("place") or None, data.get("resource") or None
    a.comment = data.get("comment") or None
    if data.get("appeal_id"):
        a.appeal_id = data["appeal_id"]
    if appt:
        a.updated_at = datetime.utcnow()
    db.add(a)
    db.commit()
    return a


def set_status(db: Session, a: Appointment, status: str, reason: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError("Невідомий статус")
    a.status, a.updated_at = status, datetime.utcnow()
    a.cancel_reason = reason if status in ("cancelled", "no_show") else None
    db.commit()


def mirror_to_cleverbox(db: Session, a: Appointment, cb=None) -> int:
    """Створює такий самий запис у CleverBox (поки CleverBox у роботі). Повертає record_id."""
    from ..integrations.cleverbox import CleverBox
    from .booking import BookingError, salon_id
    d = db.get(Doctor, a.doctor_id) if a.doctor_id else None
    if not d or not d.cleverbox_id:
        raise BookingError("У спеціаліста немає ID CleverBox — запис лише в CRM")
    if not a.services:
        raise BookingError("Без послуг запис у CleverBox не створюється")
    p: Patient = a.patient
    cb = cb or CleverBox()
    t, svc = a.start, []
    for s in a.services:
        svc.append({"id": s["id"], "master_id": d.cleverbox_id, "time": t.strftime("%H:%M"), "long": int(s.get("minutes") or 30)})
        t += timedelta(minutes=int(s.get("minutes") or 30))
    body = {"date": a.start.strftime("%d.%m.%Y"), "salon_id": salon_id(db, cb), "services": svc,
            "comment": ("MOLODIST CRM. " + (a.comment or "")).strip()}
    if p.cleverbox_id:
        body["client_id"] = p.cleverbox_id
    elif p.phone:
        body.update(client_id=0, phone=p.phone, name=p.full_name)
    else:
        raise BookingError("У пацієнта немає телефону — додайте його, щоб записати в CleverBox")
    res = cb.call("slots/save", body)
    rec = int(res.get("record_id") or (res.get("data") or {}).get("id") or 0)
    a.cb_record_id = rec or None
    db.commit()
    return rec


def day_list(db: Session, start: date, days: int = 1) -> list[Appointment]:
    a, b = datetime.combine(start, datetime.min.time()), datetime.combine(start + timedelta(days=days), datetime.min.time())
    return db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start < b).order_by(Appointment.start)).all()


# ---------------- візити CleverBox → записи CRM (автономний режим) ----------------
def _rid(v) -> int | None:
    try:
        return int(str(v.external_id).split("-")[0])
    except (TypeError, ValueError):
        return None


def adopt_cb(db: Session, rid: int, user_id: int | None = None) -> Appointment | None:
    """Візит CleverBox (№ запису rid) стає записом CRM: той самий пацієнт, лікар, час і послуги — далі ним керують у CRM
    (перенести, змінити, статус, витратні, розрахунок). Рядки CleverBox не змінюються; календар показує запис CRM замість них."""
    from ..models import CbService, Visit
    from .specialists import DoctorIndex, all_doctors
    have = db.scalar(select(Appointment).where(Appointment.cb_record_id == rid))
    if have:
        return have
    vs = [v for v in db.scalars(select(Visit).where(Visit.source == "cb_visit", Visit.external_id.like(f"{rid}-%")).order_by(Visit.id))
          if _rid(v) == rid]
    if not vs:
        return None
    idx = DoctorIndex(all_doctors(db))
    doc = next((d for d in (idx.find(v.master_name) for v in vs) if d), None)
    svcs, start = [], min(v.starts_at for v in vs)
    from .techcards import resolve_service
    for v in vs:
        sv = resolve_service(db, v.service_id, v.service_name)
        mins = int((sv.duration or sv.time_min) if sv and (sv.duration or sv.time_min) else 0) or \
            (int((v.ends_at - v.starts_at).total_seconds() // 60) if v.ends_at and v.ends_at > v.starts_at else 60)
        price = float(sv.price) if sv and sv.price else float(v.amount or 0)
        svcs.append({"id": sv.id if sv else v.service_id, "name": v.service_name or (sv.name if sv else "Послуга"), "price": price, "minutes": mins,
                     "category": sv.category if sv else None})
    ends = [v.ends_at for v in vs if v.ends_at and v.ends_at > start]
    end = max(ends) if ends else start + timedelta(minutes=sum(s_["minutes"] for s_ in svcs))
    a = Appointment(patient_id=vs[0].patient_id, doctor_id=doc.id if doc else None, start=start, end=end, services=svcs,
                    status="arrived" if all(v.status == "attended" for v in vs) else "booked", source="cleverbox",
                    cb_record_id=rid, created_by=user_id,
                    comment=f"Запис CleverBox №{rid}" + (f" · лікар у CleverBox: {vs[0].master_name}" if vs[0].master_name and not doc else ""))
    db.add(a)
    db.flush()
    try:
        from . import techcards
        techcards.seed(db, a)
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return a


def adopt_all_cb(db: Session, since: datetime | None = None) -> int:
    """Усі майбутні (з дати переходу) записи CleverBox — у записи CRM. Повторний запуск дублів не створює."""
    from ..models import Visit
    since = since or datetime.combine(date.today(), datetime.min.time())
    rids = {_rid(v) for v in db.scalars(select(Visit).where(Visit.source == "cb_visit", Visit.status.in_(("booked", "attended")),
                                                           Visit.starts_at >= since))}
    rids.discard(None)
    done = {x for (x,) in db.execute(select(Appointment.cb_record_id).where(Appointment.cb_record_id.in_(list(rids))))} if rids else set()
    n = 0
    for rid in sorted(rids - done):
        if adopt_cb(db, rid):
            n += 1
    seed_open(db, since)
    return n


def seed_open(db: Session, since: datetime) -> int:
    """Витратні з техкарт для всіх незакритих записів CRM з дати (ті, що ще без матеріалів, або послугу змінили)."""
    from . import techcards
    n = 0
    for a in db.scalars(select(Appointment).where(Appointment.start >= since, Appointment.status.notin_(("cancelled", "no_show")))):
        try:
            n += int(bool(techcards.seed(db, a)))
        except Exception:  # noqa: BLE001
            db.rollback()
    return n
