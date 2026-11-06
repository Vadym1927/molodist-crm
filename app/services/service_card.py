"""Картка послуги (як у CleverBox) і планувальник ціни.

Планувальник: склад техкарти з двома цінами матеріалів —
  • на дату формування ціни продажу (остання закупівля не пізніше цієї дати);
  • зараз (остання поставка / закупівля).
Звідси дві собівартості, два валових прибутки (ціна − собівартість) і дві маржі. Препарати виводяться окремо від
розхідників (категорія техкарти «Препарат…» або склад «Ін'єкційна»).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CbService, Product, ProductPurchase, ServiceLog, TechCard

FIELDS = {"price": "Ціна", "name": "Назва", "techcard": "Техкарта", "duration": "Тривалість", "created": "Послугу створено",
          "category": "Категорія", "price_set_on": "Дата формування ціни", "active": "Активність"}
COLORS = ["#D9483B", "#DD8547", "#CDA33B", "#5E9F52", "#3A8778", "#4499DD", "#3F86D9", "#3E49A0",
          "#8A2FA8", "#C7305E", "#5B707B", "#36434B"]


def log(db: Session, sid: int, field: str, old, new, user_id: int | None = None, source: str = "crm", commit: bool = True) -> None:
    f = lambda v: None if v is None else (f"{v:g}" if isinstance(v, float) else str(v))[:300]  # noqa: E731
    db.add(ServiceLog(service_id=sid, field=field, old=f(old), new=f(new), user_id=user_id, source=source, at=datetime.utcnow()))
    if commit:
        db.commit()


def history(db: Session, sid: int) -> list[ServiceLog]:
    return db.scalars(select(ServiceLog).where(ServiceLog.service_id == sid).order_by(ServiceLog.at.desc(), ServiceLog.id.desc())).all()


def is_drug(cat: str | None, p: Product | None) -> bool:
    c = (cat or "").lower()
    return "препарат" in c or (p is not None and p.warehouse == "inject" and "розх" not in c and "одноразк" not in c)


def _purchases(db: Session, pids: set[int]) -> dict[int, list[ProductPurchase]]:
    out: dict[int, list] = defaultdict(list)
    if pids:
        for pp in db.scalars(select(ProductPurchase).where(ProductPurchase.product_id.in_(pids), ProductPurchase.unit_cost > 0)
                             .order_by(ProductPurchase.bought_on, ProductPurchase.id)):
            out[pp.product_id].append(pp)
    return out


def planner(db: Session, sv: CbService, on: date | None = None, new_price: float | None = None) -> dict:
    """Розрахунок: лінії техкарти з ціною одиниці на дату формування ціни і зараз, підсумки й маржі."""
    from .techcards import unit_costs
    on = on or sv.price_set_on
    if not on:                                   # дату не задано — остання зміна ціни з історії
        last = db.scalar(select(ServiceLog.at).where(ServiceLog.service_id == sv.id, ServiceLog.field == "price").order_by(ServiceLog.at.desc()).limit(1))
        on = last.date() if last else None
    card = db.get(TechCard, sv.techcard_id) if sv.techcard_id else None
    lines = list(card.lines) if card else []
    hist = _purchases(db, {ln.product_id for ln in lines})
    uc = unit_costs(db)
    rows = []
    for ln in lines:
        p = ln.product
        arr = hist.get(ln.product_id, [])
        now_pp = arr[-1] if arr else None
        now_u = float(now_pp.unit_cost) if now_pp else float(uc.get(ln.product_id, 0) or 0)
        then_pp = None
        if on and arr:
            prior = [x for x in arr if x.bought_on <= on]
            then_pp = prior[-1] if prior else arr[0]            # до першої закупівлі в CRM — найраніша відома ціна
        then_u = float(then_pp.unit_cost) if then_pp else now_u
        q = float(ln.qty or 0)
        rows.append({"name": p.name if p else f"#{ln.product_id}", "pid": ln.product_id, "cat": ln.cat, "qty": q,
                     "unit": (p.unit if p else "") or "шт", "drug": is_drug(ln.cat, p),
                     "then_u": round(then_u, 4), "now_u": round(now_u, 4), "then": round(q * then_u, 2), "now": round(q * now_u, 2),
                     "then_at": then_pp.bought_on if then_pp else None, "now_at": now_pp.bought_on if now_pp else None,
                     "now_sup": now_pp.supplier if now_pp else None, "diff": round(q * (now_u - then_u), 2)})
    rows.sort(key=lambda r: (not r["drug"], -r["now"]))
    price = float(sv.price or 0)

    def tot(key, drug=None):
        return round(sum(r[key] for r in rows if drug is None or r["drug"] == drug), 2)

    c_then, c_now = tot("then"), tot("now")
    res = {"card": card, "rows": rows, "on": on, "price": price,
           "drug_then": tot("then", True), "drug_now": tot("now", True), "cons_then": tot("then", False), "cons_now": tot("now", False),
           "cost_then": c_then, "cost_now": c_now, "cost_diff": round(c_now - c_then, 2),
           "gross_then": round(price - c_then, 2), "gross_now": round(price - c_now, 2),
           "margin_then": round(100 * (price - c_then) / price, 1) if price else None,
           "margin_now": round(100 * (price - c_now) / price, 1) if price else None}
    res["gross_diff"] = round(res["gross_now"] - res["gross_then"], 2)
    res["margin_diff"] = round(res["margin_now"] - res["margin_then"], 1) if price else None
    # ціна, щоб повернути маржу на дату формування при поточній собівартості
    m = (res["margin_then"] or 0) / 100
    res["keep_margin_price"] = round(c_now / (1 - m), 0) if price and m < 0.99 and c_now else None
    if new_price:
        res["np"] = new_price
        res["np_gross"] = round(new_price - c_now, 2)
        res["np_margin"] = round(100 * (new_price - c_now) / new_price, 1)
    return res
