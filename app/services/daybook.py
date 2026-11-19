"""Каса за добу (як «Продажі за добу» у CleverBox): оплачено по касах і не грошима, залишки кас на початок і кінець дня,
чеки дня (CleverBox + каса CRM)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Patient, Sale, User
from . import cashboxes as cbx, finops as fo

NON_MONEY_ROWS = [("Баланс пацієнта", ("баланс",)), ("Сертифікат", ("сертиф",)), ("Абонемент", ("абонем",)), ("Бонусний рахунок", ("бонус",))]



WEB_MARK = "Інтернет-магазин"


def is_web(s) -> bool:
    """Чек створено з оплаченого замовлення сайту molodist.store (касир — «Сайт-магазин»)."""
    return not s.created_by and (s.comment or "").startswith(WEB_MARK)


WEB_SELLER = "MOLODIST shop"


def web_order(db, s):
    """Замовлення сайту, з якого створено чек."""
    from sqlalchemy import select as _sel
    from ..models import SiteOrder
    num = (s.comment or "").replace(WEB_MARK, "").replace(", замовлення", "").strip(" ,")
    o = db.scalar(_sel(SiteOrder).where(SiteOrder.number == num)) if num else None
    return o


def web_seller(db, s) -> str:
    """Ініціатор продажу з сайту — відповідальний за замовлення (хто опрацював і запакував); інакше «MOLODIST shop»."""
    from ..models import User
    o = web_order(db, s)
    rid = (o.raw or {}).get("resp") if o else None
    u = db.get(User, rid) if rid else None
    return (u.name or u.login) if u else WEB_SELLER


def web_item_discount(db, it) -> float:
    """Знижка сайту на рядок чека: акція / короткий термін «вшиті» в ціну сайту → різниця зі звичайною ціною товару в CRM."""
    if float(it.discount or 0) > 0 or it.kind != "product" or not it.ref_id:
        return float(it.discount or 0)
    from ..models import Product
    p = db.get(Product, it.ref_id)
    reg = float((p.price_manual or p.price or 0) if p else 0)
    q = float(it.qty or 1)
    return round(max(0.0, reg * q - float(it.total or 0)), 2) if reg else 0.0


def web_discount(db, s) -> float:
    """Повна знижка чека з сайту: знижки сайту в цінах + коїни / промокод (знижка чека)."""
    return round(sum(web_item_discount(db, i) for i in s.items) + float(s.discount or 0), 2)

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
        web = is_web(s)
        rows.append({"at": s.at, "num": f"Ч-{s.id}", "link": f"/sales/{s.id}", "src": "Сайт" if web else "CRM",
                     "basis": ("Інтернет-магазин · " + (s.comment or "").split("замовлення", 1)[-1].strip()) if web else "Продаж CRM" + (f" · запис №{s.appointment_id}" if s.appointment_id else ""),
                     "patient": pats.get(s.patient_id), "web": web,
                     "staff": ", ".join(sorted({docs.get(i.doctor_id) for i in s.items if i.doctor_id and docs.get(i.doctor_id)})) or (WEB_SELLER if web else users.get(s.created_by, "")),
                     "lines": [f"{i.name}" + (f" × {i.qty:g}" if i.qty != 1 else "") for i in s.items], "cost": (round(float(s.total or 0) + web_discount(db, s), 2) if web else s.subtotal),
                     "discount": (web_discount(db, s) if web else s.discount),
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
