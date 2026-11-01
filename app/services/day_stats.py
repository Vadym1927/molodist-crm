"""Статистика дня для календаря (як у CleverBox): пацієнти, дохід, записи, скасування й перенесення, чеки, товари, послуги,
сертифікати й абонементи, поповнення рахунку, оплати, лікарі. Дані — CRM (каса й записи)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, AuditLog, Doctor, GiftCertificate, Patient, Sale, Subscription, Visit

CERT_W, SUB_W, TOPUP_W = ("сертиф",), ("абонем",), ("поповн", "депозит", "на рахунок")


def _has(name: str, words) -> bool:
    n = (name or "").lower()
    return any(w in n for w in words)


def compute(db: Session, d: date, doctor_id: int | None = None) -> dict:
    a, b = datetime.combine(d, datetime.min.time()), datetime.combine(d + timedelta(days=1), datetime.min.time())
    docs = {x.id: x.name for x in db.scalars(select(Doctor))}
    appts = db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start < b)).all()
    if doctor_id:
        appts = [x for x in appts if x.doctor_id == doctor_id]
    price = lambda ap: round(sum(float(s.get("price") or 0) for s in ap.services or []), 2)  # noqa: E731
    live = [x for x in appts if x.status not in ("cancelled", "no_show")]
    cancelled = [x for x in appts if x.status == "cancelled"]
    no_show = [x for x in appts if x.status == "no_show"]
    done = [x for x in appts if x.status in ("done", "arrived") or x.sale_id]

    # пацієнти: нові — перший візит саме цього дня (раніше не було ні записів CRM, ні візитів CleverBox)
    pids = {x.patient_id for x in live}
    earlier = set()
    if pids:
        earlier |= {p for (p,) in db.execute(select(Appointment.patient_id).where(Appointment.patient_id.in_(pids), Appointment.start < a,
                                                                                    Appointment.status.notin_(("cancelled",))))}
        earlier |= {p for (p,) in db.execute(select(Visit.patient_id).where(Visit.patient_id.in_(pids), Visit.starts_at < a,
                                                                              Visit.status.in_(("done", "attended"))))}
    new_p = pids - earlier

    # записи, створені цього дня
    created = db.scalars(select(Appointment).where(Appointment.created_at >= a - timedelta(hours=3), Appointment.created_at < b)).all()
    created = [x for x in created if x.created_at and (x.created_at + timedelta(hours=_tz_shift())).date() == d]
    online = [x for x in created if x.source in ("online", "bot")]
    by_admin = [x for x in created if x.source in ("crm", "appeal")]
    from_cb = [x for x in created if x.source == "cleverbox"]

    # скасовано цього дня (будь-які дати візитів) і перенесено (дата / час змінились)
    logs = db.scalars(select(AuditLog).where(AuditLog.at >= a - timedelta(hours=3), AuditLog.at < b + timedelta(hours=3),
                                             AuditLog.action.in_(("appointment.status", "appointment.save")))).all()
    logs = [l for l in logs if (l.at + timedelta(hours=_tz_shift())).date() == d]
    canc_ids = {int((l.details or {}).get("appt") or 0) for l in logs if l.action == "appointment.status" and (l.details or {}).get("status") == "cancelled"}
    moved_ids = {int((l.details or {}).get("appt") or 0) for l in logs if l.action == "appointment.save" and (l.details or {}).get("moved")}
    canc_today = [db.get(Appointment, i) for i in canc_ids if i]
    moved = [db.get(Appointment, i) for i in moved_ids if i]
    canc_today, moved = [x for x in canc_today if x], [x for x in moved if x]

    # каса
    sales = db.scalars(select(Sale).where(Sale.at >= a, Sale.at < b, Sale.status.in_(("paid", "refunded")))).all()
    paid = [s for s in sales if s.status == "paid"]
    refunded = [s for s in sales if s.status == "refunded"]
    if doctor_id:
        paid = [s for s in paid if any(i.doctor_id == doctor_id for i in s.items)]
    kinds = defaultdict(lambda: {"n": 0.0, "sum": 0.0})
    by_doc = defaultdict(lambda: {"services": 0.0, "products": 0.0, "visits": 0})
    pay = defaultdict(float)
    for s in paid:
        for it in s.items:
            if doctor_id and it.doctor_id != doctor_id:
                continue
            k = it.kind if it.kind in ("service", "product") else "other"
            if k == "other":
                k = "cert" if _has(it.name, CERT_W) else "sub" if _has(it.name, SUB_W) else "topup" if _has(it.name, TOPUP_W) else "other"
            kinds[k]["n"] += float(it.qty or 1)
            kinds[k]["sum"] += float(it.total or 0)
            if it.doctor_id:
                by_doc[it.doctor_id]["services" if it.kind == "service" else "products"] += float(it.total or 0)
        for p in s.payments:
            pay[p.method] += float(p.amount or 0)
    for x in live:
        if x.doctor_id:
            by_doc[x.doctor_id]["visits"] += 1
    certs = db.scalars(select(GiftCertificate).where(GiftCertificate.sold_at >= a, GiftCertificate.sold_at < b)).all()
    subs = db.scalars(select(Subscription).where(Subscription.sold_at >= a, Subscription.sold_at < b)).all()
    from .sales import CERT, BALANCE, SUB, BONUS
    revenue = round(sum(float(s.total or 0) for s in paid), 2)
    return {
        "day": d,
        "patients": {"new": len(new_p), "total": len(pids), "came": len({x.patient_id for x in done})},
        "income": {"fact": revenue, "plan": round(sum(price(x) for x in live), 2), "discount": round(sum(float(s.discount or 0) for s in paid), 2),
                   "with_discount": sum(1 for s in paid if (s.discount or 0) > 0), "avg": round(revenue / len(paid), 2) if paid else 0,
                   "unpaid": round(sum(price(x) for x in live if not x.sale_id and x.start < datetime.now()), 2)},
        "created": {"n": len(created), "online": len(online), "admin": len(by_admin), "cleverbox": len(from_cb)},
        "cancel_day": {"n": len(cancelled), "sum": round(sum(price(x) for x in cancelled), 2)},
        "cancel_today": {"n": len(canc_today), "sum": round(sum(price(x) for x in canc_today), 2)},
        "no_show": {"n": len(no_show), "sum": round(sum(price(x) for x in no_show), 2)},
        "moved": {"n": len(moved), "sum": round(sum(price(x) for x in moved), 2)},
        "visits": {"n": len(live), "done": len(done)},
        "checks": {"n": len(paid), "sum": revenue, "refunded": len(refunded), "refund_sum": round(sum(float(s.total or 0) for s in refunded), 2),
                   "test": sum(1 for s in paid if (s.fiscal or {}).get("test_sale"))},
        "kinds": {k: {"n": round(v["n"], 2), "sum": round(v["sum"], 2)} for k, v in kinds.items()},
        "certs_sold": {"n": len(certs), "sum": round(sum(float(c.price or 0) for c in certs), 2)},
        "subs_sold": {"n": len(subs), "sum": round(sum(float(c.price or 0) for c in subs), 2)},
        "pay": dict(sorted(((k, round(v, 2)) for k, v in pay.items()), key=lambda kv: -kv[1])),
        "non_money": {k: round(pay.get(k, 0), 2) for k in (CERT, SUB, BALANCE, BONUS) if pay.get(k)},
        "doctors": sorted(({"name": docs.get(i, "—"), **{k: round(v, 2) if isinstance(v, float) else v for k, v in x.items()}}
                           for i, x in by_doc.items()), key=lambda z: -(z["services"] + z["products"])),
    }


def _tz_shift() -> int:
    """created_at / AuditLog пишуться в UTC — переводимо в київський час для дати дня."""
    from zoneinfo import ZoneInfo
    off = datetime.now(ZoneInfo("Europe/Kyiv")).utcoffset()
    return int(off.total_seconds() // 3600) if off else 2
