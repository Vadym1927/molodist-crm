"""Каса за добу (як «Продажі за добу» у CleverBox): оплачено по касах і не грошима, залишки кас на початок і кінець дня,
чеки дня (CleverBox + каса CRM)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Patient, Sale, User
from . import cashboxes as cbx, finops as fo

NON_MONEY_ROWS = [("Баланс пацієнта", ("баланс",)), ("Сертифікат", ("сертиф",)), ("Абонемент", ("абонем",)), ("Бонусний рахунок", ("бонус",))]


def _non_money(name: str) -> str | None:
    n = (name or "").lower()
    for label, keys in NON_MONEY_ROWS:
        if any(k in n for k in keys):
            return label
    return None


def day(db: Session, d: date) -> dict:
    boxes = [x for x in cbx.items(db) if x.get("active") and not x.get("deleted")]
    m2box = {m: x["name"] for x in boxes for m in x.get("methods") or []}
    paid: dict[str, float] = {x["name"]: 0.0 for x in boxes}
    for label, _ in NON_MONEY_ROWS:
        paid[label] = 0.0
    a, b = datetime.combine(d, datetime.min.time()), datetime.combine(d + timedelta(days=1), datetime.min.time())

    def add(name: str, amt: float):
        key = m2box.get(name) or (name if name in paid else None) or _non_money(name) or name
        paid[key] = round(paid.get(key, 0.0) + amt, 2)

    cb_checks = fo.checks(db, d, d)
    for c in cb_checks:
        for p in c.payments or []:
            add(p.get("payment_name") or "—", float(p.get("amount") or 0))
    sales = db.scalars(select(Sale).where(Sale.at >= a, Sale.at < b, Sale.archived_at.is_(None)).order_by(Sale.at.desc())).all()
    for s in sales:
        if s.status == "paid":
            for p in s.payments:
                add(p.method, p.amount)
    accounts = []
    for x in boxes:
        mv = cbx.moves(db, x)
        accounts.append({"name": x["name"], "color": x.get("color"), "start": cbx.balance_at(db, x, a, mv), "end": cbx.balance_at(db, x, b, mv)})
    # чеки дня
    pats = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_(
        [c.patient_id for c in cb_checks if c.patient_id] + [s.patient_id for s in sales if s.patient_id] or [0])))}
    docs = {dd.id: dd.name for dd in db.scalars(select(fo.Doctor))}
    users = {u.id: u.name for u in db.scalars(select(User))}
    rows = []
    for s in sales:
        rows.append({"at": s.at, "num": f"Ч-{s.id}", "link": f"/sales/{s.id}", "src": "CRM", "basis": "Продаж CRM" + (f" · запис №{s.appointment_id}" if s.appointment_id else ""),
                     "patient": pats.get(s.patient_id), "staff": ", ".join(sorted({docs.get(i.doctor_id) for i in s.items if i.doctor_id and docs.get(i.doctor_id)})) or users.get(s.created_by, ""),
                     "lines": [f"{i.name}" + (f" × {i.qty:g}" if i.qty != 1 else "") for i in s.items], "cost": s.subtotal, "discount": s.discount,
                     "to_pay": s.total, "status": {"refunded": "Повернено", "awaiting": "Очікує оплати", "cancelled": "Скасовано"}.get(s.status, "Оплачено"),
                     "sid": s.id, "st": s.status,
                     "fiscal": bool(s.fiscal and s.fiscal.get("fiscal_code"))})
    for c in cb_checks:
        its = c.items or []
        cost = round(sum(fo.item_amount(i) for i in its) + (c.discount or 0), 2)
        rows.append({"at": c.date, "num": f"#{c.id}", "link": None, "src": "CleverBox", "basis": f"Продаж #{c.id}", "patient": pats.get(c.patient_id),
                     "staff": ", ".join(sorted({i.get("initiator_name") for i in its if i.get("initiator_name")})) or (c.author or ""),
                     "lines": [fo.item_name(i) + (f" × {i.get('quantity')}" if i.get("quantity") not in (None, 1, "1") else "") for i in its],
                     "cost": cost, "discount": c.discount or 0, "to_pay": c.amount, "status": "Оплачено", "fiscal": False})
    rows.sort(key=lambda r: r["at"], reverse=True)
    paid_rows = [(k, v) for k, v in paid.items()]
    n_arch = db.scalar(select(func.count(Sale.id)).where(Sale.archived_at.is_not(None))) or 0
    return {"n_arch": n_arch, "paid": paid_rows, "paid_total": round(sum(v for _, v in paid_rows), 2), "accounts": accounts, "rows": rows,
            "money_total": round(sum(v for k, v in paid_rows if k not in {l for l, _ in NON_MONEY_ROWS}), 2)}
