"""Продаж у CRM (каса): розрахунок (знижки, бонуси), проведення (склад, бонуси, сертифікати, візити, запис), повернення."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (Appointment, CbService, Doctor, GiftCertificate, Patient, Product, ProductSale, Sale, SaleItem,
                      SalePayment, StockMove, Visit)
from . import loyalty as ly

BONUS = "Бонуси"
CERT = "Сертифікат"
BALANCE = "Рахунок пацієнта"
SUB = "Абонемент"
NON_MONEY = (BONUS, CERT, BALANCE, SUB)
TOL = 0.005


class SaleError(ValueError):
    pass


PAYLINK = "Посилання на оплату"


def methods(db: Session) -> list[str]:
    """Способи оплати каси = активні каси / рахунки (як у CleverBox) + посилання на оплату по кожному ФОП."""
    from . import cashboxes as cbx, paylinks as pls
    out = [x["name"] for x in cbx.items(db) if x.get("active") and not x.get("deleted") and cbx.in_pos(x)]
    out += [paylink_method(a["name"]) for a in pls.public(db)["accounts"] if a["ready"]]
    return out or ["Готівка", "Картка (термінал)", "Інше"]


def paylink_method(account_name: str) -> str:
    return f"{PAYLINK} · {account_name}"


def method_kind(m: str) -> str:
    if m.startswith(PAYLINK):
        return "link"
    if m in NON_MONEY:
        return "nonmoney"
    n = m.lower()
    return "cash" if ("готів" in n or "сейф" in n) else "card"


def quote(db: Session, items: list[dict], patient: Patient | None, rule_ids: list[int] | None = None,
          manual_discount: float = 0.0, bonus_pay: float = 0.0) -> dict:
    """items: [{kind, ref_id, name, category, qty, price}] → знижки по рядках, підсумки, бонуси."""
    li = [{"type": it["kind"], "price": float(it["price"]), "qty": float(it.get("qty") or 1), "category": it.get("category") or ""}
          for it in items]
    d = ly.discount_for(db, li, patient, rule_ids=rule_ids) if items else {"lines": [], "total": 0.0, "rules": [], "subtotal": 0.0}
    lines = list(d["lines"])
    sub = round(sum(x["price"] * x["qty"] for x in li), 2)
    md = round(min(max(manual_discount or 0, 0), sub - sum(lines)), 2)
    if md > 0:                                      # ручна знижка — пропорційно
        base = (sub - sum(lines)) or 1
        lines = [round(l + md * (x["price"] * x["qty"] - l) / base, 2) for l, x in zip(lines, li)]
    disc = round(sum(lines), 2)
    total = round(sub - disc, 2)
    max_b = ly.max_bonus_pay(db, total, patient.id) if patient else 0.0
    bp = round(min(max(bonus_pay or 0, 0), max_b), 2)
    nets = [{"type": x["type"], "price": x["price"], "qty": x["qty"], "net": round(x["price"] * x["qty"] - l, 2)} for x, l in zip(li, lines)]
    accr = 0.0
    s = ly.settings(db)
    if patient and s.get("enabled") and total + TOL >= float(s.get("min_check") or 0):
        accr = ly.bonus_accrual(db, nets, bp)
    return {"lines": lines, "subtotal": sub, "discount": disc, "total": total, "rules": d["rules"] + (["Ручна знижка"] if md > 0 else []),
            "bonus_max": max_b, "bonus_pay": bp, "bonus_accrual": accr, "to_pay": round(total - bp, 2),
            "bonus_balance": ly.balance(db, patient.id) if patient else 0.0}


def create(db: Session, data: dict, user_id: int | None = None) -> Sale:
    """data: patient_id, appointment_id, items[{kind, ref_id, name, category, qty, price, doctor_id, warehouse, expiry}],
    rule_ids, manual_discount, bonus_pay, payments[{method, amount, cert_id}], comment."""
    from .stock import ledger_active, _fefo
    patient = db.get(Patient, data["patient_id"]) if data.get("patient_id") else None
    items = [it for it in data.get("items") or [] if float(it.get("qty") or 0) > 0 and float(it.get("price") or 0) >= 0]
    if not items:
        raise SaleError("Додайте хоча б одну позицію")
    q = quote(db, items, patient, data.get("rule_ids"), float(data.get("manual_discount") or 0), float(data.get("bonus_pay") or 0))
    pays = [p for p in data.get("payments") or [] if float(p.get("amount") or 0) > TOL and p.get("method") not in (BONUS,)]
    paid = round(sum(float(p["amount"]) for p in pays), 2)
    if abs(paid - q["to_pay"]) > 0.01:
        raise SaleError(f"Оплата {paid:.2f} ₴ не дорівнює сумі до сплати {q['to_pay']:.2f} ₴")
    from ..models import Subscription
    if data.get("fiscal_rro") == 0 and any(method_kind(str(p.get("method") or "")) in ("card", "link") for p in pays):
        raise SaleError("Без РРО можна приймати лише готівку — оберіть касу Checkbox для оплати карткою чи посиланням")
    for p in pays:                                  # сертифікат / рахунок / абонемент: перевірка залишку
        if p.get("method") == CERT:
            c = db.get(GiftCertificate, int(p.get("cert_id") or 0))
            if not c or (c.balance or 0) + TOL < float(p["amount"]):
                raise SaleError("Недостатньо коштів на сертифікаті")
        if p.get("method") == BALANCE:
            if not patient or (patient.cb_balance or 0) + TOL < sum(float(x["amount"]) for x in pays if x.get("method") == BALANCE):
                raise SaleError("Недостатньо коштів на рахунку пацієнта")
        if p.get("method") == SUB:
            sb = db.get(Subscription, int(p.get("sub_id") or 0))
            if not sb or not patient or sb.patient_id != patient.id or sb.state() not in ("active", "expiring"):
                raise SaleError("Абонемент недоступний для цього пацієнта")
            if sb.visits_total is None and (sb.balance or 0) + TOL < float(p["amount"]):
                raise SaleError("Недостатньо коштів на абонементі")
            if sb.visits_total is not None and (sb.visits_left or 0) < 1:
                raise SaleError("На абонементі не лишилось візитів")
    if any(str(p.get("method") or "").startswith(PAYLINK) for p in pays) and not patient:
        raise SaleError("Посилання на оплату можна надіслати лише пацієнту — оберіть пацієнта")
    sale = Sale(patient_id=patient.id if patient else None, appointment_id=data.get("appointment_id"), created_by=user_id,
                status="awaiting" if any(str(p.get("method") or "").startswith(PAYLINK) for p in pays) else "paid",
                subtotal=q["subtotal"], discount=q["discount"], total=q["total"], bonus_spent=q["bonus_pay"],
                bonus_accrued=q["bonus_accrual"], rules=q["rules"] or None, comment=(data.get("comment") or "")[:500] or None,
                at=datetime.now())
    db.add(sale)
    for it, disc in zip(items, q["lines"]):
        qty, price = float(it.get("qty") or 1), round(float(it["price"]), 2)
        sale.items.append(SaleItem(kind=it["kind"], ref_id=it.get("ref_id"), name=str(it.get("name") or "")[:300], category=it.get("category"),
                                   qty=qty, price=price, discount=disc, total=round(price * qty - disc, 2),
                                   doctor_id=it.get("doctor_id") or None, warehouse=it.get("warehouse") or None,
                                   expiry_date=it.get("expiry") or None))
    for p in pays:
        sale.payments.append(SalePayment(method=str(p["method"])[:60], amount=round(float(p["amount"]), 2),
                                         cert_id=int(p["cert_id"]) if p.get("cert_id") else None,
                                         sub_id=int(p["sub_id"]) if p.get("sub_id") else None))
    if q["bonus_pay"] > 0:
        sale.payments.append(SalePayment(method=BONUS, amount=q["bonus_pay"]))
    db.flush()
    # наслідки
    docs = {d.id: d.name for d in db.scalars(select(Doctor))}
    cache: dict = {}
    ledger = ledger_active(db)
    for si in sale.items:
        if si.kind == "service" and patient:
            db.add(Visit(source="crm_sale", external_id=f"crm:{sale.id}:{si.id}", patient_id=patient.id, service_id=si.ref_id,
                         service_name=si.name, master_name=docs.get(si.doctor_id), starts_at=sale.at, status="done",
                         status_label="Продаж у CRM", amount=si.total))
        if si.kind == "product":
            pr = db.get(Product, si.ref_id) if si.ref_id else None
            if patient:
                db.add(ProductSale(external_id=f"crm:{sale.id}:{si.id}", patient_id=patient.id, product_id=si.ref_id, product_name=si.name,
                                   quantity=max(1, int(round(si.qty))), amount=si.total, sold_at=sale.at))
            if pr and ledger:
                wh = si.warehouse or pr.warehouse or "vitrina"
                k = pr.coef if pr.coef and pr.coef > 0 else 1
                qty = si.qty * k
                parts = [(si.expiry_date, qty)] if si.expiry_date else _fefo(cache, db, pr.id, wh, qty)
                for i, (e, qq) in enumerate(parts):
                    db.add(StockMove(product_id=pr.id, warehouse=wh, qty=-qq, kind="sale", at=sale.at, patient_id=sale.patient_id,
                                     ref=f"crmsale:{sale.id}:{si.id}" + (f"#{i}" if i else ""), note=f"Продаж CRM №{sale.id}",
                                     expiry_date=e))
    for p in sale.payments:
        if p.method == CERT and p.cert_id:
            c = db.get(GiftCertificate, p.cert_id)
            c.balance = round((c.balance or 0) - p.amount, 2)
        if p.method == BALANCE and patient:
            patient.cb_balance = round((patient.cb_balance or 0) - p.amount, 2)
        if p.method == SUB and p.sub_id:
            sb = db.get(Subscription, p.sub_id)
            if sb.visits_total is not None:
                sb.visits_used = (sb.visits_used or 0) + 1
            else:
                sb.balance = round((sb.balance or 0) - p.amount, 2)
    db.commit()
    if patient and q["bonus_pay"] > 0:
        ly.add(db, patient.id, -q["bonus_pay"], "spend", f"чек №{sale.id}", f"sale:{sale.id}", user_id)
    if patient and q["bonus_accrual"] > 0:
        ly.add(db, patient.id, q["bonus_accrual"], "accrual", f"чек №{sale.id}", f"sale:{sale.id}", user_id)
    if sale.appointment_id:
        a = db.get(Appointment, sale.appointment_id)
        if a:
            a.sale_id, a.status = sale.id, "done"
            db.commit()
    return sale


def refund(db: Session, sale: Sale, reason: str, user_id: int | None = None) -> None:
    from ..models import BonusTxn
    if sale.status not in ("paid", "awaiting"):
        raise SaleError("Чек уже повернено")
    db.execute(StockMove.__table__.delete().where(StockMove.ref.like(f"crmsale:{sale.id}:%")))
    db.execute(Visit.__table__.delete().where(Visit.external_id.like(f"crm:{sale.id}:%")))
    db.execute(ProductSale.__table__.delete().where(ProductSale.external_id.like(f"crm:{sale.id}:%")))
    for p in sale.payments:
        if p.method == CERT and p.cert_id:
            c = db.get(GiftCertificate, p.cert_id)
            if c:
                c.balance = round((c.balance or 0) + p.amount, 2)
        if p.method == BALANCE and sale.patient:
            sale.patient.cb_balance = round((sale.patient.cb_balance or 0) + p.amount, 2)
        if p.method == SUB and p.sub_id:
            from ..models import Subscription
            sb = db.get(Subscription, p.sub_id)
            if sb and sb.visits_total is not None:
                sb.visits_used = max(0, (sb.visits_used or 0) - 1)
            elif sb:
                sb.balance = round((sb.balance or 0) + p.amount, 2)
    sale.status, sale.refunded_at, sale.refund_reason = "refunded", datetime.now(), reason[:300]
    db.commit()
    if sale.patient_id:
        for t in db.scalars(select(BonusTxn).where(BonusTxn.ref == f"sale:{sale.id}")).all():
            db.add(BonusTxn(patient_id=t.patient_id, amount=-t.amount, kind="manual", comment=f"повернення чека №{sale.id}",
                            ref=f"refund:{sale.id}", user_id=user_id, at=datetime.now()))
        db.commit()
    if sale.appointment_id:
        a = db.get(Appointment, sale.appointment_id)
        if a and a.sale_id == sale.id:
            a.sale_id, a.status = None, "arrived"
            db.commit()


def cancel(db: Session, sale: Sale, reason: str, user_id: int | None = None, http=None) -> None:
    """Скасувати неоплачений чек (очікує оплати): відкотити склад/візити/рахунки, закрити посилання на оплату."""
    from ..models import PayLink
    from . import paylinks as pls
    if sale.status != "awaiting":
        raise SaleError("Скасувати можна лише чек, що очікує оплати" if sale.status != "cancelled" else "Чек уже скасовано")
    links = db.query(PayLink).filter(PayLink.sale_id == sale.id).all()
    for pl in links:                      # раптом уже оплатили — не скасовуємо
        if pl.status == "created":
            try:
                pls.check_status(db, pl, http)
            except Exception:  # noqa: BLE001
                pass
    db.refresh(sale)
    if sale.status != "awaiting" or any(pl.status == "paid" for pl in links):
        raise SaleError("За посиланням уже є оплата — скасувати не можна, оформіть повернення")
    refund(db, sale, reason or "скасовано", user_id)
    for pl in links:
        if pl.status == "created":
            pls.remove(db, pl, http)
    sale.status = "cancelled"
    db.commit()


def archive(db: Session, sale: Sale, on: bool = True) -> None:
    """Видалити чек з каси в архів (лише скасовані) або повернути з архіву."""
    if on and sale.status not in ("cancelled",):
        raise SaleError("В архів можна лише скасований чек")
    sale.archived_at = datetime.now() if on else None
    db.commit()


def staff_totals(db: Session, d_from: date, d_to: date) -> dict:
    """(місяць, лікар) → послуги / товари з продажів CRM — для зарплати."""
    out: dict = {}
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    for s in db.scalars(select(Sale).where(Sale.status == "paid", Sale.at >= a, Sale.at <= b)):
        mk = f"{s.at.year}-{s.at.month:02d}"
        for it in s.items:
            if not it.doctor_id:
                continue
            r = out.setdefault((mk, it.doctor_id), {"services": 0.0, "products": 0.0, "n": 0})
            if it.kind == "service":
                r["services"] += it.total
                r["n"] += int(round(it.qty))
            elif it.kind == "product":
                r["products"] += it.total
    return out
