"""Персональні знижки пацієнтів (як у CleverBox): % на товари і % на послуги для конкретного пацієнта.
Загальні налаштування (SyncState personal_discount): назва знижки, автозастосування (інакше адміністратор обирає її в касі),
сумування з іншими знижками. Старе поле Patient.discount_pct — та сама знижка на все (якщо окремі % не задані)."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Patient, SyncState

KEY = "personal_discount"
DEFAULT = {"name": "Персональна знижка", "auto": True, "combine": False}


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        return {**DEFAULT, **(json.loads(st.value) if st and st.value else {})}
    except ValueError:
        return dict(DEFAULT)


def save_cfg(db: Session, name: str, auto: bool, combine: bool) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value = json.dumps({"name": (name or "").strip()[:100] or DEFAULT["name"], "auto": bool(auto), "combine": bool(combine)},
                          ensure_ascii=False)
    st.updated_at = datetime.utcnow()
    db.merge(st)
    db.commit()


def pcts(p: Patient | None) -> tuple[float, float]:
    """(% на товари, % на послуги)."""
    if not p:
        return 0.0, 0.0
    base = float(p.discount_pct or 0)
    prod = p.disc_products_pct if p.disc_products_pct is not None else base
    serv = p.disc_services_pct if p.disc_services_pct is not None else base
    return float(prod or 0), float(serv or 0)


def has(p: Patient | None) -> bool:
    return any(pcts(p))


def _clip(v) -> float | None:
    try:
        x = float(str(v).replace(",", ".").strip()) if str(v or "").strip() != "" else None
    except ValueError:
        return None
    return None if x is None else min(max(x, 0.0), 100.0)


def set_for(p: Patient, products, services) -> None:
    pr, sv = _clip(products) or 0.0, _clip(services) or 0.0
    p.disc_products_pct, p.disc_services_pct = pr, sv
    p.discount_pct = pr if pr == sv and pr else None        # стара «знижка на все» — лише коли однакові
    if not pr and not sv:
        p.disc_products_pct = p.disc_services_pct = p.discount_pct = None


def patients(db: Session) -> list[Patient]:
    q = select(Patient).where(or_(Patient.discount_pct > 0, Patient.disc_products_pct > 0, Patient.disc_services_pct > 0))
    return db.scalars(q.order_by(Patient.full_name)).all()
