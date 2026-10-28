"""Зіставлення пацієнтів за ПІБ (PDF бюті-плану ↔ картка CleverBox) і об'єднання дублів.

У PDF OBSERV зазвичай «Імʼя Прізвище», у CleverBox — «Прізвище Імʼя По батькові». Тому:
- 100 — ті самі слова в будь-якому порядку;
- 90  — усі слова з PDF є в картці (у картці ще по батькові) — достатньо для автопривʼязки, якщо збіг один;
- 60–85 — схоже з одруківкою (Фабаровська / Фаборовська) — лише як підказка, без автопривʼязки."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import (Patient, BeautyPlan, Visit, ProductSale, PatientFile, StockDoc, Check, Task,
                      CampaignRecipient, MessageLog, StockMove, GiftCertificate)
from ..parsers.observ import norm

AUTO = 90


def _tokens(name: str | None) -> list[str]:
    return [t for t in norm(name or "").split() if len(t) > 1]


def score(query: str, candidate: str) -> int:
    q, c = _tokens(query), _tokens(candidate)
    if len(q) < 2 or not c:
        return 0
    qs, cs = set(q), set(c)
    if qs == cs:
        return 100
    if qs <= cs:
        return AUTO
    best = []
    for t in q:
        r = max((SequenceMatcher(None, t, x).ratio() for x in c), default=0)
        best.append(r)
    if min(best) < 0.75:
        return 0
    return int(50 + 35 * sum(best) / len(best))          # 76…85


def digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")[-9:]


def best_match(db: Session, name: str, phone: str | None = None, exclude_id: int | None = None) -> Patient | None:
    """Автопривʼязка: телефон, або один-єдиний пацієнт зі збігом ≥ 90 (перевага — картки з CleverBox)."""
    if phone and len(digits(phone)) == 9:
        d = digits(phone)
        for p in db.scalars(select(Patient).where(Patient.phone.is_not(None))):
            if p.id != exclude_id and digits(p.phone) == d:
                return p
    hits = [(score(name, p.full_name), p) for p in db.scalars(select(Patient)) if p.id != exclude_id]
    top = [p for s, p in hits if s >= AUTO]
    exact = [p for s, p in hits if s == 100]
    pool = exact or top
    if len(pool) > 1:
        linked = [p for p in pool if p.cleverbox_id]
        pool = linked if len(linked) == 1 else pool
    return pool[0] if len(pool) == 1 else None


def suggestions(db: Session, patient: Patient, limit: int = 5) -> list[tuple[int, Patient]]:
    """Схожі картки (для «CB?» — ймовірні дублікати з CleverBox)."""
    out = []
    for p in db.scalars(select(Patient).where(Patient.id != patient.id)):
        s = score(patient.full_name, p.full_name) or score(p.full_name, patient.full_name)
        if s:
            out.append((s + (3 if p.cleverbox_id else 0), p))
    out.sort(key=lambda x: (-x[0], x[1].full_name))
    return [(min(s, 100), p) for s, p in out[:limit]]


_COPY = ("phone", "doctor_id", "notes", "birthday", "email", "gender", "address", "source", "telegram_chat_id",
         "telegram_username", "telegram_linked_at")


def merge(db: Session, src: Patient, dst: Patient) -> dict:
    """Переносить усе з src (дубль) у dst і видаляє src. Порожні поля dst доповнюються з src."""
    if src.id == dst.id:
        raise ValueError("Це та сама картка")
    moved = {}
    for model in (BeautyPlan, Visit, ProductSale, PatientFile, StockDoc, Check, Task, CampaignRecipient, MessageLog, StockMove, GiftCertificate):
        r = db.execute(update(model).where(model.patient_id == src.id).values(patient_id=dst.id))
        if r.rowcount:
            moved[model.__tablename__] = r.rowcount
    for f in _COPY:
        if getattr(dst, f, None) in (None, "") and getattr(src, f, None) not in (None, ""):
            v = getattr(src, f)
            if f == "telegram_chat_id":
                src.telegram_chat_id = None
                db.flush()
            setattr(dst, f, v)
    if src.notes and dst.notes and src.notes not in dst.notes:
        dst.notes = dst.notes + "\n" + src.notes
    if src.cleverbox_id and not dst.cleverbox_id:
        cid, src.cleverbox_id = src.cleverbox_id, None
        db.flush()
        dst.cleverbox_id = cid
    db.flush()
    db.expire(src)
    db.delete(db.get(Patient, src.id))
    db.commit()
    return moved
