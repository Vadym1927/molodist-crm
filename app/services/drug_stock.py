"""Склад → Залишки препаратів (як аркуш «Залишки препарати» у таблиці власника).

По постачальниках (MERZ, INNOVA, EMET, ЕСТЕТ ГЛОБАЛ, …) — препарати ін'єкційного й класичного складу:
акційна пропозиція постачальника, к-сть на 1 процедуру (з техкарт), критичний залишок (картка товару),
в наявності (облік CRM), процедур на місяць і пацієнти на 1 / 2 / 3 місяці вперед (записи в календарі, чиї
послуги за техкартою списують цей препарат), потрібно замовити = критичний + потреба на місяць − в наявності,
ціна з останньої поставки, сума. «Сформувати замовлення» по постачальнику → документ «Замовлення» →
сума в «Календарі оплат складу»."""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, Patient, Product, ProductPurchase, StockDoc, SyncState, TechCard

PROMO_KEY = "supplier_promos"
WAREHOUSES = ("inject", "classic")
PROMO_SEED = {
    "MERZ": "Belotero −30% від 3 одиниць · Rejuran — знижка від 300 € · Ксеомін замовляємо по 3 фл",
    "INNOVA": "Jalupro 4+1",
    "EMET": "HP Cell Vitaran від 2 · Медінокс по 200 МО",
    "ЕСТЕТ ГЛОБАЛ": "Plenhyage XL Strong: від 3 — −33%",
    "Casmara": "−20% від 4 упаковок + раз на пів року засіб домашнього догляду (щоб лишатись партнером із −20%)",
    "Аптека": "Диспорт — по 300 або 500 МО",
}


def promos(db: Session) -> dict:
    st = db.get(SyncState, PROMO_KEY)
    if not st:
        return dict(PROMO_SEED)
    try:
        return json.loads(st.value or "{}")
    except ValueError:
        return {}


def save_promo(db: Session, supplier: str, text: str) -> None:
    d = promos(db)
    if text.strip():
        d[supplier] = text.strip()[:500]
    else:
        d.pop(supplier, None)
    db.merge(SyncState(key=PROMO_KEY, value=json.dumps(d, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def _per_proc(db: Session) -> tuple[dict[int, float], dict[int, list]]:
    """К-сть на 1 процедуру (найчастіша в техкартах) і техкарти → [(product_id, qty)]."""
    per: dict = defaultdict(Counter)
    card_lines: dict = defaultdict(list)
    for c in db.scalars(select(TechCard).where(TechCard.active.is_not(False))):
        for ln in c.lines:
            per[ln.product_id][float(ln.qty or 0)] += 1
            card_lines[c.id].append((ln.product_id, float(ln.qty or 0)))
    return {pid: cnt.most_common(1)[0][0] for pid, cnt in per.items()}, card_lines


def upcoming(db: Session, days: int, card_lines: dict, today: date | None = None) -> dict[int, dict]:
    """Записи на N днів уперед → по препарату: процедур, к-сть, пацієнти [(дата, прізвище)]."""
    from . import techcards as tc
    today = today or date.today()
    a, b = datetime.combine(today, datetime.min.time()), datetime.combine(today + timedelta(days=days), datetime.max.time())
    out: dict = defaultdict(lambda: {"n": 0, "qty": 0.0, "pts": []})
    pts = {}
    cache: dict = {}
    for ap in db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start <= b,
                                                   Appointment.status.notin_(("cancelled", "no_show", "done"))).order_by(Appointment.start)):
        seen = set()
        for s in ap.services or []:
            key = (s.get("id"), s.get("name"))
            if key not in cache:
                sv = tc.resolve_service(db, s.get("id"), s.get("name"))
                cache[key] = sv.techcard_id if sv and sv.techcard_id else None
            cid = cache[key]
            for pid, q in card_lines.get(cid, []) if cid else []:
                o = out[pid]
                o["qty"] += q
                if (pid, ap.id) not in seen:
                    seen.add((pid, ap.id))
                    o["n"] += 1
                    if ap.patient_id not in pts:
                        p = db.get(Patient, ap.patient_id)
                        pts[ap.patient_id] = (p.full_name.split()[0] if p and p.full_name else "—")
                    o["pts"].append((ap.start.date(), pts[ap.patient_id]))
    return out


def last_prices(db: Session) -> dict[int, tuple[float, date]]:
    out = {}
    for pp in db.scalars(select(ProductPurchase).where(ProductPurchase.unit_cost > 0).order_by(ProductPurchase.bought_on, ProductPurchase.id)):
        out[pp.product_id] = (float(pp.unit_cost), pp.bought_on)
    return out


def last_supplier(db: Session) -> dict[int, str]:
    out = {}
    for pp in db.scalars(select(ProductPurchase).where(ProductPurchase.supplier.is_not(None)).order_by(ProductPurchase.bought_on)):
        out[pp.product_id] = pp.supplier
    return out


def build(db: Session, months: int = 1, show_all: bool = False, today: date | None = None) -> dict:
    from . import stock
    today = today or date.today()
    per, card_lines = _per_proc(db)
    up1 = upcoming(db, 30, card_lines, today)
    upn = up1 if months == 1 else upcoming(db, 30 * months, card_lines, today)
    bal = stock.balance_by_product(db)
    prices = last_prices(db)
    lsup = last_supplier(db)
    pr = promos(db)
    groups: dict = defaultdict(list)
    for p in db.scalars(select(Product).where(Product.active.is_(True), Product.warehouse.in_(WAREHOUSES)).order_by(Product.name)):
        st = round(float(bal.get(p.id, 0.0)), 3)
        u1 = up1.get(p.id, {"n": 0, "qty": 0.0, "pts": []})
        un = upn.get(p.id, {"n": 0, "qty": 0.0, "pts": []})
        mn = p.min_stock or 0
        if not show_all and not (mn or st > 0 or u1["n"] or un["n"]):
            continue
        need = max(0, math.ceil(mn + u1["qty"] - max(st, 0) - 1e-9)) if (mn or u1["qty"]) else 0
        price, pdate = prices.get(p.id, (float(p.cost or 0) or None, None))
        sup = p.supplier or lsup.get(p.id) or "Без постачальника"
        groups[sup].append({"p": p, "per": per.get(p.id), "unit": p.unit or "шт.", "min": mn, "stock": st, "n1": u1["n"], "q1": round(u1["qty"], 2),
                            "pts": un["pts"], "need": need, "price": price, "pdate": pdate, "sum": round(need * (price or 0), 2),
                            "low": mn and st < mn})
    out = []
    for sup, rows in groups.items():
        rows.sort(key=lambda r: (-(r["need"] > 0), r["p"].name.lower()))
        out.append({"supplier": sup, "rows": rows, "total": round(sum(r["sum"] for r in rows), 2), "promo": pr.get(sup),
                    "need_n": sum(1 for r in rows if r["need"] > 0), "order": _open_order(db, sup)})
    out.sort(key=lambda g: (-g["total"], g["supplier"].lower()))
    return {"groups": out, "months": months, "today": today, "total": round(sum(g["total"] for g in out), 2), "show_all": show_all}


def _open_order(db: Session, supplier: str) -> StockDoc | None:
    from . import stock_paycal
    return stock_paycal.open_order(db, supplier)
