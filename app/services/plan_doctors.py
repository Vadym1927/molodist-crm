"""Лікарі для вибору в бюті-плані й картці пацієнта («Лікар пацієнта»): лише справжні лікарі, без адміністраторів,
лабораторій, службових записів (CRM, Financial Operations), масажистів тощо.

Список задає власник у «Налаштування → Лікарі бюті-планів» (SyncState plan_doctors — id лікарів).
Поки список не задано — автоматично: працюючі спеціалісти, у посаді яких є «лікар», «дерматолог», «косметолог» тощо."""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Doctor, SyncState

KEY = "plan_doctors"
MED_RE = re.compile(r"лікар|врач|дермат|косметол|трихол|хірург|гінек|венеро|терапевт|ендокрин", re.I)
NOT_RE = re.compile(r"адмін|директор|лаборат|online|онлайн|crm|срм|financial|диктатор|масаж|бухгалт|маркет|менеджер", re.I)


def saved(db: Session) -> list[int] | None:
    st = db.get(SyncState, KEY)
    try:
        v = json.loads(st.value) if st and st.value else None
    except ValueError:
        v = None
    return [int(x) for x in v] if isinstance(v, list) else None


def auto(d: Doctor) -> bool:
    text = f"{d.position or ''}"
    return bool(d.active) and bool(MED_RE.search(text)) and not NOT_RE.search(f"{d.name} {text}")


def ids(db: Session) -> set[int]:
    s = saved(db)
    if s is not None:
        return set(s)
    return {d.id for d in db.scalars(select(Doctor)) if auto(d)}


def options(db: Session, keep: int | None = None) -> list[Doctor]:
    """Лікарі для випадного списку; `keep` — поточний лікар плану/пацієнта лишається в списку, навіть якщо його немає серед лікарів."""
    want = ids(db) | ({keep} if keep else set())
    if not ids(db):                           # нічого не визначено — не ламаємо вибір
        return list(db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)))
    return list(db.scalars(select(Doctor).where(Doctor.id.in_(want)).order_by(Doctor.name)))


def save(db: Session, id_list: list[int]) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(sorted(set(id_list))), updated_at=datetime.utcnow()))
    db.commit()
