"""Нарахування зарплати документами за період (як у CleverBox «Нарахування зарплати»).

Алгоритм для кожного співробітника з окладом у картці (Doctor.extra["salary"] = назва окладу):
  період ділиться на частини по календарних місяцях; для кожної частини —
  • фіксована ставка: за день × робочі дні графіка в періоді (немає графіка — дні з послугами/продажами),
    за годину × години змін графіка, за місяць — пропорційно частці місяця в періоді (1–15 з 31 → 15/31);
  • комісійні: за кожну позицію чеків, де співробітник — ініціатор (CleverBox) або спеціаліст у продажі CRM:
    окрема ставка (послуга / категорія / товар / бренд) або загальна для послуг / товарів / сертифікатів / абонементів;
    % від вартості, % від оплати або фіксована сума за одиницю; «мінімум за день» — більше з мінімуму й комісійних за день;
  • % від обороту клініки (за потреби лише в робочі дні співробітника).
Нараховане можна виправити вручну перед збереженням; «На баланс» додає суми до балансу співробітника
(баланс = нараховано в проведених документах − виплати зарплати у фінопераціях з дати першого такого документа)."""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Doctor, FinOp, PayrollDoc, PayrollLine


def default_period(today: date | None = None) -> tuple[date, date]:
    t = today or date.today()
    last = calendar.monthrange(t.year, t.month)[1]
    return (t.replace(day=16), t.replace(day=last)) if t.day >= 16 else (t.replace(day=1), t.replace(day=15))


def _chunks(d_from: date, d_to: date):
    a = d_from
    while a <= d_to:
        last = date(a.year, a.month, calendar.monthrange(a.year, a.month)[1])
        b = min(last, d_to)
        yield a, b
        a = b + timedelta(days=1)


def compute(db: Session, d_from: date, d_to: date) -> list[dict]:
    from . import salary_plans as spl
    from .finops import _staff_lines
    from .schedule import shifts as _shifts
    doctors = db.scalars(select(Doctor).order_by(Doctor.name)).all()
    plans = {d.id: spl.by_name(db, (d.extra or {}).get("salary")) for d in doctors}
    by_doc, clinic = _staff_lines(db, d_from, d_to, doctors)
    sh = _shifts(db, d_from, d_to)
    rows = []
    for d in doctors:
        pl = plans.get(d.id)
        lines = [ln for k, v in by_doc.items() if k[1] == d.id for ln in v if d_from <= ln["day"] <= d_to]
        if not pl and not lines and not d.active:
            continue
        if not pl and not (d.extra or {}).get("salary") and not lines:
            continue
        det = {"fixed": 0.0, "comm_services": 0.0, "comm_products": 0.0, "turn": 0.0, "hours": 0.0,
               "services": round(sum(ln["paid"] for ln in lines if ln["type"] == "service"), 2),
               "products": round(sum(ln["paid"] for ln in lines if ln["type"] == "product"), 2),
               "n": int(sum(ln["qty"] for ln in lines if ln["type"] == "service")), "by_schedule": True}
        days_all: set = set()
        if pl:
            for a, b in _chunks(d_from, d_to):
                lns = [ln for ln in lines if a <= ln["day"] <= b]
                days = {dd for (did, dd) in sh if did == d.id and a <= dd <= b}
                if not days:
                    days = {ln["day"] for ln in lns}
                    det["by_schedule"] = False if lns else det["by_schedule"]
                hours = sum(s.minutes for (did, dd), s in sh.items() if did == d.id and a <= dd <= b) / 60
                cl = [ln for v in clinic.values() for ln in v if a <= ln["day"] <= b]
                res = spl.accrue(pl, lns, days, hours, cl)
                fx = res["fixed"]
                if pl["fixed"]["on"] and pl["fixed"]["period"] == "month":      # місячна ставка — пропорційно частці місяця
                    mdays = calendar.monthrange(a.year, a.month)[1]
                    fx = round(pl["fixed"]["amount"] * ((b - a).days + 1) / mdays, 2)
                det["fixed"] += fx
                det["comm_services"] += res["comm_services"]
                det["comm_products"] += res["comm"] - res["comm_services"]
                det["turn"] += res["turn"]
                det["hours"] += hours
                days_all |= days
        det = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in det.items()}
        calc = round(det["fixed"] + det["comm_services"] + det["comm_products"] + det["turn"], 2)
        rows.append({"doctor": d, "plan": pl["name"] if pl else None, "days": len(days_all), "calc": calc, "amount": calc, "detail": det})
    rows.sort(key=lambda r: (not r["plan"], r["doctor"].name))
    return rows


def balances(db: Session) -> dict[int, float]:
    """Баланс співробітника: нараховано в проведених документах − виплати зарплати (з дати першого документа)."""
    from .fin_articles import salary_names
    docs = db.scalars(select(PayrollDoc).where(PayrollDoc.deleted_at.is_(None), PayrollDoc.posted.is_(True))).all()
    out: dict = defaultdict(float)
    if not docs:
        return out
    start = min(d.d_from for d in docs)
    for d in docs:
        for ln in d.lines:
            out[ln.doctor_id] += ln.amount
    for x in db.scalars(select(FinOp).where(FinOp.deleted_at.is_(None), FinOp.category.in_(salary_names(db)),
                                            FinOp.doctor_id.is_not(None), FinOp.day >= start)):
        out[x.doctor_id] -= x.amount if x.kind == "expense" else -x.amount
    return {k: round(v, 2) for k, v in out.items()}


def overlaps(db: Session, d_from: date, d_to: date, exclude: int | None = None) -> list[PayrollDoc]:
    q = select(PayrollDoc).where(PayrollDoc.deleted_at.is_(None), PayrollDoc.d_from <= d_to, PayrollDoc.d_to >= d_from)
    return [x for x in db.scalars(q) if x.id != exclude]


def save(db: Session, d_from: date, d_to: date, amounts: dict[int, float], comment: str, user_id: int | None) -> PayrollDoc:
    rows = compute(db, d_from, d_to)
    doc = PayrollDoc(d_from=d_from, d_to=d_to, comment=(comment or "").strip()[:2000] or None, created_by=user_id)
    for r in rows:
        amt = amounts.get(r["doctor"].id, r["calc"])
        doc.lines.append(PayrollLine(doctor_id=r["doctor"].id, plan=r["plan"], days=r["days"], calc=r["calc"],
                                     amount=round(float(amt or 0), 2), detail=r["detail"]))
    doc.total = round(sum(ln.amount for ln in doc.lines), 2)
    db.add(doc)
    db.commit()
    return doc


def set_posted(db: Session, doc: PayrollDoc, on: bool) -> None:
    doc.posted, doc.posted_at = on, (datetime.utcnow() if on else None)
    db.commit()
