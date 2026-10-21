"""Прийом файлів (з ТГ-чатів або веб-завантаження): PDF бюті-плану -> план у CRM, фото -> у картку."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Patient, PatientFile
from ..parsers.observ import parse_observ_pdf, ObservParseError, norm
from .plans import import_plan


def store(data: bytes, filename: str) -> str:
    base = Path(settings.files_dir) / datetime.now().strftime("%Y/%m")
    base.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\- ]+", "_", filename)[-120:] or "file"
    path = base / f"{uuid.uuid4().hex[:8]}_{safe}"
    path.write_bytes(data)
    return str(path)


def ingest_pdf(db: Session, data: bytes, filename: str, tg_chat: str | None = None):
    """Повертає (plan | None, path, error | None)."""
    path = store(data, filename)
    try:
        parsed = parse_observ_pdf(path)
    except ObservParseError as e:
        db.add(PatientFile(kind="pdf", path=path, caption=filename, tg_chat=tg_chat))
        db.commit()
        return None, path, str(e)
    plan = import_plan(db, parsed, source_file=filename)
    plan.source_file = path
    db.add(PatientFile(patient_id=plan.patient_id, kind="plan_pdf", path=path, caption=filename, tg_chat=tg_chat))
    db.commit()
    from ..integrations import webhooks
    from .kpi import compute_plan
    from .serialize import plan_dict
    webhooks.fire("plan.created", plan_dict(compute_plan(db, plan)))
    return plan, path, None


def guess_patient(db: Session, text: str | None) -> Patient | None:
    """Пацієнт за підписом до фото: шукаємо ПІБ (у будь-якому порядку) або телефон."""
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    patients = db.scalars(select(Patient)).all()
    if len(digits) >= 9:
        for p in patients:
            if p.phone and re.sub(r"\D", "", p.phone)[-9:] == digits[-9:]:
                return p
    words = set(norm(text).split())
    hits = [p for p in patients if set(norm(p.full_name).split()) <= words]
    return hits[0] if len(hits) == 1 else None


def ingest_photo(db: Session, data: bytes, filename: str, caption: str | None, tg_chat: str | None = None,
                 patient: Patient | None = None) -> PatientFile:
    path = store(data, filename)
    patient = patient or guess_patient(db, caption)
    f = PatientFile(patient_id=patient.id if patient else None, kind="photo", path=path,
                    caption=caption, tg_chat=tg_chat)
    db.add(f)
    db.commit()
    return f
