"""Звернення: довідники (джерела, мітки, статуси, причини відмов і звернень), воронка, перетворення в пацієнта."""
from __future__ import annotations

import copy
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appeal, AppealNote, Patient, SyncState

KEY = "appeal_dirs"
STATUS_TYPES = {"new": "Новий", "work": "В роботі", "success": "Успіх (записаний)", "fail": "Відмова"}
DEFAULT = {
    "sources": ["Instagram", "Facebook", "Google", "Сайт", "Telegram-бот", "Viber", "Дзвінок", "Рекомендація", "Повторний пацієнт", "Інше"],
    "marks": ["Гаряча", "Тепла", "Холодна", "VIP", "Акція"],
    "statuses": [{"name": "Новий", "type": "new", "color": "#3B82C4"}, {"name": "В роботі", "type": "work", "color": "#E9A93A"},
                 {"name": "Передзвонити", "type": "work", "color": "#8e7cc3"}, {"name": "Записаний", "type": "success", "color": "#4CAF50"},
                 {"name": "Відмова", "type": "fail", "color": "#D9534F"}],
    "deny_reasons": ["Дорого", "Неактуально", "Не додзвонились", "Обрали іншу клініку", "Далеко", "Немає зручного часу", "Інше"],
    "causes": ["Консультація", "Запис на процедуру", "Ціна", "Акція", "Сертифікат", "Повторний візит", "Скарга", "Інше"],
}
DIR_LABELS = {"sources": "Джерела звернень", "marks": "Мітки звернень", "statuses": "Статуси звернень",
              "deny_reasons": "Причини відмов", "causes": "Причини звернень"}


def dirs(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    d = copy.deepcopy(DEFAULT)
    if st and st.value:
        try:
            d.update(json.loads(st.value))
        except ValueError:
            pass
    return d


def save_dirs(db: Session, d: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def status_type(d: dict, name: str) -> str:
    return next((s["type"] for s in d["statuses"] if s["name"] == name), "work")


def status_color(d: dict, name: str) -> str:
    return next((s.get("color") or "#888" for s in d["statuses"] if s["name"] == name), "#888")


def phone_key(p: str | None) -> str:
    return re.sub(r"\D", "", p or "")[-9:]


def find_patient(db: Session, phone: str | None) -> Patient | None:
    k = phone_key(phone)
    if len(k) < 9:
        return None
    for p in db.scalars(select(Patient).where(Patient.phone.is_not(None), Patient.phone.like(f"%{k[-1]}"))):
        if phone_key(p.phone) == k:
            return p
    return None


def note(db: Session, a: Appeal, text: str, user_id: int | None = None, kind: str = "note") -> None:
    db.add(AppealNote(appeal_id=a.id, text=text[:4000], user_id=user_id, kind=kind))


def create(db: Session, data: dict, user_id: int | None = None) -> Appeal:
    d = dirs(db)
    a = Appeal(name=(data.get("name") or "").strip()[:200] or None, phone=(data.get("phone") or "").strip()[:64] or None,
               source=(data.get("source") or "").strip()[:80] or None, cause=(data.get("cause") or "").strip()[:120] or None,
               interest=(data.get("interest") or "").strip()[:200] or None, comment=(data.get("comment") or "").strip() or None,
               status=data.get("status") or d["statuses"][0]["name"], utm=(data.get("utm") or "")[:300] or None,
               marks=[m for m in (data.get("marks") or []) if m] or None, created_by=user_id,
               responsible_id=data.get("responsible_id") or None, next_contact=data.get("next_contact"),
               amount=data.get("amount"))
    a.patient_id = data.get("patient_id") or (p.id if (p := find_patient(db, a.phone)) else None)
    db.add(a)
    db.flush()
    note(db, a, "Звернення створено" + (f" · джерело: {a.source}" if a.source else ""), user_id, "system")
    db.commit()
    return a


def set_status(db: Session, a: Appeal, status: str, user_id: int | None = None, deny_reason: str | None = None) -> None:
    d = dirs(db)
    if status == a.status and not deny_reason:
        return
    old = a.status
    a.status = status
    t = status_type(d, status)
    a.deny_reason = deny_reason if t == "fail" else None
    a.closed_at = datetime.utcnow() if t in ("success", "fail") else None
    a.updated_at = datetime.utcnow()
    note(db, a, f"Статус: {old} → {status}" + (f" ({deny_reason})" if t == "fail" and deny_reason else ""), user_id, "status")
    db.commit()


def to_patient(db: Session, a: Appeal) -> Patient:
    if a.patient_id:
        return db.get(Patient, a.patient_id)
    p = find_patient(db, a.phone)
    if not p:
        p = Patient(full_name=a.name or (a.phone or "Новий пацієнт"), phone=a.phone, source=a.source)
        db.add(p)
        db.flush()
    a.patient_id = p.id
    db.commit()
    return p


def listing(db: Session, f: dict) -> list[Appeal]:
    q = select(Appeal).where(Appeal.deleted_at.is_(None))
    if f.get("d_from"):
        q = q.where(Appeal.created_at >= datetime.combine(f["d_from"], datetime.min.time()))
    if f.get("d_to"):
        q = q.where(Appeal.created_at <= datetime.combine(f["d_to"], datetime.max.time()))
    for k in ("source", "status", "cause"):
        if f.get(k):
            q = q.where(getattr(Appeal, k) == f[k])
    if f.get("resp"):
        q = q.where(Appeal.responsible_id == int(f["resp"]))
    rows = db.scalars(q.order_by(Appeal.created_at.desc()).limit(3000)).all()
    if f.get("mark"):
        rows = [a for a in rows if f["mark"] in (a.marks or [])]
    if f.get("q"):
        ql, pk = f["q"].strip().lower(), phone_key(f["q"])
        rows = [a for a in rows if ql in (a.name or "").lower() or (len(pk) >= 4 and pk in re.sub(r"\D", "", a.phone or ""))
                or ql in (a.comment or "").lower() or ql == str(a.id)]
    if f.get("due"):
        now = datetime.now()
        rows = [a for a in rows if a.next_contact and a.next_contact <= now.replace(hour=23, minute=59) and not a.closed_at]
    return rows


def stats(db: Session, rows: list[Appeal]) -> dict:
    d = dirs(db)
    by_type = Counter(status_type(d, a.status) for a in rows)
    by_source = defaultdict(lambda: {"n": 0, "success": 0})
    for a in rows:
        s = by_source[a.source or "—"]
        s["n"] += 1
        s["success"] += status_type(d, a.status) == "success"
    closed = by_type["success"] + by_type["fail"]
    return {"n": len(rows), "by_type": by_type, "conv": round(100 * by_type["success"] / closed) if closed else None,
            "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1]["n"])),
            "deny": Counter(a.deny_reason for a in rows if a.deny_reason).most_common(8)}
