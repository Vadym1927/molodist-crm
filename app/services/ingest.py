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


class Attached(str):
    """Не план, а документ, прикріплений до картки (напр. «Аналіз шкіри OBSERV»): текст для відповіді + patient_id."""
    patient_id: int | None = None
    patient_name: str | None = None


ANALYSIS_TITLE = "аналізу шкіри"


def parse_analysis(path) -> dict | None:
    """Висновок OBSERV «Висновок за результатами проведеного аналізу шкіри» (без процедур і цін):
    {name, phone, doctor, at} — пацієнт з блоку «Інформація про клієнта», дата сесії «dd.mm.yyyy, HH:MM»."""
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            t = pdf.pages[0].extract_text() or ""
    except Exception:
        return None
    return parse_analysis_text(t)


def parse_analysis_text(t: str) -> dict | None:
    if ANALYSIS_TITLE not in t.lower():
        return None
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    try:
        i = next(k for k, x in enumerate(lines) if x.startswith("Інформація про клієнта"))
    except StopIteration:
        return None
    name = phone = None
    for k in range(i + 1, min(i + 9, len(lines))):
        if lines[k] == "НАЗВА" and k + 1 < len(lines):
            name = lines[k + 1]
        if lines[k] == "ТЕЛЕФОН" and k + 1 < len(lines) and re.fullmatch(r"[\d +()\-]{9,}", lines[k + 1]):
            phone = lines[k + 1]
        if lines[k].startswith("Деталі сеансу"):
            break
    m = re.search(r"(\d{2}\.\d{2}\.\d{4}),\s*(\d{1,2}:\d{2})", t[t.find("Деталі сеансу"):] if "Деталі сеансу" in t else t)
    at = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%Y %H:%M") if m else None
    doc = re.search(r"КЛІНІКА ПРАКТИКУЮЧИЙ\s*\n\s*MOLODIST\s+([^\n]+)", t)
    if not name or name in ("ЕЛЕКТРОННА ПОШТА", "ТЕЛЕФОН"):
        return None
    return {"name": name, "phone": phone, "doctor": doc.group(1).strip() if doc else None, "at": at}


def attach_analysis(db: Session, an: dict, path: str, filename: str, tg_chat: str | None = None) -> Attached:
    from .patient_match import best_match
    p = best_match(db, an["name"], an.get("phone"))
    day = an["at"].strftime("%d.%m.%Y") if an.get("at") else None
    cap = "Аналіз шкіри OBSERV" + (f" · {day}" if day else "")
    if not p:
        db.add(PatientFile(kind="pdf", path=path, caption=f"{cap} · {an['name']}", category="doc", tg_chat=tg_chat,
                           taken_at=an["at"].date() if an.get("at") else None))
        db.commit()
        return Attached(f"{cap}: пацієнта «{an['name']}» не знайдено — файл збережено без картки")
    dup = db.scalar(select(PatientFile).where(PatientFile.patient_id == p.id, PatientFile.caption == cap))
    if dup:
        try:
            Path(path).unlink()
        except OSError:
            pass
        r = Attached(f"📎 {cap} вже є в картці: {p.full_name}")
    else:
        db.add(PatientFile(patient_id=p.id, kind="pdf", path=path, caption=cap, category="doc", tg_chat=tg_chat,
                           taken_at=an["at"].date() if an.get("at") else None))
        db.commit()
        r = Attached(f"📎 {cap} → прикріплено до картки: {p.full_name}")
    r.patient_id, r.patient_name = p.id, p.full_name
    return r


def ingest_pdf(db: Session, data: bytes, filename: str, tg_chat: str | None = None):
    """Повертає (plan | None, path, error | None)."""
    path = store(data, filename)
    try:
        parsed = parse_observ_pdf(path)
    except ObservParseError as e:
        an = parse_analysis(path)
        if an:
            return None, path, attach_analysis(db, an, path, filename, tg_chat)
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
