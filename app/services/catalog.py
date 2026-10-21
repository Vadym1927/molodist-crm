"""Розділи «Продукти» і «Склад»: каталог засобів, конверсія «призначено → куплено», потреба за планами vs залишки."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, PlanItem, StockLevel, BeautyPlan
from ..parsers.observ import norm
from .kpi import name_match, all_statuses, PlanStatus


def match_product(db: Session, name: str, products: list[Product] | None = None) -> Product | None:
    products = products if products is not None else db.scalars(select(Product)).all()
    key = norm(name)
    best, score = None, 0.0
    for p in products:
        if p.name_norm == key:
            return p
        s = name_match(name, p.name)
        if s > score:
            best, score = p, s
    return best if score >= 0.7 else None


def link_item(db: Session, it: PlanItem, products: list[Product]) -> Product:
    prod = match_product(db, it.name, products)
    if not prod:                                        # засіб є в плані, але ще немає в каталозі
        prod = Product(name=it.name, name_norm=norm(it.name), volume=it.volume, price=it.unit_price, url=it.url)
        db.add(prod)
        db.flush()
        products.append(prod)
    prod.volume = prod.volume or it.volume
    prod.url = prod.url or it.url
    if not prod.price:
        prod.price = it.unit_price
    it.product_id = prod.id
    return prod


def link_plan(db: Session, plan: BeautyPlan):
    products = db.scalars(select(Product)).all()
    for it in plan.items:
        if it.kind == "product" and not it.product_id:
            prod = link_item(db, it, list(products))
            if prod.supply_days:                        # тривалість засобу, задана в каталозі, має пріоритет
                it.supply_days = prod.supply_days


def link_all_items(db: Session):
    products = list(db.scalars(select(Product)))
    for it in db.scalars(select(PlanItem).where(PlanItem.kind == "product", PlanItem.product_id.is_(None))):
        link_item(db, it, products)
    # засоби, створені з плану, які тепер зіставились з товаром CleverBox — зливаємо
    cb = [p for p in products if p.cb_product_id]
    for p in [p for p in products if not p.cb_product_id]:
        twin = match_product(db, p.name, cb)
        if twin:
            for it in db.scalars(select(PlanItem).where(PlanItem.product_id == p.id)):
                it.product_id = twin.id
            twin.supply_days = twin.supply_days or p.supply_days
            twin.url = twin.url or p.url
            twin.volume = twin.volume or p.volume
            db.delete(p)
    db.commit()


@dataclass
class ProductRow:
    product: Product
    prescribed: int = 0           # призначено в планах (шт)
    bought: int = 0               # куплено пацієнтами з планів
    revenue: float = 0.0
    demand_open: int = 0          # призначено в активних планах, але ще не куплено
    rebuy_30: int = 0             # повторні покупки, що «настають» у найближчі 30 днів
    patients: set = field(default_factory=set)
    stock: float | None = None

    @property
    def conversion(self):
        return round(100 * self.bought / self.prescribed, 1) if self.prescribed else 0.0

    @property
    def need_30(self):
        return self.demand_open + self.rebuy_30

    @property
    def deficit(self):
        if self.stock is None:
            return None
        return max(0, int(round(self.need_30 + (self.product.min_stock or 0) - self.stock)))


def stock_by_product(db: Session, products: list[Product]) -> dict[int, float]:
    levels = db.scalars(select(StockLevel)).all()
    out: dict[int, float] = defaultdict(float)
    by_cb = {p.cb_product_id: p.id for p in products if p.cb_product_id}
    for lv in levels:
        pid = by_cb.get(lv.cb_product_id)
        if pid is None:
            m = match_product(db, lv.name, products)
            pid = m.id if m else None
        if pid is not None:
            out[pid] += lv.qty
    return out


def product_rows(db: Session, statuses: list[PlanStatus] | None = None, now: datetime | None = None) -> list[ProductRow]:
    now = now or datetime.now()
    statuses = statuses if statuses is not None else all_statuses(db)
    products = db.scalars(select(Product).order_by(Product.name)).all()
    rows = {p.id: ProductRow(p) for p in products}
    for ps in statuses:
        if ps.plan.status == "cancelled":
            continue
        for st in ps.items:
            it = st.item
            if it.kind != "product" or it.product_id not in rows:
                continue
            r = rows[it.product_id]
            r.prescribed += it.qty
            r.bought += min(st.done, it.qty)
            r.revenue += min(st.done, it.qty) * it.unit_price
            r.patients.add(ps.plan.patient_id)
            if ps.plan.status == "active" and st.done < it.qty:
                r.demand_open += it.qty - st.done
            if st.last_done and it.supply_days and st.done >= it.qty:
                due = st.last_done + timedelta(days=it.supply_days)
                if now - timedelta(days=30) <= due <= now + timedelta(days=30):
                    r.rebuy_30 += 1
    have_stock = db.scalar(select(StockLevel).limit(1)) is not None
    if have_stock:
        stock = stock_by_product(db, products)
        for pid, r in rows.items():
            r.stock = stock.get(pid, 0.0)
    return sorted(rows.values(), key=lambda r: (-r.prescribed, r.product.name))


@dataclass
class ProcedureRow:
    name: str
    planned: int = 0
    done: int = 0
    booked: int = 0
    planned_sum: float = 0.0
    realized_sum: float = 0.0
    plans: int = 0
    prices: list = field(default_factory=list)

    @property
    def conversion(self):
        return round(100 * self.done / self.planned, 1) if self.planned else 0.0

    @property
    def avg_price(self):
        return round(sum(self.prices) / len(self.prices)) if self.prices else 0


def procedure_rows(statuses: list[PlanStatus]) -> list[ProcedureRow]:
    rows: dict[str, ProcedureRow] = {}
    for ps in statuses:
        if ps.plan.status == "cancelled":
            continue
        for st in ps.items:
            it = st.item
            if it.kind != "procedure":
                continue
            r = rows.setdefault(norm(it.name), ProcedureRow(it.name))
            r.planned += it.qty
            r.done += min(st.done, it.qty)
            r.booked += st.booked
            r.planned_sum += it.total
            r.realized_sum += st.realized_sum
            r.plans += 1
            r.prices.append(it.unit_price)
    return sorted(rows.values(), key=lambda r: -r.planned_sum)
