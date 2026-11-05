"""Звільнення співробітника: перед остаточним звільненням CRM питає, кому передати майбутні візити й відкриті задачі,
передає їх, прибирає з календаря й (за бажанням) блокує вхід у CRM. Нічого не видаляється."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, Doctor, StaffShift, Task, User


def users_of(db: Session, d: Doctor) -> list[User]:
    return db.scalars(select(User).where(User.doctor_id == d.id)).all()


def preview(db: Session, d: Doctor) -> dict:
    now = datetime.now()
    appts = db.scalars(select(Appointment).where(Appointment.doctor_id == d.id, Appointment.start >= now,
                                                 Appointment.status.notin_(("cancelled", "no_show", "done", "paid"))).order_by(Appointment.start)).all()
    uids = [u.id for u in users_of(db, d)]
    tasks = db.scalars(select(Task).where(Task.assignee_id.in_(uids), Task.status == "open").order_by(Task.due_date)).all() if uids else []
    shifts = db.scalars(select(StaffShift).where(StaffShift.doctor_id == d.id, StaffShift.day >= date.today())).all()
    return {"appts": appts, "tasks": tasks, "users": users_of(db, d), "shifts": len(shifts),
            "doctors": db.scalars(select(Doctor).where(Doctor.active.is_(True), Doctor.id != d.id).order_by(Doctor.name)).all(),
            "assignees": db.scalars(select(User).where(User.active.is_(True), User.id.notin_(uids or [0])).order_by(User.name)).all()}


def fire(db: Session, d: Doctor, to_doctor: int | None, to_user: int | None, block_login: bool = True,
         drop_schedule: bool = True, by_user: User | None = None) -> dict:
    """to_doctor: кому передати візити (None — візити лишаються «без фахівця»); to_user: кому задачі (None — черга адміністраторів)."""
    pv = preview(db, d)
    if to_doctor and not db.get(Doctor, to_doctor):
        raise ValueError("Спеціаліста для візитів не знайдено")
    if to_user and not db.get(User, to_user):
        raise ValueError("Користувача для задач не знайдено")
    from .appointments import conflicts
    warn = []
    for a in pv["appts"]:
        a.doctor_id = to_doctor or None
        a.updated_at = datetime.utcnow()
        a.comment = ((a.comment or "") + f"\nПередано від {d.name} (звільнення {date.today():%d.%m.%Y})").strip()[:2000]
        if to_doctor:
            c = conflicts(db, to_doctor, a.start, a.end, exclude_id=a.id)
            if c:
                warn.append(f"{a.start:%d.%m %H:%M} — {c[0]}")
    for t in pv["tasks"]:
        t.assignee_id = to_user or None
        if not to_user:
            t.assignee_role = "admin"
    n_shifts = 0
    if drop_schedule:                                   # майбутні зміни графіка — прибрати (звільнений не виходить)
        for s in db.scalars(select(StaffShift).where(StaffShift.doctor_id == d.id, StaffShift.day > date.today())):
            db.delete(s)
            n_shifts += 1
    d.active, d.status_locked, d.fired_at, d.in_calendar = False, True, date.today(), False
    blocked = 0
    if block_login:
        for u in pv["users"]:
            if u.role != "owner" and u.active:
                u.active = False
                blocked += 1
    db.commit()
    return {"appts": len(pv["appts"]), "tasks": len(pv["tasks"]), "shifts": n_shifts, "blocked": blocked, "conflicts": warn}
