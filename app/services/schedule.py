"""Графік спеціалістів: зміни по днях, заповнення за шаблоном тижня, копіювання тижня, підсумок годин."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Doctor, StaffShift

TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def valid(start: str, end: str) -> bool:
    return bool(TIME.match(start or "") and TIME.match(end or "") and start < end)


def doctors(db: Session) -> list[Doctor]:
    return [d for d in db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name))
            if getattr(d, "in_calendar", True) is not False]


def shifts(db: Session, d_from: date, d_to: date) -> dict[tuple[int, date], StaffShift]:
    return {(s.doctor_id, s.day): s for s in db.scalars(select(StaffShift).where(StaffShift.day >= d_from, StaffShift.day <= d_to))}


def for_day(db: Session, day: date) -> dict[int, StaffShift]:
    return {s.doctor_id: s for s in db.scalars(select(StaffShift).where(StaffShift.day == day))}


def set_shift(db: Session, doctor_id: int, day: date, start: str | None, end: str | None, place: str | None = None,
              note: str | None = None, user_id: int | None = None) -> StaffShift | None:
    """start/end порожні — вихідний (зміну прибрано)."""
    s = db.scalar(select(StaffShift).where(StaffShift.doctor_id == doctor_id, StaffShift.day == day))
    if not start or not end:
        if s:
            db.delete(s)
        db.commit()
        return None
    if not valid(start, end):
        raise ValueError("Час у форматі ГГ:ХХ, початок раніше за кінець")
    s = s or StaffShift(doctor_id=doctor_id, day=day)
    s.start, s.end, s.place, s.note = start, end, (place or None), (note or None)
    s.updated_by, s.updated_at = user_id, datetime.utcnow()
    db.add(s)
    db.commit()
    return s


def fill(db: Session, doctor_id: int, d_from: date, d_to: date, weekdays: set[int], start: str, end: str,
         place: str | None = None, mode: str = "weekdays", user_id: int | None = None, overwrite: bool = True) -> int:
    """Заповнити період: mode=weekdays — за днями тижня; '2/2', '1/1', '3/3' — змінний графік від d_from."""
    if not valid(start, end):
        raise ValueError("Час у форматі ГГ:ХХ, початок раніше за кінець")
    have = {s.day: s for s in db.scalars(select(StaffShift).where(StaffShift.doctor_id == doctor_id,
                                                                   StaffShift.day >= d_from, StaffShift.day <= d_to))}
    n, day, i = 0, d_from, 0
    m = re.match(r"^(\d)/(\d)$", mode or "")
    while day <= d_to:
        work = (i % (int(m[1]) + int(m[2]))) < int(m[1]) if m else day.weekday() in weekdays
        if work and (overwrite or day not in have):
            s = have.get(day) or StaffShift(doctor_id=doctor_id, day=day)
            s.start, s.end, s.place, s.updated_by, s.updated_at = start, end, place or None, user_id, datetime.utcnow()
            db.add(s)
            n += 1
        day += timedelta(days=1)
        i += 1
    db.commit()
    return n


def clear(db: Session, doctor_id: int | None, d_from: date, d_to: date) -> int:
    q = select(StaffShift).where(StaffShift.day >= d_from, StaffShift.day <= d_to)
    if doctor_id:
        q = q.where(StaffShift.doctor_id == doctor_id)
    rows = db.scalars(q).all()
    for s in rows:
        db.delete(s)
    db.commit()
    return len(rows)


def copy_week(db: Session, src_monday: date, dst_monday: date, user_id: int | None = None) -> int:
    src = db.scalars(select(StaffShift).where(StaffShift.day >= src_monday, StaffShift.day < src_monday + timedelta(days=7))).all()
    n = 0
    for s in src:
        set_shift(db, s.doctor_id, dst_monday + (s.day - src_monday), s.start, s.end, s.place, s.note, user_id)
        n += 1
    return n


def totals(sh: dict, doctor_ids: list[int]) -> dict[int, dict]:
    out = defaultdict(lambda: {"days": 0, "minutes": 0})
    for (did, _d), s in sh.items():
        if did in doctor_ids:
            out[did]["days"] += 1
            out[did]["minutes"] += s.minutes
    return out
