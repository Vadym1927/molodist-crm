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
    ends: datetime | None = None
    status_label: str | None = None
    appt_id: int | None = None           # запис CRM (відкривається вікном у календарі)
    status: str | None = None            # booked | confirmed | arrived | done | no_show (для кольору блоку)
    note: str | None = None              # замітка до запису
    tag: str | None = None               # Первинний / Постійний пацієнт / ФАНАТ

    @property
    def services(self) -> list[str]:
        return [s.strip() for s in (self.service or "").split(", ") if s.strip()]

    @property
    def minutes(self) -> int:
        return max(15, int((self.ends - self.when).total_seconds() // 60)) if self.ends else 60


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
    from ..models import CbService
    idx = DoctorIndex(all_doctors(db))
    durations = {i: m for i, m in db.execute(select(CbService.id, CbService.time_min)) if m and 5 <= m <= 480}
    merged: dict = {}

    rows = db.scalars(select(Visit).where(Visit.starts_at >= s_dt, Visit.starts_at < e_dt,
                                          Visit.status.in_(("booked", "attended", "done")))
                      .order_by(Visit.starts_at)).all()
    cal_days = {(v.patient_id, v.starts_at.date()) for v in rows if v.source != "cb_service_sale"}
    # записи CRM
    from ..models import Appointment
    from .appointments import STATUSES as _AST
    appts = db.scalars(select(Appointment).where(Appointment.start >= s_dt, Appointment.start < e_dt,
                                                 Appointment.status != "cancelled").order_by(Appointment.start)).all()
    mirrored = {f"{a.cb_record_id}-" for a in appts if a.cb_record_id}
    for a in appts:
        if user.role == "doctor" and a.doctor_id != user.doctor_id and a.patient_id not in doc_pids:
            continue
        if doctor_id and a.doctor_id != doctor_id:
            continue
        d = a.start.date()
        if d in out:
            sv = ", ".join(s.get("name", "") for s in a.services or []) or (a.comment or "")
            out[d].append(Event("attended" if a.status in ("done", "no_show") else "visit", a.start, a.patient.full_name, sv,
                                a.patient_id, in_plan=a.patient_id in plan_patients, doctor_id=a.doctor_id,
                                service=", ".join(s.get("name", "") for s in a.services or []) or None,
                                ends=a.end, status_label=_AST.get(a.status), appt_id=a.id, status=a.status, note=a.comment))
    for v in rows:
        if mirrored and any(v.external_id.startswith(m) for m in mirrored):
            continue                     # той самий запис, створений у CRM і продубльований у CleverBox
        if v.source == "cb_service_sale" and (v.patient_id, v.starts_at.date()) in cal_days:
            continue                     # той самий візит уже є як запис календаря
        if user.role == "doctor" and not (v.patient_id in doc_pids or
                                          (doc_name and v.master_name and norm(doc_name) in norm(v.master_name))):
            continue
        doc = idx.find(v.master_name)
        if doctor_id and (not doc or doc.id != doctor_id):
            continue
        d = v.starts_at.date()
        dur = durations.get(v.service_id) or 60
        ends = v.ends_at or v.starts_at + timedelta(minutes=dur)
        key = (v.patient_id, v.starts_at, doc.id if doc else v.master_name)
        if key in merged:                    # кілька послуг в одному візиті — один блок
            e = merged[key]
            if v.service_name and v.service_name not in (e.service or ""):
                e.service = ", ".join(filter(None, [e.service, v.service_name]))
                e.subtitle = e.service if doc else " · ".join(filter(None, [e.service, v.master_name]))
            if not v.ends_at:
                e.ends = max(e.ends, ends) if e.ends else ends
            continue
        if d in out:
            out[d].append(Event("attended" if v.status != "booked" else "visit", v.starts_at,
                                v.patient.full_name,
                                v.service_name if doc else " · ".join(filter(None, [v.service_name, v.master_name])),
                                v.patient_id, in_plan=v.patient_id in plan_patients,
                                doctor_id=doc.id if doc else None, service=v.service_name, ends=ends,
                                status_label=v.status_label, status="booked" if v.status == "booked" else "done"))
            merged[key] = out[d][-1]

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
    _tag_patients(db, out)
    for d in out:
        out[d].sort(key=lambda e: (not e.all_day, e.when))
    return out


def tag_for(visits: int) -> str:
    """Мітка пацієнта за кількістю візитів (як категорії у CleverBox)."""
    if visits <= 1:
        return "Первинний пацієнт"
    if visits >= 10:
        return "ФАНАТ"
    return "Постійний пацієнт"


def _tag_patients(db: Session, out: dict) -> None:
    from sqlalchemy import func
    pids = {e.patient_id for evs in out.values() for e in evs if e.patient_id and e.kind in ("visit", "attended")}
    if not pids:
        return
    days = {pid: n for pid, n in db.execute(
        select(Visit.patient_id, func.count(func.distinct(func.date(Visit.starts_at))))
        .where(Visit.patient_id.in_(pids), Visit.status.in_(("attended", "done"))).group_by(Visit.patient_id))}
    for evs in out.values():
        for e in evs:
            if e.patient_id in pids and e.kind in ("visit", "attended"):
                e.tag = tag_for((days.get(e.patient_id) or 0) + (0 if e.kind == "attended" else 1))


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


# ---------------- Сітка часу (вигляд «День») ----------------

@dataclass
class Block:
    ev: Event
    top: float          # у хвилинах від початку сітки
    height: float
    lane: int = 0
    lanes: int = 1


@dataclass
class Free:
    start: datetime
    end: datetime
    top: float
    height: float

    @property
    def hours(self) -> str:
        m = int((self.end - self.start).total_seconds() // 60)
        return (f"{m // 60} год" + (f" {m % 60} хв" if m % 60 else "")) if m >= 60 else f"{m} хв"


def time_grid(day: date, cols: list[Column], work_from: int = 10, work_to: int = 19) -> dict:
    """Розкладка блоків по часу: межі сітки, блоки з доріжками для накладок, вільні вікна ≥ 1 год."""
    timed = [e for c in cols for e in c.events if not e.all_day]
    h0 = min([work_from] + [e.when.hour for e in timed])
    h1 = max([work_to] + [min(24, (e.ends or e.when).hour + (1 if (e.ends or e.when).minute else 0)) for e in timed])
    origin = datetime.combine(day, datetime.min.time()).replace(hour=h0)
    now = datetime.now()
    out = []
    for c in cols:
        evs = sorted((e for e in c.events if not e.all_day), key=lambda e: e.when)
        blocks, cluster, cluster_end = [], [], None
        def flush():
            lanes_end: list[datetime] = []
            for b in cluster:
                for i, le in enumerate(lanes_end):
                    if le <= b.ev.when:
                        b.lane, lanes_end[i] = i, b.ev.when + timedelta(minutes=b.ev.minutes)
                        break
                else:
                    b.lane = len(lanes_end)
                    lanes_end.append(b.ev.when + timedelta(minutes=b.ev.minutes))
            for b in cluster:
                b.lanes = max(1, len(lanes_end))
        for e in evs:
            end = e.when + timedelta(minutes=e.minutes)
            b = Block(e, (e.when - origin).total_seconds() / 60, e.minutes)
            if cluster and cluster_end and e.when >= cluster_end:
                flush(); cluster = []
                cluster_end = None
            cluster.append(b)
            cluster_end = max(cluster_end, end) if cluster_end else end
            blocks.append(b)
        if cluster:
            flush()
        free = []
        if c.doctor is not None and day >= now.date():
            cur = datetime.combine(day, datetime.min.time()).replace(hour=work_from)
            if day == now.date():
                cur = max(cur, now.replace(minute=(0 if now.minute < 30 else 30), second=0, microsecond=0)
                          + timedelta(minutes=30))
            stop = datetime.combine(day, datetime.min.time()).replace(hour=work_to)
            for e in evs + [None]:
                s_ = e.when if e else stop
                if s_ > stop:
                    s_ = stop
                if (s_ - cur).total_seconds() >= 3600:
                    free.append(Free(cur, s_, (cur - origin).total_seconds() / 60, (s_ - cur).total_seconds() / 60))
                if e:
                    cur = max(cur, e.when + timedelta(minutes=e.minutes))
        out.append({"col": c, "blocks": blocks, "free": free,
                    "allday": [e for e in c.events if e.all_day]})
    return {"h0": h0, "h1": h1, "cols": out, "now_top": (now - origin).total_seconds() / 60
            if now.date() == day and h0 <= now.hour < h1 else None}


def month_summary(db: Session, user: User, first: date, doctor_id: int | None = None) -> dict:
    """Місяць: по кожному дню — скільки записів у кожного спеціаліста."""
    from .specialists import DoctorIndex, all_doctors
    idx = DoctorIndex(all_doctors(db))
    start = first - timedelta(days=first.weekday())
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    end = last + timedelta(days=6 - last.weekday())
    days: dict[date, dict] = {start + timedelta(days=i): {"total": 0, "docs": {}} for i in range((end - start).days + 1)}
    q = select(Visit.starts_at, Visit.master_name, Visit.patient_id).where(
        Visit.starts_at >= datetime.combine(start, datetime.min.time()),
        Visit.starts_at < datetime.combine(end + timedelta(days=1), datetime.min.time()),
        Visit.status.in_(("booked", "attended")), Visit.source != "cb_service_sale")
    doc_pids = None
    if user.role == "doctor" and user.doctor_id:
        doc_pids = {p.id for p in db.scalars(select(Patient).where(Patient.doctor_id == user.doctor_id))}
    seen = set()
    for at, master, pid in db.execute(q):
        d = idx.find(master)
        if doctor_id and (not d or d.id != doctor_id):
            continue
        if doc_pids is not None and pid not in doc_pids and not (d and d.id == user.doctor_id):
            continue
        key = (pid, at)
        if key in seen:
            continue
        seen.add(key)
        cell = days.get(at.date())
        if cell is None:
            continue
        cell["total"] += 1
        k = d.id if d else 0
        cell["docs"][k] = cell["docs"].get(k, 0) + 1
    return {"start": start, "end": end, "days": days, "by_id": idx.by_id}
