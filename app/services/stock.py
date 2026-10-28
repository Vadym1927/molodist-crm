"""Складський облік у CRM: журнал рухів, документи, залишки, автосписання продажів і процедур.

Правда по складу — журнал StockMove (залишок = сума рухів по товару і складу).
Облік стартує документом «Стартові залишки» (з поточних залишків CleverBox / імпорту Excel);
після старту продажі з CleverBox списують вітрину, а надані послуги — матеріали за нормами.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import (Product, StockLevel, StockDoc, StockDocLine, StockMove, ProductPurchase, ProductSale, Visit,
                      ProcedureNorm, SyncState, Supplier)
from .catalog import WAREHOUSES

DOC_KINDS = {"supply": "Поставка", "order": "Замовлення", "transfer": "Переміщення", "writeoff": "Списання",
             "return": "Повернення", "inventory": "Інвентаризація", "opening": "Стартові залишки"}
DOC_PREFIX = {"supply": "П", "order": "З", "transfer": "ПМ", "writeoff": "С", "return": "В", "inventory": "І", "opening": "СТ"}
WRITEOFF_REASONS = ["Використано в кабінеті", "Прострочено", "Пошкоджено", "Брак", "Подарунок / тестер", "Інше"]
MOVE_KINDS = {"supply": "Поставка", "sale": "Продаж", "procedure": "Процедура", "writeoff": "Списання",
              "transfer": "Переміщення", "return": "Повернення", "inventory": "Інвентаризація", "opening": "Старт"}
STATUS = {"draft": "Чернетка", "posted": "Проведено", "cancelled": "Скасовано", "sent": "Відправлено",
          "received": "Отримано"}


class StockError(Exception):
    pass


# ---------------- старт обліку ----------------

def ledger_start(db: Session) -> datetime | None:
    st = db.get(SyncState, "stock_ledger_start")
    return datetime.fromisoformat(st.value) if st and st.value else None


def ledger_active(db: Session) -> bool:
    return ledger_start(db) is not None


def start_ledger(db: Session, user_id: int | None = None) -> StockDoc:
    """Документ «Стартові залишки» з поточних залишків (CleverBox / Excel) і дата старту обліку."""
    if ledger_active(db):
        raise StockError("Облік уже розпочато")
    products = db.scalars(select(Product)).all()
    by_cb = {p.cb_product_id: p for p in products if p.cb_product_id}
    qty: dict[int, float] = defaultdict(float)
    for lv in db.scalars(select(StockLevel)):
        p = by_cb.get(lv.cb_product_id)
        if p and lv.qty and lv.qty > 0:
            qty[p.id] += lv.qty
    doc = new_doc(db, "opening", user_id=user_id, note="Залишки на момент старту обліку в CRM")
    pmap = {p.id: p for p in products}
    for pid, q in qty.items():
        doc.lines.append(StockDocLine(product_id=pid, qty=q, unit_cost=pmap[pid].cost))
    db.flush()
    now = datetime.now()
    for ln in doc.lines:
        p = pmap[ln.product_id]
        db.add(StockMove(product_id=p.id, warehouse=p.warehouse or "vitrina", qty=ln.qty, kind="opening", at=now,
                         doc_id=doc.id, unit_cost=ln.unit_cost))
    doc.status, doc.posted_at = "posted", now
    st = db.get(SyncState, "stock_ledger_start") or SyncState(key="stock_ledger_start")
    st.value = now.isoformat(timespec="seconds")
    db.merge(st)
    db.commit()
    return doc


# ---------------- документи ----------------

def next_number(db: Session, kind: str) -> str:
    n = db.scalar(select(func.count()).select_from(StockDoc).where(StockDoc.kind == kind)) or 0
    return f"{DOC_PREFIX.get(kind, 'Д')}-{n + 1:04d}"


def new_doc(db: Session, kind: str, user_id: int | None = None, **kw) -> StockDoc:
    if kind not in DOC_KINDS:
        raise StockError("Невідомий тип документа")
    doc = StockDoc(kind=kind, number=next_number(db, kind), doc_date=kw.pop("doc_date", None) or date.today(),
                   status="draft", created_by=user_id, **kw)
    if kind == "supply" and not doc.wh_to:
        doc.wh_to = "vitrina"
    db.add(doc)
    db.flush()
    return doc


def balances(db: Session, warehouse: str | None = None, at: datetime | None = None) -> dict[tuple[int, str], float]:
    q = select(StockMove.product_id, StockMove.warehouse, func.sum(StockMove.qty)).group_by(StockMove.product_id,
                                                                                         StockMove.warehouse)
    if warehouse:
        q = q.where(StockMove.warehouse == warehouse)
    if at:
        q = q.where(StockMove.at <= at)
    return {(pid, wh): float(s or 0) for pid, wh, s in db.execute(q)}


def balance_by_product(db: Session) -> dict[int, float]:
    out: dict[int, float] = defaultdict(float)
    for (pid, _wh), q in balances(db).items():
        out[pid] += q
    return out


def _move(db, doc, ln, wh, qty, kind, note=None):
    db.add(StockMove(product_id=ln.product_id, warehouse=wh, qty=qty, kind=kind, at=datetime.combine(doc.doc_date, datetime.now().time()),
                     doc_id=doc.id, unit_cost=ln.unit_cost, note=note, patient_id=doc.patient_id))


def post(db: Session, doc: StockDoc, allow_negative: bool = True) -> None:
    """Провести документ: створити рухи. Поставка також створює закупівлі (сер./остання ціна, термін)."""
    if doc.status not in ("draft", "sent"):
        raise StockError("Документ уже проведено або скасовано")
    if doc.kind == "order":
        raise StockError("Замовлення не проводиться — прийміть його як поставку")
    lines = [ln for ln in doc.lines if ln.qty or doc.kind == "inventory"]
    if not lines:
        raise StockError("Додайте хоча б один товар")
    k = doc.kind
    if k in ("supply", "transfer") and not doc.wh_to:
        raise StockError("Оберіть склад отримання")
    if k in ("transfer", "writeoff", "inventory") and not doc.wh_from:
        raise StockError("Оберіть склад")
    if k == "transfer" and doc.wh_from == doc.wh_to:
        raise StockError("Склади відправлення і отримання однакові")
    if k == "return" and not (doc.wh_from or doc.wh_to):
        raise StockError("Оберіть склад")
    bal = balances(db)
    if k == "inventory":
        for ln in lines:
            sys_q = bal.get((ln.product_id, doc.wh_from), 0.0)
            ln.sys_qty = sys_q
            diff = round((ln.qty or 0) - sys_q, 4)
            if diff:
                _move(db, doc, ln, doc.wh_from, diff, "inventory", "надлишок" if diff > 0 else "нестача")
    else:
        for ln in lines:
            q = abs(ln.qty)
            if k == "supply":
                _move(db, doc, ln, doc.wh_to, q, "supply")
                db.add(ProductPurchase(product_id=ln.product_id, bought_on=doc.doc_date, qty=q, unit_cost=ln.unit_cost or 0,
                                       supplier=doc.supplier, expiry_date=ln.expiry_date, doc_no=doc.ext_no or doc.number,
                                       note=f"stockdoc:{doc.id}"))
                if doc.supplier:
                    p = db.get(Product, ln.product_id)
                    p.supplier = p.supplier or doc.supplier
            elif k == "transfer":
                _move(db, doc, ln, doc.wh_from, -q, "transfer", f"→ {WAREHOUSES.get(doc.wh_to)}")
                _move(db, doc, ln, doc.wh_to, q, "transfer", f"← {WAREHOUSES.get(doc.wh_from)}")
            elif k == "writeoff":
                _move(db, doc, ln, doc.wh_from, -q, "writeoff", doc.reason)
            elif k == "return":
                if doc.reason == "patient":                  # пацієнт повернув — товар назад на склад
                    _move(db, doc, ln, doc.wh_to or doc.wh_from, q, "return", "від пацієнта")
                else:                                        # повернення постачальнику
                    _move(db, doc, ln, doc.wh_from or doc.wh_to, -q, "return", "постачальнику")
            if not allow_negative and k in ("transfer", "writeoff") and bal.get((ln.product_id, doc.wh_from), 0) < q:
                raise StockError(f"Недостатньо залишку: {ln.product.name}")
    doc.status, doc.posted_at = "posted", datetime.now()
    db.commit()


def cancel(db: Session, doc: StockDoc) -> None:
    """Скасувати проведений документ: рухи видаляються, закупівлі з поставки теж."""
    if doc.kind == "opening":
        raise StockError("Стартові залишки не скасовуються — зробіть інвентаризацію")
    if doc.status == "posted":
        db.execute(StockMove.__table__.delete().where(StockMove.doc_id == doc.id))
        if doc.kind == "supply":
            db.execute(ProductPurchase.__table__.delete().where(ProductPurchase.note == f"stockdoc:{doc.id}"))
    doc.status = "cancelled"
    db.commit()


def receive_order(db: Session, order: StockDoc, user_id: int | None = None) -> StockDoc:
    """Замовлення → чернетка поставки з тими самими товарами."""
    if order.kind != "order" or order.status in ("received", "cancelled"):
        raise StockError("Замовлення вже отримано або скасовано")
    sup = new_doc(db, "supply", user_id=user_id, supplier=order.supplier, wh_to=order.wh_to or "vitrina",
                  source_id=order.id, note=f"За замовленням {order.number}")
    for ln in order.lines:
        sup.lines.append(StockDocLine(product_id=ln.product_id, qty=ln.qty, unit_cost=ln.unit_cost))
    order.status = "received"
    db.commit()
    return sup


def inventory_prefill(db: Session, doc: StockDoc) -> int:
    """Інвентаризація: усі товари складу з обліковою к-стю (фактичну вводять поверх)."""
    bal = balances(db, doc.wh_from)
    have = {ln.product_id for ln in doc.lines}
    pids = {pid for (pid, _), q in bal.items() if q}
    pids |= {p.id for p in db.scalars(select(Product).where(Product.warehouse == doc.wh_from))}
    n = 0
    for pid in sorted(pids - have):
        q = bal.get((pid, doc.wh_from), 0.0)
        doc.lines.append(StockDocLine(product_id=pid, qty=q, sys_qty=q))
        n += 1
    db.commit()
    return n


def order_from_critical(db: Session, rows, supplier: str | None, user_id: int | None = None) -> StockDoc | None:
    """Замовлення постачальнику з критичних залишків: до мін. залишку + потреба 30 днів."""
    doc = None
    for r in rows:
        p = r.product
        sup = p.supplier or r.last_supplier
        if supplier and sup != supplier:
            continue
        need = suggested_qty(r)
        if need <= 0:
            continue
        if doc is None:
            doc = new_doc(db, "order", user_id=user_id, supplier=supplier, wh_to=p.warehouse or "vitrina")
        doc.lines.append(StockDocLine(product_id=p.id, qty=need, unit_cost=r.cost_last or r.cost_avg))
    db.commit()
    return doc


def suggested_qty(r) -> int:
    stock = r.stock or 0
    target = (r.product.min_stock or 0) + r.need_30 + (round(r.sold_90 / 3) if r.sold_90 else 0)
    return max(0, int(round(target - stock)))


def is_critical(r) -> bool:
    if r.stock is None:
        return False
    if r.product.min_stock:
        return r.stock <= r.product.min_stock
    return r.stock <= 0 and (r.sold_90 or r.prescribed)


# ---------------- автосписання ----------------

def auto_moves(db: Session) -> dict:
    """Після синку CleverBox: продажі списують товар (вітрина), надані послуги — матеріали за нормами.
    Лише події після старту обліку; кожна подія — один раз (ref)."""
    start = ledger_start(db)
    if not start:
        return {}
    rep = {"sales": 0, "procedures": 0}
    existing = {r for (r,) in db.execute(select(StockMove.ref).where(StockMove.ref.is_not(None),
                                                                       StockMove.at >= start - timedelta(days=1)))}
    by_cb = {p.cb_product_id: p for p in db.scalars(select(Product).where(Product.cb_product_id.is_not(None)))}
    for s in db.scalars(select(ProductSale).where(ProductSale.sold_at >= start)):
        ref = f"sale:{s.external_id}"
        p = by_cb.get(s.product_id)
        if ref in existing or not p:
            continue
        db.add(StockMove(product_id=p.id, warehouse=p.warehouse or "vitrina", qty=-(s.quantity or 1), kind="sale",
                         at=s.sold_at, ref=ref, patient_id=s.patient_id, note="продаж CleverBox"))
        existing.add(ref)
        rep["sales"] += 1
    norms: dict[int, list[ProcedureNorm]] = defaultdict(list)
    for n in db.scalars(select(ProcedureNorm)):
        norms[n.service_id].append(n)
    if norms:
        for v in db.scalars(select(Visit).where(Visit.source == "cb_service_sale", Visit.status == "done",
                                                Visit.starts_at >= start, Visit.service_id.in_(list(norms)))):
            for n in norms[v.service_id]:
                ref = f"proc:{v.id}:{n.id}"
                if ref in existing:
                    continue
                db.add(StockMove(product_id=n.product_id, warehouse=n.warehouse or n.product.warehouse or "single",
                                 qty=-n.qty, kind="procedure", at=v.starts_at, ref=ref, patient_id=v.patient_id,
                                 note=(v.service_name or "")[:300]))
                existing.add(ref)
                rep["procedures"] += 1
    db.commit()
    return rep


# ---------------- постачальники ----------------

def ensure_suppliers(db: Session) -> None:
    """Довідник постачальників з товарів і закупівель (без дублів)."""
    have = {s.name.lower() for s in db.scalars(select(Supplier))}
    names = {x for (x,) in db.execute(select(Product.supplier).where(Product.supplier.is_not(None)).distinct())}
    names |= {x for (x,) in db.execute(select(ProductPurchase.supplier).where(ProductPurchase.supplier.is_not(None)).distinct())}
    for nm in sorted(names):
        if nm and nm.strip().lower() not in have:
            db.add(Supplier(name=nm.strip()[:200]))
            have.add(nm.strip().lower())
    db.commit()


@dataclass
class DocRow:
    doc: StockDoc
    items: int = 0
    qty: float = 0
    total: float = 0


def doc_rows(db: Session, kind: str, limit: int = 200) -> list[DocRow]:
    docs = db.scalars(select(StockDoc).where(StockDoc.kind == kind).order_by(StockDoc.doc_date.desc(), StockDoc.id.desc())
                      .limit(limit)).all()
    out = []
    for d in docs:
        r = DocRow(d, len(d.lines), sum(abs(ln.qty or 0) for ln in d.lines),
                   sum(abs(ln.qty or 0) * (ln.unit_cost or 0) for ln in d.lines))
        out.append(r)
    return out


def product_moves(db: Session, product_id: int, limit: int = 100):
    return db.scalars(select(StockMove).where(StockMove.product_id == product_id)
                      .order_by(StockMove.at.desc(), StockMove.id.desc()).limit(limit)).all()
