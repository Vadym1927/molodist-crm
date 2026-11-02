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
    det = _details(db, d, docs, live, done, new_p, created, cancelled, canc_today, moved, no_show, paid, refunded, logs, doctor_id)
    return {"det": det,
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


# ---------------- деталізація (провалитися в показник) ----------------
SRC = {"online": "Онлайн-запис", "bot": "Бот", "crm": "Адміністратор", "appeal": "Зі звернення", "cleverbox": "CleverBox"}
TZ = None


def _loc(dt: datetime | None) -> datetime | None:
    return dt + timedelta(hours=_tz_shift()) if dt else None


def _lead(start: datetime, at: datetime | None) -> str:
    """За скільки часу до візиту: «20 хв», «5 год 10 хв», «28 дн»; після початку — «після початку»."""
    if not at:
        return "—"
    m = int((start - at).total_seconds() // 60)
    if m < 0:
        return "після початку"
    if m < 60:
        return f"{m} хв"
    if m < 24 * 60:
        return f"{m // 60} год {m % 60:02d} хв"
    return f"{m // 1440} дн {(m % 1440) // 60} год"


def _ap_row(a: Appointment, docs: dict, users: dict, **extra) -> dict:
    svc = [str(x.get("name") or "—") for x in a.services or []]
    return {"id": a.id, "pid": a.patient_id, "patient": a.patient.full_name if a.patient else "—",
            "phone": a.patient.phone if a.patient else "", "start": a.start, "doctor": docs.get(a.doctor_id, "—"),
            "services": svc, "sum": round(sum(float(x.get("price") or 0) for x in a.services or []), 2),
            "author": users.get(a.created_by, "—") if a.created_by else ("онлайн" if a.source in ("online", "bot") else "—"),
            "source": SRC.get(a.source, a.source or "—"), "status": a.status, "sale_id": a.sale_id, **extra}


def _details(db: Session, d: date, docs: dict, live, done, new_p, created, cancelled, canc_today, moved, no_show,
             paid, refunded, logs, doctor_id) -> dict:
    from ..models import Product, SaleItem, User, CbService
    users = {u.id: u.name for u in db.scalars(select(User))}
    # хто й коли скасував / переніс (журнал дій по пацієнтах цих візитів)
    pids = {a.patient_id for a in list(cancelled) + list(canc_today) + list(moved)}
    st_logs = db.scalars(select(AuditLog).where(AuditLog.entity == "patient", AuditLog.entity_id.in_(pids or [0]),
                                                AuditLog.action.in_(("appointment.status", "appointment.save")))
                         .order_by(AuditLog.at)).all() if pids else []
    canc_by: dict[int, AuditLog] = {}
    move_by: dict[int, AuditLog] = {}
    for l in st_logs:
        det = l.details or {}
        aid = int(det.get("appt") or 0)
        if l.action == "appointment.status" and det.get("status") == "cancelled":
            canc_by[aid] = l
        elif l.action == "appointment.save" and det.get("moved") and _loc(l.at).date() == d:
            move_by[aid] = l

    def canc_row(a):
        l = canc_by.get(a.id)
        at = _loc(l.at) if l else _loc(a.updated_at)
        return _ap_row(a, docs, users, cancelled_at=at, lead=_lead(a.start, at),
                       cancelled_by=(l.user.name if l and l.user else "—"), reason=a.cancel_reason or "")

    def move_row(a):
        l = move_by.get(a.id)
        det = (l.details or {}) if l else {}
        frm = None
        try:
            frm = datetime.fromisoformat(det["frm"]) if det.get("frm") else None
        except ValueError:
            pass
        return _ap_row(a, docs, users, moved_at=_loc(l.at) if l else None, frm=frm,
                       frm_doc=docs.get(det.get("frm_doc")) if det.get("frm_doc") else None,
                       moved_by=(l.user.name if l and l.user else "—"))

    out = {
        "new": [_ap_row(a, docs, users) for a in live if a.patient_id in new_p],
        "visits": [_ap_row(a, docs, users) for a in sorted(live, key=lambda x: x.start)],
        "created": [_ap_row(a, docs, users, created_at=_loc(a.created_at)) for a in sorted(created, key=lambda x: x.created_at)],
        "cancel_day": [canc_row(a) for a in sorted(cancelled, key=lambda x: x.start)],
        "cancel_today": [canc_row(a) for a in sorted(canc_today, key=lambda x: x.start)],
        "moved": [move_row(a) for a in sorted(moved, key=lambda x: x.start)],
        "no_show": [_ap_row(a, docs, users) for a in sorted(no_show, key=lambda x: x.start)],
    }
    # дохід: план по візитах дня і факт по чеках
    sale_by = {s.id: s for s in paid}
    out["income"] = [dict(_ap_row(a, docs, users), paid=round(float(sale_by[a.sale_id].total or 0), 2) if a.sale_id in sale_by else None,
                          discount=round(float(sale_by[a.sale_id].discount or 0), 2) if a.sale_id in sale_by else None)
                     for a in sorted(live, key=lambda x: x.start)]
    # чеки
    def sale_row(s):
        p = s.patient
        return {"id": s.id, "at": s.at, "patient": p.full_name if p else "—", "pid": s.patient_id, "total": round(float(s.total or 0), 2),
                "discount": round(float(s.discount or 0), 2), "author": users.get(s.created_by, "—"),
                "pay": ", ".join(f"{x.method} {float(x.amount or 0):,.2f}".replace(",", " ") for x in s.payments),
                "items": ", ".join(f"{i.name}{(' ×%g' % i.qty) if (i.qty or 1) != 1 else ''}" for i in s.items),
                "test": bool((s.fiscal or {}).get("test_sale")), "reason": s.refund_reason or "",
                "refunded_at": s.refunded_at}
    out["checks"] = [sale_row(s) for s in sorted(paid, key=lambda x: x.at)]
    out["refunds"] = [sale_row(s) for s in sorted(refunded, key=lambda x: x.at)]
    # послуги й товари з собівартістю
    from . import techcards as tc
    uc = tc.unit_costs(db)
    fixed = {c.id: c.cost_fixed for c in db.scalars(select(CbService).where(CbService.cost_fixed.is_not(None)))}
    svc: dict[str, dict] = {}
    prod: dict[str, dict] = {}
    for s in paid:
        costs = {k: list(v) for k, v in tc.sale_service_costs(db, s).items()} if any(i.kind == "service" for i in s.items) else {}
        for it in s.items:
            if doctor_id and it.doctor_id != doctor_id:
                continue
            q = float(it.qty or 1)
            if it.kind == "service":
                c = costs.get(it.ref_id).pop(0) if costs.get(it.ref_id) else (float(fixed[it.ref_id]) * q if it.ref_id in fixed else None)
                g = svc.setdefault(it.name, {"name": it.name, "qty": 0.0, "sum": 0.0, "discount": 0.0, "cost": 0.0, "cost_known": True,
                                            "doctors": set(), "patients": []})
            elif it.kind == "product":
                c = uc.get(it.ref_id, 0) * q if it.ref_id and uc.get(it.ref_id) else None
                g = prod.setdefault(it.name, {"name": it.name, "qty": 0.0, "sum": 0.0, "discount": 0.0, "cost": 0.0, "cost_known": True,
                                             "doctors": set(), "patients": [], "price": float(it.price or 0),
                                             "unit_cost": uc.get(it.ref_id) if it.ref_id else None})
            else:
                continue
            g["qty"] += q
            g["sum"] += float(it.total or 0)
            g["discount"] += float(it.discount or 0)
            if c is None:
                g["cost_known"] = False
            else:
                g["cost"] += c
            if it.doctor_id:
                g["doctors"].add(docs.get(it.doctor_id, "—"))
            if s.patient:
                g["patients"].append(s.patient.full_name)

    def fin(rows):
        res = []
        for g in sorted(rows.values(), key=lambda x: -x["sum"]):
            g = dict(g, doctors=", ".join(sorted(g["doctors"])), patients=", ".join(dict.fromkeys(g["patients"])))
            for k in ("qty", "sum", "discount", "cost"):
                g[k] = round(g[k], 2)
            g["margin"] = round(g["sum"] - g["cost"], 2)
            g["margin_pct"] = round(100 * g["margin"] / g["sum"], 1) if g["sum"] else None
            res.append(g)
        return res
    out["services"], out["products"] = fin(svc), fin(prod)
    # оплати сертифікатами / абонементами, поповнення рахунку
    from .sales import CERT, SUB
    pays = {"cert": [], "sub": []}
    for s in paid:
        for x in s.payments:
            k = "cert" if x.method == CERT else "sub" if x.method == SUB else None
            if k:
                pays[k].append({"id": s.id, "at": s.at, "patient": s.patient.full_name if s.patient else "—", "pid": s.patient_id,
                                "amount": round(float(x.amount or 0), 2), "ref": x.cert_id or x.sub_id, "items": ", ".join(i.name for i in s.items)})
    out["cert_pay"], out["sub_pay"] = pays["cert"], pays["sub"]
    sold = {"cert": [], "sub": [], "topup": []}
    for s in paid:
        for it in s.items:
            if it.kind in ("service", "product"):
                continue
            k = "cert" if _has(it.name, CERT_W) or it.kind == "certificate" else "sub" if _has(it.name, SUB_W) else "topup" if _has(it.name, TOPUP_W) else None
            if k:
                sold[k].append({"id": s.id, "at": s.at, "patient": s.patient.full_name if s.patient else "—", "pid": s.patient_id,
                                "name": it.name, "amount": round(float(it.total or 0), 2)})
    out["cert_sold"], out["sub_sold"], out["topup"] = sold["cert"], sold["sub"], sold["topup"]
    return out
