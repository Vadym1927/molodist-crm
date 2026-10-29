"""Каси / рахунки — місця, де зберігаються гроші: готівка, рахунки ФОП, термінал, онлайн-оплати.
Налаштування — SyncState cashboxes (назва, локація, тип, способи оплати з каси CRM, стартовий залишок і дата).
Залишок = стартовий + надходження − витрати ± переміщення (фіноперації CRM) + оплати продажів CRM цими способами
(повернені чеки не рахуються) — змінюється після кожної операції."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FinOp, Sale, SalePayment, SyncState

KEY = "cashboxes"
TYPES = {"cash": "Готівка", "bank": "Банківський рахунок", "card": "Термінал (картка)", "online": "Онлайн-оплати", "other": "Інше"}
COLORS = ["#7FE3E8", "#5CB85C", "#3A3F44", "#E040C8", "#3B5BDB", "#4C3FB8", "#F5E06E", "#F0883E", "#9B59B6", "#2ECC71"]


def _guess_type(name: str) -> str:
    n = name.lower()
    if "готів" in n or "налич" in n:
        return "cash"
    if "термінал" in n or "картк" in n or "pos" in n:
        return "card"
    if "онлайн" in n or "еквайр" in n:
        return "online"
    return "bank"


def _defaults(db: Session) -> list[dict]:
    """Каси з журналу CleverBox (ті самі назви, що й там); якщо журналу ще немає — базовий набір."""
    from ..integrations.checkbox import accounts
    from ..models import CbFinOp
    cb = sorted({a for (a,) in db.execute(select(CbFinOp.account).where(CbFinOp.account.is_not(None)).distinct()) if a} |
                {a for (a,) in db.execute(select(CbFinOp.vid).where(CbFinOp.kind == "transfer", CbFinOp.vid.is_not(None)).distinct()) if a})
    if cb:
        out = [{"id": i + 1, "name": n, "type": _guess_type(n), "methods": ["Готівка"] if n == "Готівка" else []} for i, n in enumerate(cb)]
        if "Онлайн-оплати" not in cb:
            out.append({"id": len(out) + 1, "name": "Онлайн-оплати", "type": "online", "methods": []})
        loc = _first_location(db)
        for i, x in enumerate(out):
            x.update(location_id=loc, active=True, opening=0.0, opening_date=None, note="", color=COLORS[i % len(COLORS)])
        return out
    out = [{"id": 1, "name": "Готівка", "type": "cash", "methods": ["Готівка"]},
           {"id": 2, "name": "Термінал", "type": "card", "methods": ["Картка (термінал)"]}]
    for a in accounts(db):
        out.append({"id": len(out) + 1, "name": f"ФОП {a['name']}", "type": "bank", "methods": [f"ФОП {a['name']} — рахунок"]})
    out.append({"id": len(out) + 1, "name": "Онлайн-оплати", "type": "online", "methods": []})
    loc = _first_location(db)
    for i, x in enumerate(out):
        x.update(location_id=loc, active=True, opening=0.0, opening_date=None, note="", color=COLORS[i % len(COLORS)])
    return out


def _first_location(db: Session) -> int | None:
    from . import clinic
    locs = clinic.get(db).get("locations") or []
    return int(locs[0]["id"]) if locs else None


def items(db: Session) -> list[dict]:
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            return json.loads(st.value)
        except ValueError:
            pass
    return _defaults(db)


def names(db: Session, active_only: bool = True) -> list[str]:
    return [x["name"] for x in items(db) if not x.get("deleted") and (x.get("active") or not active_only)]


def _save(db: Session, xs: list[dict]) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(xs, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def save(db: Session, bid: int | None, data: dict) -> dict:
    xs = items(db)
    name = str(data.get("name") or "").strip()[:100]
    if not name:
        raise ValueError("Вкажіть назву каси")
    if any(x["name"].lower() == name.lower() and int(x["id"]) != int(bid or 0) for x in xs):
        raise ValueError("Каса з такою назвою вже є")
    if data.get("type") not in TYPES:
        raise ValueError("Оберіть тип")
    x = next((x for x in xs if bid and int(x["id"]) == int(bid)), None)
    if not x:
        x = {"id": max([int(y["id"]) for y in xs] + [0]) + 1}
        xs.append(x)
    old = x.get("name")
    taken = {m for y in xs if y is not x for m in y.get("methods") or []}
    methods = [m for m in data.get("methods") or [] if m]
    clash = [m for m in methods if m in taken]
    if clash:
        raise ValueError("Спосіб оплати вже привʼязаний до іншої каси: " + ", ".join(clash))
    x.update(name=name, type=data["type"], location_id=data.get("location_id") or None, active=bool(data.get("active")),
             opening=round(float(data.get("opening") or 0), 2), opening_date=data.get("opening_date") or None,
             methods=methods, note=str(data.get("note") or "")[:300], color=data.get("color") or x.get("color") or COLORS[0],
             deleted=False)
    _save(db, xs)
    if old and old != name:                                    # перейменування — переносимо історію операцій
        for op in db.scalars(select(FinOp).where(FinOp.cashbox == old)):
            op.cashbox = name
        for op in db.scalars(select(FinOp).where(FinOp.cashbox_to == old)):
            op.cashbox_to = name
        db.commit()
    return x


def has_ops(db: Session, name: str) -> bool:
    return bool(db.scalar(select(FinOp.id).where(FinOp.deleted_at.is_(None), (FinOp.cashbox == name) | (FinOp.cashbox_to == name)).limit(1)))


def delete(db: Session, bid: int) -> None:
    xs = items(db)
    x = next((x for x in xs if int(x["id"]) == int(bid)), None)
    if not x:
        return
    if has_ops(db, x["name"]) or balance(db, x):
        x.update(deleted=True, active=False, deleted_at=datetime.now().strftime("%d.%m.%Y %H:%M"))   # історія лишається — у «Видалені каси»
        _save(db, xs)
        return
    _save(db, [y for y in xs if y is not x])


def restore(db: Session, bid: int) -> None:
    xs = items(db)
    for x in xs:
        if int(x["id"]) == int(bid):
            x.update(deleted=False, active=True)
    _save(db, xs)


@dataclass
class Move:
    at: datetime
    kind: str          # in | out
    amount: float
    what: str
    link: str | None


def _since(x: dict) -> date | None:
    try:
        return date.fromisoformat(x["opening_date"]) if x.get("opening_date") else None
    except ValueError:
        return None


def moves(db: Session, x: dict, limit: int | None = None) -> list[Move]:
    since = _since(x)
    out: list[Move] = []
    q = select(FinOp).where(FinOp.deleted_at.is_(None), (FinOp.cashbox == x["name"]) | (FinOp.cashbox_to == x["name"]))
    if since:
        q = q.where(FinOp.day >= since)
    for op in db.scalars(q):
        at = datetime.combine(op.day, datetime.min.time())
        what = f"CRM-{op.id} · {op.category}" + (f" · {op.counterparty}" if op.counterparty else "")
        if op.kind == "transfer":
            if op.cashbox == x["name"]:
                out.append(Move(at, "out", op.amount, what + f" → {op.cashbox_to}", "/finance/ops"))
            if op.cashbox_to == x["name"]:
                out.append(Move(at, "in", op.amount, what + f" ← {op.cashbox}", "/finance/ops"))
        elif op.cashbox == x["name"]:
            out.append(Move(at, "in" if op.kind == "income" else "out", op.amount, what, "/finance/ops"))
    from ..models import CbFinOp                               # журнал CleverBox (поки CleverBox працює паралельно)
    q = select(CbFinOp).where((CbFinOp.account == x["name"]) | (CbFinOp.vid == x["name"]), CbFinOp.kind.in_(("income", "expense", "transfer")))
    if since:
        q = q.where(CbFinOp.at >= datetime.combine(since, datetime.min.time()))
    for op in db.scalars(q):
        what = f"CleverBox #{op.id} · {op.article or op.basis or ''}" + (f" · {op.counterparty}" if op.counterparty else "")
        if op.kind == "transfer":
            if op.account == x["name"]:
                out.append(Move(op.at, "out", op.amount, what + f" → {op.vid}", None))
            if op.vid == x["name"]:
                out.append(Move(op.at, "in", op.amount, what + f" ← {op.account}", None))
        elif op.account == x["name"]:
            out.append(Move(op.at, "in" if op.kind == "income" else "out", op.amount, what, None))
    meth = x.get("methods") or []
    if meth:
        q = select(SalePayment, Sale).join(Sale, Sale.id == SalePayment.sale_id).where(SalePayment.method.in_(meth), Sale.status == "paid")
        if since:
            q = q.where(Sale.at >= datetime.combine(since, datetime.min.time()))
        for p, s in db.execute(q):
            out.append(Move(s.at, "in", p.amount, f"Чек Ч-{s.id} · {p.method}", f"/sales/{s.id}"))
    out.sort(key=lambda m: m.at, reverse=True)
    return out[:limit] if limit else out


def balance(db: Session, x: dict) -> float:
    return round(float(x.get("opening") or 0) + sum(m.amount if m.kind == "in" else -m.amount for m in moves(db, x)), 2)


def overview(db: Session) -> list[dict]:
    from . import clinic
    locs = {int(l["id"]): l["name"] for l in clinic.get(db).get("locations") or []}
    if not db.get(SyncState, KEY):
        _save(db, items(db))                                   # закріпити ID кас
    out = []
    for x in items(db):
        mv = moves(db, x)
        out.append({**x, "type_label": TYPES.get(x.get("type"), x.get("type")), "location": locs.get(int(x["location_id"])) if x.get("location_id") else None,
                    "balance": round(float(x.get("opening") or 0) + sum(m.amount if m.kind == "in" else -m.amount for m in mv), 2),
                    "last": max((m.at for m in mv), default=None), "cash": x.get("type") == "cash"})
    return out
