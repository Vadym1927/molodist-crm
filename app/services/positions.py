"""Посади співробітників (як у CleverBox «Посади співробітників»): довідник для картки спеціаліста.
SyncState staff_positions: [{id, name, on}] — порядок списку = порядок у виборі; on=False — приховано у виборі."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Doctor, SyncState

KEY = "staff_positions"
SEED = ["Директор", "Старший адміністратор", "Адміністратор", "Маркетолог", "Прибиральниця", "Адміністратор системи",
        "Масажист-реабілітолог", "Лікар дерматолог, косметолог", "Лікар дерматолог, дерматоонколог, дерматохірург",
        "Лікар-косметолог", "Лабораторія"]


def items(db: Session) -> list[dict]:
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            return json.loads(st.value)
        except ValueError:
            pass
    names = list(SEED)
    for p in db.scalars(select(Doctor.position).where(Doctor.position.is_not(None)).distinct()):
        p = (p or "").strip()
        if p and p.lower() not in {n.lower() for n in names}:
            names.append(p)
    xs = [{"id": i + 1, "name": n, "on": True} for i, n in enumerate(names)]
    _save(db, xs)
    return xs


def _save(db: Session, xs: list[dict]) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(xs, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def names(db: Session) -> list[str]:
    return [x["name"] for x in items(db) if x.get("on", True)]


def usage(db: Session) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in db.scalars(select(Doctor.position).where(Doctor.position.is_not(None))):
        out[p] = out.get(p, 0) + 1
    return out


def save(db: Session, pid: int | None, name: str) -> str | None:
    name = " ".join((name or "").split())[:120]
    if not name:
        return "Вкажіть назву посади"
    xs = items(db)
    if any(x["name"].lower() == name.lower() and x["id"] != pid for x in xs):
        return "Така посада вже є"
    x = next((x for x in xs if pid and x["id"] == pid), None)
    if x:
        old = x["name"]
        x["name"] = name
        if old != name:                                   # перейменування — і в картках спеціалістів
            for d in db.scalars(select(Doctor).where(Doctor.position == old)):
                d.position = name
    else:
        xs.append({"id": max([y["id"] for y in xs] + [0]) + 1, "name": name, "on": True})
    _save(db, xs)
    return None


def toggle(db: Session, pid: int) -> None:
    xs = items(db)
    for x in xs:
        if x["id"] == pid:
            x["on"] = not x.get("on", True)
    _save(db, xs)


def delete(db: Session, pid: int) -> None:
    _save(db, [x for x in items(db) if x["id"] != pid])


def reorder(db: Session, ids: list[int]) -> None:
    xs = items(db)
    pos = {i: n for n, i in enumerate(ids)}
    xs.sort(key=lambda x: pos.get(x["id"], 10_000))
    _save(db, xs)
