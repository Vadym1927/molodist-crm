"""Склад → Замовлення → «Сформувати замовлення» постачальнику (як таблиця власника «Залишки / замовлення»).

По кожному товару постачальника: критичний (мінімальний) залишок у картці товару (Product.min_stock), к-сть в обліку CRM
зараз, по факту (адміністратор перераховує на вітрині, за замовчуванням = облік), «потрібно замовити» =
критичний − в обліку (не менше 0; якщо по факту менше обліку — від факту), ціна за одиницю (прайс постачальника:
ціна закупівлі товару, для реалізації — ціна банки з останньої поставки), сума. Кнопка створює документ «Замовлення»."""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ConsignLot, Product, StockDocLine, SyncState

SEED_KEY = "order_min_tebiskin_v1"
# Таблиця «Залишки / замовлення», вкладка TEBISKIN (09.11.2026): артикул, назва (ключ), критичний залишок
TEBISKIN_MIN = [("T13-50", "TEBISKIN LC 50", 2), ("T2N", "SOOTH-CLEAN 200", 4), ("T5-50", "SOOTH CREAM 50", 10), ("T6-50", "OSK CREAM 50", 2),
                ("T28", "OSK-CLEAN CLEANSER 200", 2), ("T9-50", "UV-OSK CREAM 50", 2), ("4034", "Laser Doctor (20", 1),
                ("T30-50", "UV-LC 50", 0), ("T1N", "GLY-CLEAN 200", 0), ("T8-50", "UVSOOTH SPF 50", 0)]


def _n(s: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (s or "").lower()).split())


def ensure_seed(db: Session) -> int:
    """Один раз: критичні залишки TEBISKIN з таблиці → картки товарів (і артикул, якщо порожній)."""
    if db.get(SyncState, SEED_KEY):
        return 0
    prods = db.scalars(select(Product).where(Product.active.is_(True))).all()
    n = 0
    for sku, key, mn in TEBISKIN_MIN:
        k = _n(key)
        hits = [p for p in prods if (p.sku or "").strip().upper() == sku.upper()] or [p for p in prods if k in _n(p.name)]
        if "laser doctor" in k:
            hits = [p for p in hits if "20" in (p.name or "") and "490" not in (p.name or "")] or hits[:1]
        for p in hits[:1]:
            p.min_stock = mn
            if not p.sku:
                p.sku = sku
            n += 1
    db.merge(SyncState(key=SEED_KEY, value=datetime.now().isoformat(timespec="minutes"), updated_at=datetime.utcnow()))
    db.commit()
    return n


def supplier_products(db: Session, supplier: str) -> list[Product]:
    low = supplier.strip().lower()
    ids = {pid for (pid,) in db.execute(select(ConsignLot.product_id).where(ConsignLot.supplier == supplier, ConsignLot.product_id.is_not(None)))}
    out = [p for p in db.scalars(select(Product).where(Product.active.is_(True)).order_by(Product.name))
           if p.id in ids or (p.supplier or "").strip().lower() == low]
    return out


def rows(db: Session, supplier: str, facts: dict[int, float] | None = None) -> list[dict]:
    from . import stock
    facts = facts or {}
    bal = stock.balance_by_product(db)
    last_cost = {}
    for l in db.scalars(select(ConsignLot).where(ConsignLot.supplier == supplier).order_by(ConsignLot.inv_date, ConsignLot.id)):
        if l.product_id:
            last_cost[l.product_id] = l.cost
    out = []
    for p in supplier_products(db, supplier):
        st = round(float(bal.get(p.id, 0.0)), 3)
        fact = facts.get(p.id, st)
        mn = p.min_stock or 0
        need = max(0, int(round(mn - min(st, fact))))
        price = last_cost.get(p.id) or float(p.cost or 0) or None
        out.append({"p": p, "sku": p.sku or "", "min": mn, "stock": st, "fact": fact, "need": need, "price": price,
                    "sum": round(need * (price or 0), 2)})
    out.sort(key=lambda r: (-(r["need"] > 0), -(r["min"] > 0), r["p"].name))
    return out


def create(db: Session, supplier: str, lines: list[tuple[int, float, float | None]], user_id: int | None = None):
    from . import stock
    lines = [(pid, q, c) for pid, q, c in lines if q and q > 0]
    if not lines:
        return None
    wh = (db.get(Product, lines[0][0]).warehouse or "vitrina")
    doc = stock.new_doc(db, "order", user_id=user_id, supplier=supplier, wh_to=wh,
                        note="Сформовано з критичних залишків (критичний − в обліку)")
    for pid, q, c in lines:
        doc.lines.append(StockDocLine(product_id=pid, qty=q, unit_cost=c))
    db.commit()
    return doc
