"""Масове рознесення документів пацієнтів з архіву (медичні висновки, бюті-плани, інша документація).

Файли підписані пацієнтом у назві: телефон (10–12 цифр, напр. «380671234567__012__Прізвище Імʼя.pdf») або номер
(№ клієнта CleverBox, інакше № картки CRM) — у назві файлу чи папки. Кожен PDF пробуємо прочитати як бюті-план OBSERV:
вийшло → створюється план (пацієнт — за телефоном/номером, а не за ПІБ у PDF); ні → документ картки («Документи»;
анкети/згоди → «Згоди»); фото → «Обличчя». Повторне завантаження того ж файлу
(той самий пацієнт + назва + розмір) не дублює. Спершу — перегляд (dry run), запис лише після підтвердження."""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Patient, PatientFile
from ..parsers.observ import ObservParseError, parse_observ_pdf

IMG = re.compile(r"\.(jpe?g|png|webp|heic|gif|bmp|tiff?)$", re.I)
DOC = re.compile(r"\.(pdf|docx?|xlsx?|txt|rtf)$", re.I)
SKIP = re.compile(r"(^|/)(__MACOSX|\.DS_Store|Thumbs\.db|\._)", re.I)


def _phone(name: str) -> str | None:
    m = re.search(r"(?<!\d)(\d{10,12})(?!\d)", name)
    return m.group(1)[-9:] if m else None


def _num(name: str) -> int | None:
    m = re.search(r"(?<!\d)(\d{1,6})(?!\d)", name)
    return int(m.group(1)) if m else None


def _patient_key(path: str) -> tuple[str | None, int | None, str]:
    """Ключ пацієнта: телефон або номер — з назви файлу, інакше з найближчої папки. (телефон, номер, звідки)."""
    p = PurePosixPath(path)
    for part, src in [(p.stem, "file")] + [(x, "folder") for x in reversed(p.parts[:-1])]:
        ph = _phone(part)
        if ph:
            return ph, None, src
        n = _num(part)
        if n is not None:
            return None, n, src
    return None, None, ""


def _kind(name: str, group: str) -> str:
    low = name.lower()
    if group == "plans" or "beautyplan" in low.replace(" ", "") or "бюті" in low or "beauty plan" in low:
        return "plan"
    return "doc" if DOC.search(low) or not IMG.search(low) else "photo"


def _category(name: str, group: str, kind: str) -> str:
    low = name.lower()
    if group == "medical" or re.search(r"висновок|заключ|аналіз|довідк|рецепт|узд|мрт", low):
        return "doc"
    if re.search(r"згод|consent|анкет|догов", low):
        return "consent"
    return "doc" if kind == "doc" else "face"


def scan(db: Session, archives: list[tuple[str, bytes]], group: str = "other") -> dict:
    """Перегляд: кожен файл → пацієнт, тип (план / документ / фото), категорія; без запису.
    group: medical | plans | other — підказка з якого архіву (впливає на категорію/тип)."""
    by_cb = {p.cleverbox_id: p for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    by_id = {p.id: p for p in db.scalars(select(Patient))}
    by_phone: dict[str, list] = {}
    for p in by_id.values():
        digits = re.sub(r"\D", "", p.phone or "")
        if len(digits) >= 9:
            by_phone.setdefault(digits[-9:], []).append(p)
    have = {(f.patient_id, (f.caption or "").lower()) for f in db.scalars(select(PatientFile).where(PatientFile.patient_id.is_not(None)))}
    rows, no_patient, dup = [], 0, 0
    for arc_name, data in archives:
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            rows.append({"archive": arc_name, "path": arc_name, "error": "не zip-архів"})
            continue
        for info in zf.infolist():
            if info.is_dir() or SKIP.search(info.filename) or info.file_size == 0:
                continue
            fname = PurePosixPath(info.filename).name
            try:
                fname_fixed = fname.encode("cp437").decode("cp866") if info.flag_bits & 0x800 == 0 else fname
            except (UnicodeEncodeError, UnicodeDecodeError):
                fname_fixed = fname
            phone, num, src = _patient_key(info.filename if info.flag_bits & 0x800 else fname_fixed)
            kind = _kind(fname_fixed, group)
            pat, via = None, ""
            if phone:
                hits = by_phone.get(phone) or []
                if len(hits) == 1:
                    pat, via = hits[0], "phone"
                elif len(hits) > 1:
                    via = "ambiguous"
            elif num is not None:
                pat = by_cb.get(num)
                via = "cb" if pat else ""
                if pat is None and num in by_id:
                    pat, via = by_id[num], "crm"
            r = {"archive": arc_name, "path": info.filename, "name": fname_fixed, "size": info.file_size,
                 "num": phone or (num if num is not None else None), "num_src": src,
                 "kind": kind, "category": _category(fname_fixed, group, kind) if kind != "plan" else "plan",
                 "patient_id": pat.id if pat else None, "patient": pat.full_name if pat else None, "via": via}
            if pat is None:
                no_patient += 1
            elif (pat.id, fname_fixed.lower()) in have:
                r["dup"] = True
                dup += 1
            rows.append(r)
    return {"rows": rows, "total": len(rows), "no_patient": no_patient, "dup": dup,
            "plans": sum(1 for r in rows if r.get("kind") == "plan"), "docs": sum(1 for r in rows if r.get("kind") == "doc"),
            "photos": sum(1 for r in rows if r.get("kind") == "photo"),
            "patients": len({r["patient_id"] for r in rows if r.get("patient_id")})}


def apply(db: Session, archives: list[tuple[str, bytes]], group: str = "other", overrides: dict[str, int] | None = None,
          user_id: int | None = None) -> dict:
    """Запис: плани → import_plan з пацієнтом за номером; документи/фото → PatientFile. overrides: {path: patient_id}."""
    from . import ingest
    from .plans import import_plan
    overrides = overrides or {}
    plan_ = scan(db, archives, group)
    rep = {"plans": 0, "plans_failed": [], "files": 0, "skipped_dup": 0, "no_patient": 0, "patients": set()}
    zfs = {name: zipfile.ZipFile(io.BytesIO(data)) for name, data in archives}
    for r in plan_["rows"]:
        if r.get("error"):
            continue
        pid = overrides.get(r["path"]) or r.get("patient_id")
        if not pid:
            rep["no_patient"] += 1
            continue
        if r.get("dup") and r["path"] not in overrides:
            rep["skipped_dup"] += 1
            continue
        pat = db.get(Patient, pid)
        data = zfs[r["archive"]].read(r["path"])
        path = ingest.store(data, r["name"])
        if r["name"].lower().endswith(".pdf"):
            try:
                parsed = parse_observ_pdf(path)
            except (ObservParseError, Exception) as e:          # не OBSERV-план → документ картки
                parsed = None
                if r["kind"] == "plan":
                    rep["plans_failed"].append({"name": r["name"], "patient": pat.full_name, "error": str(e)[:160]})
            if parsed is not None:
                plan = import_plan(db, parsed, source_file=r["name"], patient=pat)
                plan.source_file = path
                db.add(PatientFile(patient_id=pat.id, kind="plan_pdf", path=path, caption=r["name"]))
                rep["plans"] += 1
            else:
                db.add(PatientFile(patient_id=pat.id, kind="doc", path=path, caption=r["name"], category=r["category"] if r["kind"] != "plan" else "doc"))
                rep["files"] += 1
        else:
            db.add(PatientFile(patient_id=pat.id, kind=("photo" if r["kind"] == "photo" else "doc"), path=path, caption=r["name"],
                               category=r["category"]))
            rep["files"] += 1
        rep["patients"].add(pat.id)
        db.commit()
    rep["patients"] = len(rep["patients"])
    return rep
