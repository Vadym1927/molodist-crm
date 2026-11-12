"""Перенесення медичних карток з CleverBox у CRM (записи консультацій, нотатки, алергії, аналізи, щеплення).

У публічному API CleverBox медкарти немає, тож дані читає сторінка CleverBox у браузері власника (його вхід, лише читання:
medcard listAll / getFields) і надсилає сюди пачками з тимчасовим ключем (3 год, лише власник). Повторне надсилання
оновлює ті самі записи (cb_id), видалені в CleverBox — не переносяться. Медичні дані не виходять за межі CRM.
"""
from __future__ import annotations

import hmac
import json
import secrets
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Doctor, MedRecord, Patient, SyncState

KEY = "med_import"
ORIGIN = "https://molodist.cbox.online"
KIND = {"consult": "consultation", "note": "note", "allergy": "allergy", "analize": "analysis", "analysis": "analysis",
        "vaccine": "vaccine", "vacine": "vaccine"}
SYS = {"anamnes": "anamnesis", "anamnesis": "anamnesis", "pain": "complaints", "complaints": "complaints", "real": "objective",
       "final": "recommendations", "diagnosis": "diagnosis", "diagnos": "diagnosis", "diag": "diagnosis"}
META = {"id", "date_add", "client_id", "salon_id", "user_id", "type", "form_id", "tags", "deleted", "doctor", "date", "time", "author",
        "_type", "_ctype", "files", "_doctor", "_salon", "client", "fields", "subtitle", "title", "vacine", "way", "img"}


def _cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else {}
    except ValueError:
        return {}


def _save(db: Session, c: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(c, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def new_key(db: Session) -> str:
    c = _cfg(db)
    c["key"], c["until"] = secrets.token_urlsafe(24), int(time.time()) + 3 * 3600
    _save(db, c)
    return c["key"]


def key_ok(db: Session, key: str) -> bool:
    c = _cfg(db)
    return bool(key and c.get("key")) and time.time() < float(c.get("until") or 0) and hmac.compare_digest(key, c["key"])


def status(db: Session) -> dict:
    c = _cfg(db)
    n = db.query(MedRecord).filter(MedRecord.cb_id.is_not(None), MedRecord.deleted_at.is_(None)).count()
    pats = db.query(MedRecord.patient_id).filter(MedRecord.cb_id.is_not(None)).distinct().count()
    return {"records": n, "patients": pats, "last": c.get("last"), "key_active": time.time() < float(c.get("until") or 0)}


def _val(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False) if v else ""
    return str(v).strip()


def _at(r: dict) -> datetime:
    d, t = str(r.get("date") or ""), str(r.get("time") or "") or "00:00"
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{d} {t}".strip(), fmt)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(int(r.get("date_add") or 0)) if r.get("date_add") else datetime.now()
    except (TypeError, ValueError, OSError):
        return datetime.now()


def ingest(db: Session, rows: list, forms: dict | None = None) -> dict:
    """rows — записи з medcard listAll; forms — {form_id або type: [{name, title}]} з getFields (назви полів)."""
    from ..integrations.cleverbox import match_doctor
    forms = forms or {}
    pats = {p.cleverbox_id: p.id for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    doctors = db.scalars(select(Doctor)).all()
    have = {m.cb_id: m for m in db.scalars(select(MedRecord).where(MedRecord.cb_id.in_(
        [int(r["id"]) for r in rows if isinstance(r, dict) and str(r.get("id") or "").isdigit()] or [0])))}
    out = {"new": 0, "updated": 0, "skipped_deleted": 0, "no_patient": 0, "no_patient_ids": []}
    for r in rows or []:
        if not isinstance(r, dict) or not str(r.get("id") or "").isdigit():
            continue
        cid = int(r["id"])
        if str(r.get("deleted") or "0") not in ("0", "", "False", "false"):
            out["skipped_deleted"] += 1
            continue
        try:
            pid = pats.get(int(r.get("client_id") or 0))
        except (TypeError, ValueError):
            pid = None
        if not pid:
            out["no_patient"] += 1
            if len(out["no_patient_ids"]) < 50:
                out["no_patient_ids"].append(r.get("client_id"))
            continue
        defs = forms.get(str(r.get("form_id") or "")) or forms.get(str(r.get("type") or "")) or []
        titles = {d.get("name"): d.get("title") for d in defs if isinstance(d, dict)}
        order = [d.get("name") for d in defs if isinstance(d, dict)]
        keys = order + [k for k in r if k not in order and k not in META]
        mapped: dict = {}
        fields, proto = [], []
        for k in keys:
            v = _val(r.get(k))
            if not v:
                continue
            t = (titles.get(k) or k).strip().rstrip(":").strip() or k
            fields.append({"t": t, "v": v})
            col = SYS.get(k)
            if col:
                mapped[col] = (mapped.get(col, "") + ("\n" if mapped.get(col) else "") + v)
            else:
                proto.append(f"{t}: {v}")
        kind = KIND.get(str(r.get("type") or ""), "note")
        if kind == "allergy" and r.get("title"):
            fields.insert(0, {"t": "Алергія", "v": _val(r.get("title"))})
            proto.insert(0, f"Алергія: {_val(r.get('title'))}")
        if kind == "vaccine" and r.get("vacine"):
            proto.append("Щеплення: " + _val(r.get("vacine")))
        auth = (r.get("_doctor") or r.get("author") or {})
        auth_name = (auth.get("title") if isinstance(auth, dict) else "") or ""
        doc = match_doctor(auth_name, doctors) if auth_name else None
        form_title = _val(r.get("_ctype")) or _val(r.get("_type"))
        m = have.get(cid)
        if not m:
            m = MedRecord(patient_id=pid, cb_id=cid, created_at=datetime.utcnow())
            db.add(m)
            have[cid] = m
            out["new"] += 1
        else:
            m.updated_at = datetime.utcnow()
            out["updated"] += 1
        m.patient_id, m.at, m.kind = pid, _at(r), kind
        m.title = (_val(r.get("subtitle")) or (_val(r.get("title")) if kind != "allergy" else "") or form_title or None)
        if m.title:
            m.title = m.title[:200]
        m.form_title, m.author = (form_title[:200] or None), (auth_name[:120] or None)
        m.doctor_id = doc.id if doc else m.doctor_id
        for col in ("complaints", "anamnesis", "objective", "diagnosis", "recommendations"):
            setattr(m, col, mapped.get(col) or None)
        m.protocol = "\n".join(proto) or None
        m.extra = {"source": "cleverbox", "fields": fields, "cb_form_id": r.get("form_id"), "files": r.get("files") or None, "cb_type": _val(r.get("_type")),
                   "cb_client_id": r.get("client_id")}
    c = _cfg(db)
    c["last"] = {"at": datetime.now().isoformat(timespec="minutes"), **{k: v for k, v in out.items() if k != "no_patient_ids"}}
    db.merge(SyncState(key=KEY, value=json.dumps(c, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    return out


def run_files(job, files: list) -> dict:
    """Файли записів медкарти: [[cb_record_id, cb_file_id, url, назва, дата, тип]] → файли пацієнта, привʼязані до запису."""
    from . import cb_photos
    from ..models import PatientFile, SessionLocal
    with SessionLocal() as db:
        recs = {m.cb_id: (m.id, m.patient_id) for m in db.scalars(select(MedRecord).where(MedRecord.cb_id.is_not(None)))}
        cbid = {p.id: p.cleverbox_id for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    conv, link = [], {}
    for f in files or []:
        try:
            rid, fid = int(f[0]), int(f[1])
        except (TypeError, ValueError, IndexError):
            continue
        if rid not in recs or recs[rid][1] not in cbid:
            continue
        link[fid] = recs[rid][0]
        conv.append([cbid[recs[rid][1]], fid, f[2], f[3], f[4] if len(f) > 4 else "", f[5] if len(f) > 5 else ""])
    rep = cb_photos.run(job, conv, "client")
    with SessionLocal() as db:
        n = 0
        for pf in db.scalars(select(PatientFile).where(PatientFile.cb_file_id.in_(list(link) or [0]))):
            if pf.record_id != link[pf.cb_file_id]:
                pf.record_id = link[pf.cb_file_id]
                n += 1
        db.commit()
    rep["linked"] = n
    job.log(f"Привʼязано до записів медкарти: {n}")
    return rep


def import_forms(db: Session, forms: list) -> dict:
    """Види форм CleverBox [{id, title, cname, fields:[{name, title, type}]}] → MedForm (оновлює за cb_form_id)."""
    from ..models import MedForm
    have = {f.cb_form_id: f for f in db.scalars(select(MedForm).where(MedForm.cb_form_id.is_not(None)))}
    n_new = n_upd = 0
    for i, f in enumerate(forms or []):
        try:
            fid = int(f.get("id"))
        except (TypeError, ValueError):
            continue
        flds = [{"name": str(x.get("name") or "")[:40], "title": (str(x.get("title") or "").strip().rstrip(":").strip() or str(x.get("name")))[:200],
                 "type": "textarea" if x.get("type") == "textarea" else "text"} for x in (f.get("fields") or []) if isinstance(x, dict) and x.get("name")]
        m = have.get(fid)
        if not m:
            m = MedForm(cb_form_id=fid)
            db.add(m)
            n_new += 1
        else:
            n_upd += 1
        m.name = str(f.get("title") or f"Форма {fid}")[:200]
        m.kind = KIND.get(str(f.get("cname") or "consult"), "consultation")
        m.fields, m.sort = flds, i
    db.commit()
    return {"new": n_new, "updated": n_upd}


PRINT_KEY = "med_print"
PRINT_DEFAULT = {"brand": "MOLODIST", "subtitle": "Медичний простір естетики та профілактики",
                 "legal": "Клініка естетичної та профілактичної медицини", "address": "м. Київ, вул. Романа Мстиславича, 28А",
                 "license": "Ліцензія МОЗ України, наказ №1769 від 17.10.2024", "contacts": "+380 93 375 64 37 · molodist.com.ua",
                 "title": "Консультативний висновок спеціаліста", "use_form_title": True, "show_birthday": True, "show_phone": False,
                 "show_allergies": True, "show_position": True, "sign_doctor": "Лікар", "sign_patient": "З висновком ознайомлений(а)",
                 "stamp": True, "footer": "Висновок не є підставою для лікарняного. Консультація не замінює очного огляду в разі погіршення стану."}


def print_cfg(db: Session) -> dict:
    st = db.get(SyncState, PRINT_KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {**PRINT_DEFAULT, **d}


def save_print_cfg(db: Session, d: dict) -> None:
    db.merge(SyncState(key=PRINT_KEY, value=json.dumps(d, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


AN_KEY = "med_analyses"


def analyses(db: Session) -> list:
    """Шаблони аналізів: [{name, items: [{n, norm, unit}]}] (Налаштування → Медкартка → Аналізи)."""
    st = db.get(SyncState, AN_KEY)
    try:
        d = json.loads(st.value) if st and st.value else []
    except ValueError:
        d = []
    return d if isinstance(d, list) else []


def save_analyses(db: Session, items: list) -> None:
    db.merge(SyncState(key=AN_KEY, value=json.dumps(items, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def parse_an_lines(text: str) -> list:
    """«Гемоглобін | 120–160 | г/л» → {n, norm, unit}."""
    out = []
    for line in (text or "").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if parts and parts[0]:
            out.append({"n": parts[0][:120], "norm": (parts[1] if len(parts) > 1 else "")[:60], "unit": (parts[2] if len(parts) > 2 else "")[:30]})
    return out


AL_KEY = "med_allergens"


def allergens(db: Session) -> list:
    """Шаблони алергій: [{n, reaction}] (Налаштування → Медкартка → Алергії)."""
    st = db.get(SyncState, AL_KEY)
    try:
        d = json.loads(st.value) if st and st.value else []
    except ValueError:
        d = []
    return d if isinstance(d, list) else []


def save_allergens(db: Session, text: str) -> list:
    out, seen = [], set()
    for line in (text or "").splitlines():
        parts = [x.strip() for x in line.split("|")]
        if parts and parts[0] and parts[0].lower() not in seen:
            seen.add(parts[0].lower())
            out.append({"n": parts[0][:120], "reaction": (parts[1] if len(parts) > 1 else "")[:200]})
    db.merge(SyncState(key=AL_KEY, value=json.dumps(out, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    return out
