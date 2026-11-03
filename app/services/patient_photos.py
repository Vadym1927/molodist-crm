"""Фото й документи пацієнтів за категоріями: згоди, обличчя, тіло, документи.
Категорію можна задати вручну (PatientFile.category); якщо не задано — визначається з типу файлу, підпису й зони."""
from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Patient, PatientFile

CATS = {"face": "Обличчя", "body": "Тіло", "consent": "Згоди", "doc": "Документи"}
_CONSENT = re.compile(r"згод|згід|consent|догов|анкет|інформ", re.I)
_DOC = re.compile(r"паспорт|документ|аналіз|довідк|висновок|рецепт|\.pdf$|\.docx?$|\.xlsx?$", re.I)
_BODY = re.compile(r"тіл|body|живіт|живот|ноги|нога|стегн|сідниц|спин|рук[аи]|плеч|груд|талі|колін|целюліт|пахв", re.I)
IMG = re.compile(r"\.(jpe?g|png|webp|gif|heic|bmp)$", re.I)


def is_image(f: PatientFile) -> bool:
    name = (f.path or "").lower()
    if IMG.search(name):
        return True
    if re.search(r"\.(pdf|docx?|xlsx?|txt|zip)$", name):
        return False
    return f.kind == "photo"


def guess(f: PatientFile) -> str:
    text = " ".join(x for x in (f.caption, f.zone, Path(f.path or "").name) if x)
    if _CONSENT.search(text):
        return "consent"
    if not is_image(f) or _DOC.search(text):
        return "doc"
    if _BODY.search(text):
        return "body"
    return "face"


def cat(f: PatientFile) -> str:
    return f.category if f.category in CATS else guess(f)


def all_files(db: Session, patient_id: int | None = None) -> list[PatientFile]:
    q = select(PatientFile).where(PatientFile.patient_id.is_not(None), PatientFile.kind != "plan_pdf")
    if patient_id:
        q = q.where(PatientFile.patient_id == patient_id)
    return db.scalars(q.order_by(PatientFile.created_at.desc(), PatientFile.id.desc())).all()


def library(db: Session, tab: str, q: str = "", page: int = 1, per: int = 96) -> dict:
    rows = all_files(db)
    names = {i: n for i, n in db.execute(select(Patient.id, Patient.full_name))}
    counts = {k: 0 for k in CATS}
    for f in rows:
        counts[cat(f)] += 1
    tab = tab if tab in CATS else "face"
    sel = [f for f in rows if cat(f) == tab]
    if q.strip():
        ql = q.strip().lower()
        from .search import match as _m
        sel = [f for f in sel if _m(ql, names.get(f.patient_id) or "", f.caption or "")]
    pages = max(1, -(-len(sel) // per))
    page = min(max(1, page), pages)
    return {"tab": tab, "counts": counts, "total": len(sel), "page": page, "pages": pages,
            "items": sel[(page - 1) * per: page * per], "names": names, "q": q}


def set_category(db: Session, ids: list[int], category: str) -> int:
    if category not in CATS:
        return 0
    n = 0
    for f in db.scalars(select(PatientFile).where(PatientFile.id.in_(ids or [0]))):
        f.category = category
        n += 1
    db.commit()
    return n


def delete(db: Session, ids: list[int]) -> list[tuple[int, int | None]]:
    """Видаляє файли з картки. Сам файл не стирається, а переноситься в «_trash» (його можна відновити з диска/бекапу)."""
    out = []
    trash = Path(settings.files_dir) / "_trash" / datetime.now().strftime("%Y-%m")
    for f in db.scalars(select(PatientFile).where(PatientFile.id.in_(ids or [0]), PatientFile.kind != "plan_pdf")).all():
        try:
            src = Path(f.path)
            if src.exists():
                trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(trash / f"{f.id}_{src.name}"))
        except OSError:
            pass
        out.append((f.id, f.patient_id))
        db.delete(f)
    db.commit()
    return out
