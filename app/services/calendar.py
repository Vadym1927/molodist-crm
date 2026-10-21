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


def events(db: Session, user: User, start: date, days: int = 7, master: str | None = None) -> dict[date, list[Event]]:
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

    def master_ok(m: str | None) -> bool:
        if master and (not m or norm(master) not in norm(m)):
            return False
        return True

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
        if not master_ok(v.master_name):
            continue
        d = v.starts_at.date()
        if d in out:
            out[d].append(Event("attended" if v.status != "booked" else "visit", v.starts_at,
                                v.patient.full_name, " · ".join(filter(None, [v.service_name, v.master_name])),
                                v.patient_id, in_plan=v.patient_id in plan_patients))

    if not master:
        for ps in statuses:
            if ps.plan.status != "active" or ps.is_booked or not ps.next_procedure or not ps.due_date:
                continue
            due = max(ps.due_date, datetime.now())
            if s_dt <= due < e_dt:
                out[due.date()].append(Event("expected", due, ps.plan.patient.full_name,
                                             f"очікується: {ps.next_procedure.item.name} (без запису)",
                                             ps.plan.patient_id, in_plan=True, all_day=True))

        for t in db.scalars(visible_query(user).where(Task.status == "open", Task.due_date >= start, Task.due_date < end,
                                                      Task.patient_id.is_not(None))):   # задачі по клієнтах
            out[t.due_date].append(Event("task", datetime.combine(t.due_date, datetime.min.time()), t.title,
                                         None, t.patient_id, all_day=True))
    for d in out:
        out[d].sort(key=lambda e: (not e.all_day, e.when))
    return out
