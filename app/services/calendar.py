"""Розділ «Календар»: записи CleverBox + очікувані процедури за бюті-планами + дедлайни задач."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Visit, Task, User, Patient
from ..parsers.observ import norm
from .kpi import all_statuses
from .tasks import visible_query


@dataclass
class Event:
    kind: str            # visit | attended | expected | task
    when: datetime
    title: str
    subtitle: str | None
    patient_id: int | None
    in_plan: bool = False
    all_day: bool = False
    doctor_id: int | None = None
    service: str | None = None


def events(db: Session, user: User, start: date, days: int = 7, doctor_id: int | None = None) -> dict[date, list[Event]]:
    end = start + timedelta(days=days)
    s_dt, e_dt = datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())
    out: dict[date, list[Event]] = {start + timedelta(days=i): [] for i in range(days)}
    doc_name = user.doctor.name if user.role == "doctor" and user.doctor else None
    doc_pids = set()
    if user.role == "doctor" and user.doctor_id:
        doc_pids = {p.id for p in db.scalars(select(Patient).where(Patient.doctor_id == user.doctor_id))}

    statuses = all_statuses(db, doctor_id=user.doctor_id if user.role == "doctor" else None)
    plan_visit_ids = set()
    plan_patients = {ps.plan.patient_id for ps in statuses if ps.plan.status == "active"}

    from .specialists import DoctorIndex, all_doctors
    idx = DoctorIndex(all_doctors(db))

    rows = db.scalars(select(Visit).where(Visit.starts_at >= s_dt, Visit.starts_at < e_dt,
                                          Visit.status.in_(("booked", "attended", "done")))
                      .order_by(Visit.starts_at)).all()
    cal_days = {(v.patient_id, v.starts_at.date()) for v in rows if v.source != "cb_service_sale"}
    for v in rows:
        if v.source == "cb_service_sale" and (v.patient_id, v.starts_at.date()) in cal_days:
            continue                     # той самий візит уже є як запис календаря
        if user.role == "doctor" and not (v.patient_id in doc_pids or
                                          (doc_name and v.master_name and norm(doc_name) in norm(v.master_name))):
            continue
        doc = idx.find(v.master_name)
        if doctor_id and (not doc or doc.id != doctor_id):
            continue
        d = v.starts_at.date()
        if d in out:
            out[d].append(Event("attended" if v.status != "booked" else "visit", v.starts_at,
                                v.patient.full_name,
                                v.service_name if doc else " · ".join(filter(None, [v.service_name, v.master_name])),
                                v.patient_id, in_plan=v.patient_id in plan_patients,
                                doctor_id=doc.id if doc else None, service=v.service_name))

    if True:
        for ps in statuses:
            if ps.plan.status != "active" or ps.is_booked or not ps.next_procedure or not ps.due_date:
                continue
            due = max(ps.due_date, datetime.now())
            if doctor_id and ps.plan.doctor_id != doctor_id:
                continue
            if s_dt <= due < e_dt:
                out[due.date()].append(Event("expected", due, ps.plan.patient.full_name,
                                             f"очікується: {ps.next_procedure.item.name} (без запису)",
                                             ps.plan.patient_id, in_plan=True, all_day=True,
                                             doctor_id=ps.plan.doctor_id))

        for t in ([] if doctor_id else db.scalars(visible_query(user).where(Task.status == "open", Task.due_date >= start, Task.due_date < end,
                                                      Task.patient_id.is_not(None)))):   # задачі по клієнтах
            out[t.due_date].append(Event("task", datetime.combine(t.due_date, datetime.min.time()), t.title,
                                         None, t.patient_id, all_day=True))
    for d in out:
        out[d].sort(key=lambda e: (not e.all_day, e.when))
    return out


@dataclass
class Column:
    key: str
    doctor: object | None          # Doctor або None («Без спеціаліста»)
    events: list[Event]


def columns(db: Session, ev: dict[date, list[Event]], fixed: bool) -> dict[date, list[Column]]:
    """Колонки по спеціалістах для кожного дня.
    fixed=True (день): усі чинні спеціалісти календаря + ті, в кого є записи.
    fixed=False (тиждень): у кожен день — лише ті, хто того дня має записи (1, 2, 3…)."""
    from .specialists import calendar_doctors, all_doctors
    base = calendar_doctors(db)
    by_id = {d.id: d for d in all_doctors(db)}
    order = {d.id: i for i, d in enumerate(base)}
    out: dict[date, list[Column]] = {}
    for d, items in ev.items():
        groups: dict[int | None, list[Event]] = {}
        for e in items:
            groups.setdefault(e.doctor_id if e.doctor_id in by_id else None, []).append(e)
        ids = [k for k in groups if k is not None]
        if fixed:
            ids = [x.id for x in base] + [k for k in ids if k not in order]
        ids.sort(key=lambda k: order.get(k, 999))
        cols = [Column(str(k), by_id.get(k), groups.get(k, [])) for k in ids]
        if None in groups:
            cols.append(Column("none", None, groups[None]))
        out[d] = cols or [Column("none", None, [])]
    return out
