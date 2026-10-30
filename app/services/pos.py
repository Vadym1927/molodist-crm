"""Каталог для каси (як у CleverBox): візити дня, послуги (категорії / напрямки), товари (склади / категорії / бренди)."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, CbService, Doctor, Patient, Product, StockLevel, Visit
from .catalog import WAREHOUSES, stock_by_product

GROUPS_S = {"category": "Категорії", "direction": "Напрямки"}
GROUPS_P = {"warehouse": "Склади", "category": "Категорії", "brand": "Бренди"}
NONE = "Без групи"


def visits(db: Session, d: date, user=None) -> list[dict]:
    """Записи дня: CRM (з послугами й цінами) + візити CleverBox. Уже розраховані — позначені."""
    from .specialists import DoctorIndex, all_doctors
    a, b = datetime.combine(d, datetime.min.time()), datetime.combine(d + timedelta(days=1), datetime.min.time())
    docs = {x.id: x.name for x in db.scalars(select(Doctor))}
    svc = {s.id: s for s in db.scalars(select(CbService))}
    out = []
    appts = db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start < b, Appointment.status != "cancelled")
                       .order_by(Appointment.start)).all()
    mirrored = {str(x.cb_record_id) for x in appts if x.cb_record_id}
    for x in appts:
        out.append({"key": f"a{x.id}", "appt_id": x.id, "at": x.start.strftime("%H:%M"), "end": x.end.strftime("%H:%M") if x.end else "",
                    "patient_id": x.patient_id, "patient": x.patient.full_name if x.patient else "—", "doctor_id": x.doctor_id,
                    "doctor": docs.get(x.doctor_id, ""), "sale_id": x.sale_id, "status": x.status,
                    "services": [{"id": s.get("id"), "name": s.get("name"), "price": float(s.get("price") or 0),
                                  "category": s.get("category") or (svc[s["id"]].category if s.get("id") in svc else "")} for s in x.services or []]})
    idx = DoctorIndex(all_doctors(db))
    groups: dict = {}
    for v in db.scalars(select(Visit).where(Visit.starts_at >= a, Visit.starts_at < b, Visit.source == "cb_visit",
                                            Visit.status.in_(("booked", "attended", "done"))).order_by(Visit.starts_at)):
        if any(v.external_id.startswith(m + "-") for m in mirrored):
            continue
        doc = idx.find(v.master_name)
        k = (v.patient_id, v.starts_at, doc.id if doc else v.master_name)
        g = groups.get(k)
        if not g:
            g = groups[k] = {"key": f"v{v.id}", "appt_id": None, "at": v.starts_at.strftime("%H:%M"),
                             "end": v.ends_at.strftime("%H:%M") if v.ends_at else "", "patient_id": v.patient_id,
                             "patient": v.patient.full_name if v.patient else "—", "doctor_id": doc.id if doc else None,
                             "doctor": doc.name if doc else (v.master_name or ""), "sale_id": None, "status": v.status, "services": [], "cb": True}
            out.append(g)
        s = svc.get(v.service_id)
        if v.service_name or s:
            g["services"].append({"id": v.service_id if s else None, "name": v.service_name or s.name,
                                  "price": float((s.price if s else None) or v.amount or 0), "category": s.category if s else ""})
    out.sort(key=lambda r: r["at"])
    return out


def _svc_key(s: CbService, group: str) -> str:
    return (getattr(s, group, None) or NONE).strip() or NONE


def services(db: Session, group: str = "category", key: str | None = None, q: str = "") -> dict:
    group = group if group in GROUPS_S else "category"
    rows = [s for s in db.scalars(select(CbService).order_by(CbService.name)) if (s.price or 0) >= 0]
    ql = q.strip().lower()
    if ql:
        rows = [s for s in rows if ql in (s.name or "").lower() or ql in (s.category or "").lower() or ql in (s.direction or "").lower()]
        return {"items": [_s(s) for s in rows[:80]]}
    if key is None:
        c = Counter(_svc_key(s, group) for s in rows)
        return {"groups": [{"key": k, "n": n} for k, n in sorted(c.items(), key=lambda kv: (kv[0] == NONE, kv[0].lower()))]}
    return {"items": [_s(s) for s in rows if _svc_key(s, group) == key]}


def _s(s: CbService) -> dict:
    return {"id": s.id, "name": s.name, "price": s.price or 0, "minutes": s.time_min or 30, "category": s.category or "",
            "direction": s.direction or ""}


def _stock(db: Session, products: list[Product]) -> dict[int, float]:
    from .stock import ledger_active, balance_by_product
    if ledger_active(db):
        return balance_by_product(db)
    if db.scalar(select(StockLevel).limit(1)) is not None:
        return stock_by_product(db, products)
    return {}


def _p_key(p: Product, group: str) -> str:
    if group == "warehouse":
        return WAREHOUSES.get(p.warehouse or "vitrina", p.warehouse or NONE)
    return (getattr(p, group, None) or NONE).strip() or NONE


def products(db: Session, group: str = "warehouse", key: str | None = None, q: str = "", in_stock: bool = False) -> dict:
    group = group if group in GROUPS_P else "warehouse"
    ps = [p for p in db.scalars(select(Product).order_by(Product.name)) if p.active is not False and p.for_sale is not False]
    stock = _stock(db, ps)
    if in_stock and stock:
        ps = [p for p in ps if stock.get(p.id, 0) > 1e-6]
    ql = q.strip().lower()
    if ql:
        ps = [p for p in ps if ql in " ".join(filter(None, [p.name, p.sku, p.brand, p.barcode, p.category])).lower()]
        return {"items": [_p(db, p, stock) for p in ps[:80]]}
    if key is None:
        c = Counter(_p_key(p, group) for p in ps)
        order = list(WAREHOUSES.values()) if group == "warehouse" else []
        return {"groups": [{"key": k, "n": n} for k, n in sorted(c.items(), key=lambda kv: (kv[0] == NONE, order.index(kv[0]) if kv[0] in order else 99, kv[0].lower()))]}
    return {"items": [_p(db, p, stock) for p in ps if _p_key(p, group) == key]}


def _p(db: Session, p: Product, stock: dict) -> dict:
    from .stock import ledger_active, batch_balances
    wh = p.warehouse or "vitrina"
    return {"id": p.id, "name": p.name, "brand": p.brand or "", "sku": p.sku or "", "barcode": p.barcode or "", "category": p.brand or p.category or "",
            "price": p.price or p.price_manual or p.shop_price or 0, "stock": round(stock[p.id], 3) if p.id in stock else None,
            "warehouse": wh, "wh": WAREHOUSES.get(wh), "photo": p.photo_url or ""}
