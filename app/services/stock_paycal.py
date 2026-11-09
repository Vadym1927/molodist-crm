"""Склад → Календар оплат складу (як «Календар Оплат» у таблиці власника).

Групи: ін'єкційна / косметика / одноразка, у кожній — постачальники. Рядок = поточний цикл оплати постачальнику:
ФОП (Колган / Бондар / Готівка / очікує вибору), планова сума (із замовлення CRM, а поки його немає — оцінка
за критичними залишками: скільки доведеться дозамовити), фактична сума (з рахунку постачальника), відхилення,
планова дата, дозвіл на оплату (власник), статус. Статус «Оплачено» — з банку (виписка monobank, привʼязана до
постачальника) або з фінoперації; після оплати цикл закривається й починається новий.
Рекомендований ФОП: залишок рахунку ФОП + прихід за графіком ФОПів до планової дати − уже заплановані оплати."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BankTxn, FinOp, StockDoc, StockPayRow, Supplier, SupplierInvoice
from . import fop_schedule as fs

GROUPS = [("inject", "Ін'єкційна косметологія"), ("cosmetics", "Косметика"), ("single", "Одноразка")]
FOP_CHOICES = ["ФОП Колган", "ФОП Бондар", "Готівка"]


def _low(s: str | None) -> str:
    return (s or "").strip().lower()


def default_group(db: Session, supplier: str) -> str:
    whs = Counter(x.warehouse for x in db.scalars(select(SupplierInvoice).where(SupplierInvoice.supplier == supplier)) if x.warehouse)
    if not whs:
        return "cosmetics"
    top = whs.most_common(1)[0][0]
    return "inject" if top == "inject" else ("single" if top == "single" else "cosmetics")


def open_order(db: Session, supplier: str, after: datetime | None = None) -> StockDoc | None:
    q = select(StockDoc).where(StockDoc.kind == "order", StockDoc.status.in_(("draft", "sent")), StockDoc.archived_at.is_(None))
    docs = [d for d in db.scalars(q.order_by(StockDoc.created_at.desc())) if _low(d.supplier) == _low(supplier)]
    if after:
        docs = [d for d in docs if d.created_at >= after - timedelta(days=1)]
    return docs[0] if docs else None


def order_total(doc: StockDoc) -> float:
    return round(sum(float(ln.qty or 0) * float(ln.unit_cost or 0) for ln in doc.lines), 2)


def estimate(db: Session, supplier: str, bal: dict | None = None) -> float:
    from . import order_builder as ob
    return round(sum(r["sum"] for r in ob.rows(db, supplier, bal=bal) if r["min"]), 2)


def suppliers(db: Session) -> list[str]:
    """Постачальники в календарі: з поставками за рік, критичними залишками, замовленнями або рядками."""
    from ..models import Product
    since = datetime.now() - timedelta(days=365)
    names = {x.supplier for x in db.scalars(select(SupplierInvoice).where(SupplierInvoice.inv_date >= since)) if x.supplier}
    names |= {p.supplier for p in db.scalars(select(Product).where(Product.min_stock > 0)) if p.supplier}
    names |= {d.supplier for d in db.scalars(select(StockDoc).where(StockDoc.kind == "order", StockDoc.status.in_(("draft", "sent")))) if d.supplier}
    names |= {r.supplier for r in db.scalars(select(StockPayRow).where(StockPayRow.closed.is_(False)))}
    names |= {s.name for s in db.scalars(select(Supplier).where(Supplier.consignment.is_(True)))}
    canon = {_low(s.name): s.name for s in db.scalars(select(Supplier))}
    return sorted({canon.get(_low(n), n) for n in names if n}, key=str.lower)


def row_for(db: Session, supplier: str) -> StockPayRow:
    r = db.scalar(select(StockPayRow).where(StockPayRow.supplier == supplier, StockPayRow.closed.is_(False)).order_by(StockPayRow.id.desc()))
    if not r:
        r = StockPayRow(supplier=supplier, grp=default_group(db, supplier), closed=False)
        db.add(r)
        db.flush()
    return r


def _match_paid(db: Session, r: StockPayRow, amount: float) -> tuple | None:
    """Оплата з банку (monobank, привʼязана до постачальника) або фіноперація з постачальником — після старту циклу."""
    since = (r.created_at - timedelta(days=2)) if r.created_at else datetime.now() - timedelta(days=30)
    sup = db.scalar(select(Supplier).where(Supplier.name == r.supplier))
    used = {x.paid_ref for x in db.scalars(select(StockPayRow).where(StockPayRow.paid_ref.is_not(None)))}
    if sup:
        for t in db.scalars(select(BankTxn).where(BankTxn.supplier_id == sup.id, BankTxn.amount < 0, BankTxn.at >= since).order_by(BankTxn.at)):
            ref = f"bank:{t.id}"
            if ref in used:
                continue
            if not amount or abs(abs(t.amount) - amount) <= max(1.0, amount * 0.15):
                return (t.at.date(), abs(t.amount), ref, t.fop)
    for op in db.scalars(select(FinOp).where(FinOp.deleted_at.is_(None), FinOp.kind == "expense", FinOp.day >= since.date()).order_by(FinOp.day)):
        if _low(r.supplier) and _low(r.supplier) in _low(op.counterparty) + " " + _low(op.comment):
            ref = f"finop:{op.id}"
            if ref in used:
                continue
            if not amount or abs(float(op.amount or 0) - amount) <= max(1.0, amount * 0.15):
                return (op.day, float(op.amount or 0), ref, op.cashbox)
    return None


def build(db: Session, today: date | None = None) -> dict:
    today = today or date.today()
    from ..models import Product
    from . import stock
    out = {k: [] for k, _ in GROUPS}
    rows_all = []
    with_min = {_low(p.supplier) for p in db.scalars(select(Product).where(Product.min_stock > 0)) if p.supplier}
    with_min |= {_low(s) for (s,) in db.execute(select(__import__("app.models", fromlist=["ConsignLot"]).ConsignLot.supplier).distinct())}
    sbal = stock.balance_by_product(db)
    for name in suppliers(db):
        r = row_for(db, name)
        doc = db.get(StockDoc, r.order_doc_id) if r.order_doc_id else None
        if not doc or doc.status not in ("draft", "sent", "received"):
            doc = open_order(db, name, r.created_at)
            r.order_doc_id = doc.id if doc else None
        plan = order_total(doc) if doc else None
        est = None if doc or _low(name) not in with_min else estimate(db, name, sbal)
        amount = r.fact_amount or plan or est or 0.0
        if not r.paid_at and (doc or r.fact_amount):
            m = _match_paid(db, r, amount)
            if m:
                r.paid_at, r.paid_amount, r.paid_ref, r.paid_fop = m
        status = ("paid" if r.paid_at else "approved" if r.approved else "ordered" if doc else "waiting")
        rows_all.append({"r": r, "doc": doc, "plan": plan, "est": est, "amount": amount, "status": status,
                         "dev": round((r.fact_amount or 0) - (plan or est or 0), 2) if r.fact_amount else None})
    db.commit()
    # рекомендований ФОП: баланс + прихід до дати − інші заплановані оплати з цього ФОП до дати
    bal = fs.fund_balances(db)
    planned = defaultdict(list)
    for x in rows_all:
        if x["status"] != "paid" and x["r"].fop in fs.FOPS and x["amount"]:
            planned[x["r"].fop].append((x["r"].plan_date or today, x["amount"], x["r"].id))
    inc_cache: dict = {}
    for x in rows_all:
        r = x["r"]
        if x["status"] == "paid" or not x["amount"]:
            x["rec"], x["proj"] = None, {}
            continue
        d = max(r.plan_date or today, today)
        if d not in inc_cache:
            inc_cache[d] = fs.income_until(db, d, today)
        proj = {}
        for f in fs.FOPS:
            others = sum(a for dd, a, rid in planned[f] if dd <= d and rid != r.id)
            proj[f] = round(bal.get(f, 0) + inc_cache[d].get(f, 0) - others - x["amount"], 2)
        x["proj"] = proj
        x["rec"] = max(proj, key=proj.get)
        out.setdefault(r.grp or "cosmetics", []).append(x)
    for x in rows_all:
        if x["status"] == "paid":
            out.setdefault(x["r"].grp or "cosmetics", []).append(x)
    order = {"paid": 4, "approved": 1, "ordered": 0, "waiting": 2}
    for k in out:
        out[k].sort(key=lambda x: (order[x["status"]], -(x["amount"] or 0), x["r"].supplier.lower()))
    tot = {k: {"plan": round(sum(x["amount"] or 0 for x in v if x["status"] != "paid"), 2),
               "fact": round(sum(x["r"].fact_amount or 0 for x in v if x["status"] != "paid"), 2)} for k, v in out.items()}
    history = db.scalars(select(StockPayRow).where(StockPayRow.closed.is_(True)).order_by(StockPayRow.paid_at.desc()).limit(30)).all()
    return {"groups": [(k, t, out.get(k, [])) for k, t in GROUPS], "tot": tot, "balances": bal, "FUND": fs.FUND, "FOPS": FOP_CHOICES,
            "GROUPS": GROUPS, "today": today, "history": history,
            "week": fs.income_until(db, today + timedelta(days=6), today)}


def close_paid(db: Session) -> int:
    """Оплачені цикли → історія; для постачальника починається новий рядок."""
    n = 0
    for r in db.scalars(select(StockPayRow).where(StockPayRow.closed.is_(False), StockPayRow.paid_at.is_not(None))):
        if r.paid_at <= date.today() - timedelta(days=3):
            r.closed = True
            n += 1
    db.commit()
    return n
