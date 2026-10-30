"""Вікно візиту в календарі (як у CleverBox): візит, нотатки, історія, витрати, фото до/після, про візит.

Працює і для записів CRM (Appointment), і для візитів CleverBox (Visit, кілька послуг одного візиту).
Медичні поля пацієнта (анамнез, алергії…) сюди НЕ потрапляють.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (Appointment, AuditLog, CbService, Doctor, Patient, PatientFile, ProcedureNorm, User, Visit,
                      VisitNote)

CB_STATUS = {"booked": "Записаний", "attended": "Візит завершено", "done": "Візит завершено", "cancelled": "Скасовано"}


def _services_appt(db: Session, a: Appointment) -> list[dict]:
    out, t = [], a.start
    for s in a.services or []:
        m = int(s.get("minutes") or 30)
        out.append({"id": s.get("id"), "name": s.get("name") or "Послуга", "start": t, "end": t + timedelta(minutes=m),
                    "minutes": m, "price": float(s.get("price") or 0)})
        t += timedelta(minutes=m)
    return out


def _services_cb(db: Session, vs: list[Visit]) -> list[dict]:
    out = []
    for v in vs:
        cs = db.get(CbService, v.service_id) if v.service_id else None
        m = (cs.time_min if cs and cs.time_min else None)
        end = v.ends_at or (v.starts_at + timedelta(minutes=m or 60))
        out.append({"id": v.service_id, "name": v.service_name or "Послуга", "start": v.starts_at, "end": end,
                    "minutes": int((end - v.starts_at).total_seconds() // 60),
                    "price": float(v.amount if v.amount is not None else (cs.price if cs and cs.price else 0))})
    return out


def costs(db: Session, services: list[dict]) -> tuple[list[dict], float]:
    """Витратні матеріали за нормами списання (ProcedureNorm) на кожну послугу візиту."""
    out, total = [], 0.0
    for s in services:
        if not s.get("id"):
            out.append({"service": s["name"], "rows": [], "sum": 0.0})
            continue
        rows = []
        for n in db.scalars(select(ProcedureNorm).where(ProcedureNorm.service_id == int(s["id"]))):
            pr = n.product
            c = round((n.qty or 0) * float(pr.cost or 0), 2) if pr else 0.0
            rows.append({"name": pr.name if pr else f"Товар #{n.product_id}", "qty": n.qty, "unit": (pr.unit if pr else None) or "шт.",
                         "sum": c, "wh": n.warehouse or (pr.warehouse if pr else None)})
        sm = round(sum(r["sum"] for r in rows), 2)
        total += sm
        out.append({"service": s["name"], "rows": rows, "sum": sm})
    return out, round(total, 2)


def card(db: Session, appt_id: int = 0, visit_ids: list[int] | None = None) -> dict | None:
    a = db.get(Appointment, appt_id) if appt_id else None
    vs = [] if a else [v for v in (db.get(Visit, i) for i in (visit_ids or [])) if v]
    if not a and not vs:
        return None
    if a:
        pid, start, end = a.patient_id, a.start, a.end
        services = _services_appt(db, a)
        doctor = db.get(Doctor, a.doctor_id) if a.doctor_id else None
        key = f"appt:{a.id}"
        number = f"CRM-{a.id}" + (f" · CleverBox {a.cb_record_id}" if a.cb_record_id else "")
        author = db.get(User, a.created_by).name if a.created_by and db.get(User, a.created_by) else None
        created = a.created_at
        status, status_label = a.status, None
    else:
        vs.sort(key=lambda v: v.starts_at)
        v0 = vs[0]
        pid, start = v0.patient_id, v0.starts_at
        services = _services_cb(db, vs)
        end = max(s["end"] for s in services)
        from .specialists import DoctorIndex, all_doctors
        doctor = DoctorIndex(all_doctors(db)).find(v0.master_name)
        key = f"cb:{v0.id}"
        rec = (v0.external_id or "").split("-")[0]
        number = f"CleverBox {rec}" if rec.isdigit() else f"№{v0.id}"
        author, created = None, None
        status = v0.status
        status_label = v0.status_label or CB_STATUS.get(v0.status, v0.status)
    p = db.get(Patient, pid)
    keys = [key] + ([f"cb:{v.id}" for v in vs[1:]] if vs else [])
    notes = db.scalars(select(VisitNote).where(VisitNote.key.in_(keys)).order_by(VisitNote.created_at.desc())).all()
    users = {u.id: u.name for u in db.scalars(select(User))}
    # історія: журнал дій по цьому запису CRM + нотатки
    hist = []
    if a:
        for r in db.scalars(select(AuditLog).where(AuditLog.entity == "patient", AuditLog.entity_id == pid,
                                                   AuditLog.action.like("appointment.%")).order_by(AuditLog.at.desc()).limit(200)):
            if (r.details or {}).get("appt") == a.id:
                hist.append({"at": r.at, "who": users.get(r.user_id, "Система"), "action": r.action, "details": r.details or {}})
    else:
        hist.append({"at": vs[0].synced_at, "who": "CleverBox", "action": "cb.sync",
                     "details": {"status": status_label}})
    for n in notes:
        hist.append({"at": n.created_at, "who": users.get(n.user_id, "—"), "action": "note", "details": {"text": n.text[:120]}})
    hist.sort(key=lambda h: h["at"] or datetime.min, reverse=True)
    day = start.date()
    photos = db.scalars(select(PatientFile).where(PatientFile.patient_id == pid, PatientFile.kind == "photo")
                        .order_by(PatientFile.created_at.desc())).all()
    ph_day = [f for f in photos if (f.taken_at or (f.created_at.date() if f.created_at else None)) == day]
    cst, cst_total = costs(db, services)
    return {"a": a, "vs": vs, "p": p, "start": start, "end": end, "services": services, "doctor": doctor, "key": key,
            "number": number, "author": author, "created": created, "status": status, "status_label": status_label,
            "notes": notes, "users": users, "hist": hist, "costs": cst, "cost_total": cst_total,
            "total": round(sum(s["price"] for s in services), 2),
            "before": [f for f in ph_day if f.stage == "before"], "after": [f for f in ph_day if f.stage in ("after", "progress")],
            "other_photos": [f for f in ph_day if f.stage not in ("before", "after", "progress")]}
