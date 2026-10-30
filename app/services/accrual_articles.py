"""Статті для нарахувань (як у CleverBox): премії, штрафи, лікарняні, податки… для коригувань зарплати.
SyncState accrual_articles: [{id, name, kind income|expense, on}]. income — додається до нарахування, expense — віднімається."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "accrual_articles"
KINDS = {"income": "Дохід", "expense": "Витрата"}
SEED = [("Премія", "income", True), ("Штраф", "expense", True), ("Лікарняні", "income", True), ("Податки", "expense", True),
        ("Відпускні", "income", True), ("Зарплата", "income", True), ("Виплата прибутку в ЗП", "income", False)]


def items(db: Session) -> list[dict]:
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            return json.loads(st.value)
        except ValueError:
            pass
    xs = [{"id": i, "name": n, "kind": k, "on": on} for i, (n, k, on) in enumerate(SEED, 1)]
    _save(db, xs)
    return xs


def _save(db: Session, xs: list[dict]) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(xs, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def active(db: Session) -> list[dict]:
    return [x for x in items(db) if x.get("on", True)]


def get(db: Session, aid: int) -> dict | None:
    return next((x for x in items(db) if x["id"] == aid), None)


def save(db: Session, aid: int | None, name: str, kind: str) -> str | None:
    name = " ".join((name or "").split())[:80]
    if not name:
        return "Вкажіть назву статті"
    kind = kind if kind in KINDS else "income"
    xs = items(db)
    if any(x["name"].lower() == name.lower() and x["id"] != aid for x in xs):
        return "Така стаття вже є"
    x = next((x for x in xs if aid and x["id"] == aid), None)
    if x:
        x.update(name=name, kind=kind)
    else:
        xs.append({"id": max([y["id"] for y in xs] + [0]) + 1, "name": name, "kind": kind, "on": True})
    _save(db, xs)
    return None


def toggle(db: Session, aid: int) -> None:
    xs = items(db)
    for x in xs:
        if x["id"] == aid:
            x["on"] = not x.get("on", True)
    _save(db, xs)


def reorder(db: Session, ids: list[int]) -> None:
    xs = items(db)
    pos = {i: n for n, i in enumerate(ids)}
    xs.sort(key=lambda x: pos.get(x["id"], 10_000))
    _save(db, xs)
