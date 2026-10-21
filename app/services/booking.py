"""Запис пацієнта на процедуру з бюті-плану прямо в CleverBox (/slots/get → /slots/save).

Логіка: позиція плану → послуга CleverBox (ручне зіставлення або автозбіг назви) → вільні слоти на дату
→ спеціаліст (першим — лікар плану) → запис у CleverBox → локальний «запис» у CRM одразу,
закриття задачі «записати», журнал дій.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Alias, CbService, Doctor, PlanItem, Patient, Visit, Task, SyncState
from ..parsers.observ import norm
from .kpi import name_match


class BookingError(RuntimeError):
    pass


@dataclass
class Slot:
    time: str
    masters: list[tuple[int, str]]      # (id, імʼя); перший — лікар плану, якщо він вільний


def resolve_service(db: Session, item: PlanItem) -> CbService | None:
    al = db.scalar(select(Alias).where(Alias.kind == item.kind, Alias.plan_name_norm == norm(item.name)))
    if al and al.cb_ids:
        return db.get(CbService, al.cb_ids[0])
    best, score = None, 0.0
    for s in db.scalars(select(CbService)):
        sc = name_match(item.name, s.name)
        if sc > score:
            best, score = s, sc
    return best if score >= 0.6 else None


def salon_id(db: Session, cb) -> int:
    if settings.cleverbox_filial_id:
        return int(settings.cleverbox_filial_id)
    st = db.get(SyncState, "filial_id")
    if st and st.value:
        return int(st.value)
    fil = [f for f in cb.filials() if not int(f.get("is_hidden") or 0)]
    if not fil:
        raise BookingError("У CleverBox не знайдено активної філії")
    db.merge(SyncState(key="filial_id", value=str(fil[0]["id"])))
    db.commit()
    return int(fil[0]["id"])


def get_slots(db: Session, item: PlanItem, day: date, cb=None) -> tuple[CbService, list[Slot]]:
    from ..integrations.cleverbox import CleverBox
    svc = resolve_service(db, item)
    if not svc:
        raise BookingError(f"«{item.name}» не зіставлено з послугою CleverBox — зробіть це в розділі «Зіставлення»")
    cb = cb or CleverBox()
    res = cb.call("slots/get", {"date": day.strftime("%d.%m.%Y"), "salon_id": str(salon_id(db, cb)),
                                "services": [{"id": str(svc.id), **({"long": svc.time_min} if svc.time_min else {})}]})
    names = {int(m["id"]): m.get("name", "") for m in res.get("masters") or []}
    plan_doc = item.plan.doctor.cleverbox_id if item.plan.doctor and item.plan.doctor.cleverbox_id else None
    slots = []
    for t in (res.get("slots") or {}).get(day.strftime("%d.%m.%Y"), []):
        if not t.get("on"):
            continue
        ids = [int(x) for x in t.get("masters") or []]
        ids.sort(key=lambda i: i != plan_doc)
        if ids:
            slots.append(Slot(time=t["time"], masters=[(i, names.get(i, f"#{i}")) for i in ids]))
    return svc, slots


def book(db: Session, user, item: PlanItem, day: date, time: str, master_id: int, comment: str = "", cb=None) -> Visit:
    from ..integrations.cleverbox import CleverBox
    from ..security import audit
    svc = resolve_service(db, item)
    if not svc:
        raise BookingError("Послугу не зіставлено з CleverBox")
    pat: Patient = item.plan.patient
    cb = cb or CleverBox()
    body = {"date": day.strftime("%d.%m.%Y"), "salon_id": salon_id(db, cb),
            "comment": (f"Бюті-план MOLODIST CRM: {item.name}. " + comment).strip(),
            "services": [{"id": svc.id, "master_id": master_id, "time": time, "long": svc.time_min or 60}]}
    if pat.cleverbox_id:
        body["client_id"] = pat.cleverbox_id
    elif pat.phone:
        body.update(client_id=0, phone=pat.phone, name=pat.full_name)
    else:
        raise BookingError("У пацієнта немає ні ID CleverBox, ні телефону — додайте телефон у картці")
    res = cb.call("slots/save", body)
    rec = int(res.get("record_id") or (res.get("data") or {}).get("id") or 0)
    master = (res.get("data") or {}).get("master") or (db.scalar(select(Doctor).where(Doctor.cleverbox_id == master_id)) or Doctor(name="")).name
    v = Visit(source="cb_visit", external_id=f"{rec}-{svc.id}", patient_id=pat.id, service_id=svc.id,
              service_name=svc.name, master_name=master or None, status="booked", status_label="Записано з CRM",
              starts_at=datetime.combine(day, datetime.strptime(time, "%H:%M").time()), plan_item_id=item.id,
              amount=item.unit_price)
    db.add(v)
    for t in db.scalars(select(Task).where(Task.status == "open", Task.plan_id == item.plan_id,
                                           Task.kind == "not_booked")):
        t.status, t.done_at = "done", datetime.utcnow()
        t.result = f"Записано на {day:%d.%m} {time} ({user.name})"
    db.commit()
    audit(db, "booking.create", "patient", pat.id, user=user, item=item.name, date=str(day), time=time,
          master_id=master_id, record_id=rec)
    return v
