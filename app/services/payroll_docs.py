"""Нарахування зарплати документами за період (як у CleverBox «Нарахування зарплати»).

Алгоритм для кожного співробітника з окладом у картці (Doctor.extra["salary"] = назва окладу):
  період ділиться на частини по календарних місяцях; для кожної частини —
  • фіксована ставка: за день × робочі дні з «Графіка співробітників» у періоді (немає змін у графіку — ставка 0),
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
        items: list = []
        shifts: list = []
        if pl:
            for a, b in _chunks(d_from, d_to):
                lns = [ln for ln in lines if a <= ln["day"] <= b]
                days = {dd for (did, dd) in sh if did == d.id and a <= dd <= b}     # робочі дні — лише з графіка співробітників
                hours = sum(s.minutes for (did, dd), s in sh.items() if did == d.id and a <= dd <= b) / 60
                cl = [ln for v in clinic.values() for ln in v if a <= ln["day"] <= b]
                res = spl.accrue(pl, lns, days, hours, cl)
                if pl["comm"]["on"]:                                   # розбивка по кожній позиції
                    for ln in lns:
                        if pl["comm"]["no_disc"] and ln["type"] == "product" and ln.get("disc"):
                            items.append({**_it(ln), "rate": "без % (знижка)", "sum": 0.0})
                            continue
                        a_, v, m = spl.line_amount(pl, ln)
                        items.append({**_it(ln), "rate": _rate_label(v, m), "sum": round(a_, 2), "cost": round(spl.line_cost(pl, ln), 2)})
                fx = res["fixed"]
                if pl["fixed"]["on"] and pl["fixed"]["period"] == "month":      # місячна ставка — пропорційно частці місяця
                    mdays = calendar.monthrange(a.year, a.month)[1]
                    fx = round(pl["fixed"]["amount"] * ((b - a).days + 1) / mdays, 2)
                det["fixed"] += fx
                if pl["fixed"]["on"] and pl["fixed"]["period"] in ("day", "hour"):  # ставка по кожній зміні графіка
                    for dd in sorted(days):
                        z = sh[(d.id, dd)]
                        hrs = round(z.minutes / 60, 2)
                        shifts.append({"day": dd.isoformat(), "start": z.start, "end": z.end, "hours": hrs,
                                       "sum": round(pl["fixed"]["amount"] * (hrs if pl["fixed"]["period"] == "hour" else 1), 2)})
                det["comm_services"] += res["comm_services"]
                det["comm_products"] += res["comm"] - res["comm_services"]
                det["turn"] += res["turn"]
                det["hours"] += hours
                days_all |= days
        det = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in det.items()}
        det["items"] = sorted(items, key=lambda x: x["at"])
        det["shifts"] = shifts
        det["by_schedule"] = bool(days_all)
        det["needs_schedule"] = bool(pl and pl["fixed"]["on"] and pl["fixed"]["period"] in ("day", "hour") and not days_all)
        det["period_days"] = (d_to - d_from).days + 1
        det["min_day_extra"] = round(det["comm_services"] + det["comm_products"] - sum(x["sum"] for x in items), 2)
        det["plan_fixed"] = (f"{pl['fixed']['amount']:g} ₴ " + {"day": "за день", "month": "за місяць", "hour": "за годину"}[pl["fixed"]["period"]]) \
            if pl and pl["fixed"]["on"] else ""
        calc = round(det["fixed"] + det["comm_services"] + det["comm_products"] + det["turn"], 2)
        rows.append({"doctor": d, "plan": pl["name"] if pl else None, "days": len(days_all), "calc": calc, "amount": calc, "detail": det})
    rows.sort(key=lambda r: (not r["plan"], r["doctor"].name))
    return rows


def _it(ln: dict) -> dict:
    at = ln.get("at")
    return {"at": at.isoformat(timespec="minutes") if at else ln["day"].isoformat(), "name": ln["name"], "type": ln["type"],
            "qty": ln.get("qty") or 1, "paid": round(ln["paid"], 2), "ref": ln.get("ref") or ""}


def _rate_label(v: float, m: str) -> str:
    return f"{v:g} ₴ за од." if m == "fixed" else f"{v:g}% від {'оплати' if m == 'paid' else 'вартості'}"


def line_posted(ln: PayrollLine) -> bool:
    return bool(ln.posted) if ln.posted is not None else bool(ln.doc.posted)


def doc_state(doc: PayrollDoc) -> str:
    n = sum(1 for ln in doc.lines if line_posted(ln))
    return "all" if doc.lines and n == len(doc.lines) else ("some" if n else "none")


def balances(db: Session) -> dict[int, float]:
    """Баланс співробітника: нараховано в проведених документах − виплати зарплати (з дати першого документа)."""
    from .fin_articles import salary_names
    docs = db.scalars(select(PayrollDoc).where(PayrollDoc.deleted_at.is_(None))).all()
    out: dict = defaultdict(float)
    posted = [(d, ln) for d in docs for ln in d.lines if line_posted(ln)]
    if not posted:
        return out
    start = min(d.d_from for d, _ in posted)
    for _, ln in posted:
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
                                     amount=round(float(amt or 0), 2), detail=r["detail"], posted=False))
    doc.total = round(sum(ln.amount for ln in doc.lines), 2)
    db.add(doc)
    db.commit()
    return doc


def set_posted(db: Session, doc: PayrollDoc, on: bool, doctor_id: int | None = None) -> None:
    """На баланс / з балансу: весь документ або один рядок (співробітник)."""
    now = datetime.utcnow() if on else None
    for ln in doc.lines:
        if doctor_id is None or ln.doctor_id == doctor_id:
            ln.posted, ln.posted_at = on, now
    st = doc_state(doc)
    doc.posted = st == "all"
    doc.posted_at = now if doc.posted else None
    db.commit()
