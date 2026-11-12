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
        m.extra = {"source": "cleverbox", "fields": fields, "files": r.get("files") or None, "cb_type": _val(r.get("_type")),
                   "cb_client_id": r.get("client_id")}
    c = _cfg(db)
    c["last"] = {"at": datetime.now().isoformat(timespec="minutes"), **{k: v for k, v in out.items() if k != "no_patient_ids"}}
    db.merge(SyncState(key=KEY, value=json.dumps(c, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    return out
