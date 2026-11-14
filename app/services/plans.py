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
    """Телефон → ПІБ у будь-якому порядку → ПІБ з PDF без по батькові (якщо такий пацієнт один)."""
    from .patient_match import best_match
    return best_match(db, name, phone)


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
        patient._created = True                       # нова картка — бот підкаже можливий дубль
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


def repair_glued_prices(db: Session) -> int:
    """Один раз: плани з PDF, де ціна злиплася з назвою («…MEDIK84 000,00 ₴» → 84 000 ₴ замість «…MEDIK8» за 4 000 ₴)
    або позиція без ціни загубилась. Перечитуємо збережений PDF; правимо лише такі позиції, решту (правки лікаря) не чіпаємо."""
    from pathlib import Path
    from ..models import SyncState
    from ..parsers.observ import parse_observ_pdf
    if db.get(SyncState, "plan_glued_price_fix_v1"):
        return 0
    n = 0
    for plan in db.scalars(select(BeautyPlan).where(BeautyPlan.source_file.is_not(None))):
        if not any(i.kind == "product" and (i.total or 0) >= 10000 for i in plan.items) and \
                abs(sum(i.total or 0 for i in plan.items) - (plan.total_planned or 0)) < 1:
            continue
        try:
            if not Path(plan.source_file).exists():
                continue
            parsed = parse_observ_pdf(plan.source_file)
        except Exception:  # noqa: BLE001
            continue
        if abs(sum(i.total for i in parsed.items) - (parsed.total or 0)) > 0.01:
            continue                                        # новий розбір теж не сходиться з «Загалом» — не чіпаємо
        have = {i.name: i for i in plan.items}
        changed = False
        for it in parsed.items:
            old = have.get(it.name[:-1]) if it.name not in have else None      # стара назва — без останньої цифри
            if old is not None and abs((old.total or 0) - it.total) > 0.01 and old.qty == it.qty:
                old.name, old.unit_price, old.total, changed = it.name, it.unit_price, it.total, True
            elif it.name not in have and old is None and it.kind == "product":
                plan.items.append(PlanItem(kind=it.kind, name=it.name, qty=it.qty, unit_price=it.unit_price, total=it.total,
                                           volume=it.volume, usage=it.usage, url=it.url,
                                           supply_days=default_supply_days(it.name, it.volume, it.usage)))
                changed = True
        if changed:
            plan.total_planned = round(sum(i.total or 0 for i in plan.items), 2)
            db.flush()
            from .catalog import link_plan
            link_plan(db, plan)
            n += 1
    db.merge(SyncState(key="plan_glued_price_fix_v1", value=str(n)))
    db.commit()
    return n
