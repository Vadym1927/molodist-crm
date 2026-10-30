"""Категорії спеціалістів / цін (як у CleverBox): інші ціни на послуги для певних спеціалістів (головний лікар, топ-лікар, стажер).
SyncState price_cats: {on, base_name, cats:[{id, name, on}], staff:{doctor_id: cat_id}, prices:{cat_id: {service_id: price}}}."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Doctor, SyncState

KEY = "price_cats"


def get(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            d = json.loads(st.value)
            d.setdefault("cats", []); d.setdefault("staff", {}); d.setdefault("prices", {})
            return d
        except ValueError:
            pass
    d = {"on": False, "base_name": "", "cats": [{"id": 1, "name": "Головний лікар", "on": False}], "staff": {}, "prices": {}}
    for doc in db.scalars(select(Doctor)):              # з CleverBox: Колган Дар'я — «Головний лікар»
        if "колган" in (doc.name or "").lower():
            d["staff"][str(doc.id)] = 1
    _save(db, d)
    return d


def _save(db: Session, d: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def save_main(db: Session, on: bool, base_name: str, cats: list[tuple]) -> None:
    """cats: [(id|None, name, on)] у потрібному порядку; відсутні — видаляються разом із цінами й призначеннями."""
    d = get(db)
    nid = max([c["id"] for c in d["cats"]] + [0])
    out = []
    for cid, name, con in cats:
        name = " ".join((name or "").split())[:80]
        if not name:
            continue
        if not cid:
            nid += 1
            cid = nid
        out.append({"id": int(cid), "name": name, "on": bool(con)})
    keep = {str(c["id"]) for c in out}
    d.update(on=on, base_name=base_name.strip()[:80], cats=out)
    d["staff"] = {k: v for k, v in d["staff"].items() if str(v) in keep}
    d["prices"] = {k: v for k, v in d["prices"].items() if k in keep}
    _save(db, d)


def save_staff(db: Session, staff: dict) -> None:
    d = get(db)
    ids = {c["id"] for c in d["cats"]}
    d["staff"] = {str(k): int(v) for k, v in staff.items() if v and int(v) in ids}
    _save(db, d)


def save_prices(db: Session, prices: dict) -> None:
    d = get(db)
    d["prices"] = {str(c): {str(s): p for s, p in v.items() if p is not None} for c, v in prices.items()}
    _save(db, d)


def doctor_prices(db: Session) -> dict:
    """doctor_id → {service_id: ціна} для каси (лише коли модуль і категорія увімкнені)."""
    d = get(db)
    if not d.get("on"):
        return {}
    on = {str(c["id"]) for c in d["cats"] if c.get("on")}
    return {int(did): d["prices"].get(str(cid), {}) for did, cid in d["staff"].items() if str(cid) in on and d["prices"].get(str(cid))}
