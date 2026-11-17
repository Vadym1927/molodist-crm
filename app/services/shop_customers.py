"""Покупці інтернет-магазину: хто зареєструвався в кабінеті і як купує (вкладка «Покупці» у центрі керування магазином)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CoinTxn, Patient, ShopAccount, ShopCart, SiteOrder

LOST = ("Скасовано", "Повернення", "Фейкове")
SORTS = {"new": "Нові реєстрації", "login": "Останній вхід", "sum": "Сума покупок", "orders": "Кількість замовлень"}
FILTERS = {"": "Усі", "buyers": "Купували", "nobuy": "Ще не купували", "repeat": "Повторні", "cart": "З кошиком"}


def _paid(o: SiteOrder) -> bool:
    return bool(o.paid) or (o.status or "") in ("Оплачено", "Відправлено", "Отримано", "Закрито")


def rows(db: Session, q: str = "", sort: str = "new", flt: str = "", since: date | None = None) -> dict:
    accs = list(db.scalars(select(ShopAccount)))
    pids = [a.patient_id for a in accs] or [0]
    pats = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_(pids)))}
    orders: dict[int, list[SiteOrder]] = defaultdict(list)
    for o in db.scalars(select(SiteOrder).where(SiteOrder.patient_id.in_(pids))):
        if (o.status or "") not in LOST:
            orders[o.patient_id].append(o)
    coins = dict(db.execute(select(CoinTxn.patient_id, func.sum(CoinTxn.amount)).where(CoinTxn.patient_id.in_(pids)).group_by(CoinTxn.patient_id)).all())
    carts = dict(db.execute(select(ShopCart.patient_id, ShopCart.total).where(ShopCart.patient_id.in_(pids), ShopCart.status == "open")).all())
    refs = defaultdict(int)
    for a in accs:
        if a.referred_by:
            refs[a.referred_by] += 1
    out = []
    ql = (q or "").strip().lower()
    for a in accs:
        p = pats.get(a.patient_id)
        name = " ".join(x for x in (a.first_name, a.last_name) if x) or (p.full_name if p else "") or "Покупець"
        phone = (p.phone if p else "") or ""
        if ql and ql not in f"{name} {phone} {a.email or ''}".lower():
            continue
        od = sorted(orders.get(a.patient_id, []), key=lambda o: o.created_at or datetime.min)
        paid = [o for o in od if _paid(o)]
        s = round(sum(o.total or 0 for o in paid), 2)
        r = {"acc": a, "patient": p, "name": name, "phone": phone, "orders": len(od), "paid": len(paid), "sum": s,
             "avg": round(s / len(paid), 2) if paid else 0, "last_order": od[-1].created_at if od else None,
             "coins": round(float(coins.get(a.patient_id) or 0)), "cart": float(carts.get(a.patient_id) or 0),
             "wish": len(a.wishlist or []), "invited": refs.get(a.patient_id, 0),
             "by": pats.get(a.referred_by) if a.referred_by else None}
        if flt == "buyers" and not paid or flt == "nobuy" and paid or flt == "repeat" and len(paid) < 2 or flt == "cart" and not r["cart"]:
            continue
        out.append(r)
    key = {"login": lambda r: r["acc"].last_login or datetime.min, "sum": lambda r: r["sum"], "orders": lambda r: r["paid"]}.get(
        sort, lambda r: r["acc"].created_at or datetime.min)
    out.sort(key=key, reverse=True)
    edge = datetime.combine(since or (date.today() - timedelta(days=29)), datetime.min.time())
    buyers = [pid for pid, od in orders.items() if any(_paid(o) for o in od)]
    rep = [pid for pid in buyers if sum(1 for o in orders[pid] if _paid(o)) >= 2]
    total = len(accs)
    kpi = {"total": total, "new": sum(1 for a in accs if a.created_at and a.created_at >= edge),
           "active": sum(1 for a in accs if a.last_login and a.last_login >= edge),
           "buyers": len(buyers), "conv": round(len(buyers) / total * 100, 1) if total else 0, "repeat": len(rep),
           "revenue": round(sum(sum(o.total or 0 for o in orders[pid] if _paid(o)) for pid in buyers), 2),
           "coins": round(sum(float(v or 0) for v in coins.values())), "carts": sum(1 for v in carts.values() if v)}
    kpi["ltv"] = round(kpi["revenue"] / len(buyers), 2) if buyers else 0
    return {"rows": out, "kpi": kpi}
