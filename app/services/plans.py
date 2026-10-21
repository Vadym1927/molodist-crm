"""Імпорт розпарсеного бюті-плану в базу + дефолтні інтервали/тривалість засобів."""
from __future__ import annotations

import re
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import Patient, Doctor, BeautyPlan, PlanItem
from ..parsers.observ import ParsedPlan, norm
from ..config import settings

# Інтервал між процедурами курсу (днів). Лікар може змінити в картці.
PROCEDURE_INTERVALS = [
    (r"IPL|ФОТООМОЛОДЖ", 28),
    (r"ГІГІЄН|ЧИСТК", 30),
    (r"ПІЛІНГ", 14),
    (r"БІОРЕВІТАЛ|МЕЗО|REJURAN|РЕДЖУРАН|PLENHYAGE|JALUPRO|NITHYA|VITARAN|ВІТАРАН|ПОЛІНУКЛ", 14),
    (r"КАРБОКС", 7),
    (r"RF|ЛІФТИНГ|SMAS|HIFU", 30),
    (r"БОТУЛ|БОТОКС|DYSPORT|XEOMIN", 120),
    (r"ФІЛЕР|КОНТУР", 180),
]

# Витрата засобу за одне нанесення, мл
ML_PER_USE = [
    (r"ОЧЕЙ|ОЧІ|EYE|ПОВІК", 0.1),
    (r"ОЧИЩ|CLEANS|ГЕЛЬ ДЛЯ ВМИВ|ПІНК", 2.0),
    (r"ТОНІК|TONIC|МІСТ|MIST|ЛОСЬЙОН", 1.5),
    (r"SPF|СПФ|СОНЦЕЗАХ", 1.0),
    (r"СИРОВАТК|SERUM|ЕЛІКСИР|КОНЦЕНТРАТ", 0.3),
    (r"КРЕМ|CREAM|ЕМУЛЬС|ФЛЮЇД", 0.5),
]


def default_interval(name: str) -> int:
    n = name.upper()
    for pat, days in PROCEDURE_INTERVALS:
        if re.search(pat, n):
            return days
    return settings.default_procedure_interval_days


def default_supply_days(name: str, volume: str | None, usage: list | None) -> int | None:
    if not volume:
        return 60
    m = re.match(r"(\d+(?:[.,]\d+)?)", volume)
    if not m:
        return 60
    ml = float(m.group(1).replace(",", "."))
    per_use = 0.7
    n = name.upper()
    for pat, v in ML_PER_USE:
        if re.search(pat, n):
            per_use = v
            break
    uses = max(len(usage or []), 1)
    return int(max(14, min(365, ml / (per_use * uses))))


def get_or_create_doctor(db: Session, name: str | None) -> Doctor | None:
    if not name:
        return None
    key = name.replace("ʼ", "'").strip()
    for d in db.scalars(select(Doctor)):
        if d.name.replace("ʼ", "'").strip().lower() == key.lower():
            return d
    d = Doctor(name=name.strip())
    db.add(d)
    db.flush()
    return d


def find_patient(db: Session, name: str, phone: str | None = None) -> Patient | None:
    if phone:
        digits = re.sub(r"\D", "", phone)[-9:]
        for p in db.scalars(select(Patient).where(Patient.phone.is_not(None))):
            if re.sub(r"\D", "", p.phone or "")[-9:] == digits:
                return p
    target = set(norm(name).split())
    for p in db.scalars(select(Patient)):
        if set(norm(p.full_name).split()) == target:   # «Олена Даниленко» == «Даниленко Олена»
            return p
    return None


def import_plan(db: Session, parsed: ParsedPlan, source_file: str | None = None,
                patient: Patient | None = None) -> BeautyPlan:
    doctor = get_or_create_doctor(db, parsed.doctor_name)
    if patient is None:
        patient = find_patient(db, parsed.patient_name, parsed.patient_phone)
    if patient is None:
        patient = Patient(full_name=parsed.patient_name, phone=parsed.patient_phone,
                          doctor_id=doctor.id if doctor else None)
        db.add(patient)
        db.flush()
    elif doctor and not patient.doctor_id:
        patient.doctor_id = doctor.id

    # повторне завантаження того ж плану (та сама дата сесії) — замінюємо позиції
    plan = None
    if parsed.session_at:
        plan = db.scalar(select(BeautyPlan).where(BeautyPlan.patient_id == patient.id,
                                                  BeautyPlan.session_at == parsed.session_at))
    if plan is None:
        plan = BeautyPlan(patient_id=patient.id)
        db.add(plan)
    else:
        plan.items.clear()

    plan.doctor_id = doctor.id if doctor else None
    plan.title = parsed.plan_title or _title_from_file(source_file)
    plan.session_at = parsed.session_at
    plan.complaints = parsed.complaints
    plan.medical_notes = parsed.comments
    plan.routine_comment = parsed.routine_comment
    plan.total_planned = parsed.total
    plan.source_file = source_file
    for it in parsed.items:
        plan.items.append(PlanItem(
            kind=it.kind, name=it.name, qty=it.qty, unit_price=it.unit_price, total=it.total,
            volume=it.volume, usage=it.usage, url=it.url,
            interval_days=default_interval(it.name) if it.kind == "procedure" else None,
            supply_days=default_supply_days(it.name, it.volume, it.usage) if it.kind == "product" else None,
        ))
    db.flush()
    from .catalog import link_plan          # засоби плану -> каталог «Продукти»
    link_plan(db, plan)
    db.flush()
    return plan


def _title_from_file(path: str | None) -> str | None:
    if not path:
        return None
    m = re.search(r"BeautyPlan[ _]*([^/\\]+?)\.pdf$", path, re.I)
    return ("Beauty Plan " + m.group(1).replace("_", " ").strip()) if m else None
