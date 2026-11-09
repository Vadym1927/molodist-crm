"""Графік ФОПів для оплати: який лікар у який день працює на який ФОП → куди зайдуть гроші.

Шаблон тижня (день тижня → лікар → ФОП) + зміни на конкретні дати. Прогноз приходу по ФОП на дні вперед:
записи в календарі (не скасовані) × ціни послуг, по лікарю → ФОП цього лікаря в цей день.
Гроші заходять на термінали / готівку ФОП, далі компенсуються на рахунки, з яких платимо склад:
ФОП Колган → «Операційний Фонд», ФОП Бондар → «Амортизаційний + Операційний Фонд»."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Doctor, SyncState

KEY = "fop_schedule"
FOPS = ["ФОП Колган", "ФОП Бондар"]
FUND = {"ФОП Колган": "Операційний Фонд", "ФОП Бондар": "Амортизаційний + Операційний Фонд"}
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def load(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {"weekly": d.get("weekly") or {}, "dates": d.get("dates") or {}}


def save(db: Session, data: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(data, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def fop_for(cfg: dict, doctor_id: int, day: date) -> str | None:
    d = (cfg["dates"].get(day.isoformat()) or {}).get(str(doctor_id))
    if d is not None:
        return d or None
    return (cfg["weekly"].get(str(day.weekday())) or {}).get(str(doctor_id)) or None


def doctors(db: Session) -> list[Doctor]:
    from . import payroll_share
    return [d for d in db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name)) if payroll_share.role_of(d) == "doctor"]


def forecast(db: Session, d_from: date, d_to: date) -> dict:
    """{day: {fop|None: сума}} + по лікарях {day: {doctor_id: сума}} — записи × ціни."""
    from ..models import Appointment, CbService
    cfg = load(db)
    prices = {s.id: float(s.price or 0) for s in db.scalars(select(CbService))}
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    by_day: dict = defaultdict(lambda: defaultdict(float))
    by_doc: dict = defaultdict(lambda: defaultdict(float))
    for ap in db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start <= b,
                                                   Appointment.status.notin_(("cancelled", "no_show")))):
        tot = 0.0
        for s in ap.services or []:
            try:
                tot += float(s.get("price") or prices.get(int(s.get("id") or 0), 0) or 0)
            except (TypeError, ValueError):
                pass
        if not tot:
            continue
        day = ap.start.date()
        by_doc[day][ap.doctor_id] += tot
        by_day[day][fop_for(cfg, ap.doctor_id, day) if ap.doctor_id else None] += tot
    return {"by_day": {k: dict(v) for k, v in by_day.items()}, "by_doc": {k: dict(v) for k, v in by_doc.items()}}


def income_until(db: Session, d_to: date, today: date | None = None) -> dict[str, float]:
    """Скільки зайде на кожен ФОП від сьогодні до дати (включно)."""
    today = today or date.today()
    if d_to < today:
        return {f: 0.0 for f in FOPS}
    fc = forecast(db, today, d_to)["by_day"]
    out = {f: 0.0 for f in FOPS}
    for v in fc.values():
        for f, amt in v.items():
            if f in out:
                out[f] += amt
    return {k: round(v, 2) for k, v in out.items()}


TARGET_KEY = "oper_fund_targets"
TARGETS = {"ФОП Колган": 190000.0, "ФОП Бондар": 110000.0}     # норма операційного фонду (власник, 09.11.2026)
DEFICIT_MONTHS = 3
DEFICIT_FROM = date(2026, 11, 9)


def targets(db: Session) -> dict:
    st = db.get(SyncState, TARGET_KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {"targets": {f: float((d.get("targets") or {}).get(f, TARGETS[f])) for f in FOPS},
            "months": int(d.get("months") or DEFICIT_MONTHS), "from": date.fromisoformat(d["from"]) if d.get("from") else DEFICIT_FROM}


def set_targets(db: Session, tg: dict[str, float], months: int, start: date) -> None:
    st = db.get(SyncState, TARGET_KEY) or SyncState(key=TARGET_KEY)
    st.value, st.updated_at = json.dumps({"targets": tg, "months": months, "from": start.isoformat()}, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def fund_balances(db: Session) -> dict[str, float]:
    """Операційні гроші, з яких платимо склад: «Операційний Фонд» (Колган) повністю; у спільному
    «Амортизаційний + Операційний Фонд» (Бондар) — лише операційна частина (без грошей фонду амортизації)."""
    return {f: x["oper"] for f, x in fund_state(db).items()}


def fund_state(db: Session, today: date | None = None) -> dict:
    """По ФОП: залишок рахунку, операційна частина, норма, дефіцит і план компенсації (дефіцит закриваємо за N місяців)."""
    from . import amort, cashboxes as cbx
    today = today or date.today()
    tg = targets(db)
    boxes = {b["name"]: b for b in cbx.overview(db)}
    end = tg["from"]
    y, m = divmod(end.month - 1 + tg["months"], 12)
    import calendar as _cal
    end = date(end.year + y, m + 1, min(end.day, _cal.monthrange(end.year + y, m + 1)[1]))
    out = {}
    for f, name in FUND.items():
        box = boxes.get(name) or {}
        total = round(float(box.get("balance") or 0), 2)
        amort_part = amort.fund_part(db, box)["amort"] if box and "амортиз" in name.lower() else 0.0
        oper = round(total - amort_part, 2)
        target = tg["targets"][f]
        deficit = round(max(target - oper, 0), 2)
        days_left = max((end - today).days, 1)
        weeks_left = max(days_left / 7, 1)
        months_left = max(days_left / 30.4, 1)
        out[f] = {"box": name, "total": total, "amort": round(amort_part, 2), "oper": oper, "target": target, "deficit": deficit,
                  "pct": round(100 * oper / target, 1) if target else 100.0, "deadline": end,
                  "per_month": round(deficit / months_left, 2), "per_week": round(deficit / weeks_left, 2)}
    return out


def view(db: Session, start: date | None = None, days: int = 14) -> dict:
    start = start or date.today()
    cfg = load(db)
    docs = doctors(db)
    end = start + timedelta(days=days - 1)
    fc = forecast(db, start, end)
    rows = []
    for i in range(days):
        d = start + timedelta(days=i)
        bd = fc["by_doc"].get(d, {})
        rows.append({"day": d, "wd": WEEKDAYS[d.weekday()], "fop": fc["by_day"].get(d, {}),
                     "docs": [{"d": doc, "sum": round(bd.get(doc.id, 0), 2), "fop": fop_for(cfg, doc.id, d),
                               "override": str(doc.id) in (cfg["dates"].get(d.isoformat()) or {})} for doc in docs]})
    tot = defaultdict(float)
    for r in rows:
        for f, v in r["fop"].items():
            tot[f] += v
    return {"cfg": cfg, "docs": docs, "rows": rows, "tot": dict(tot), "FOPS": FOPS, "WEEKDAYS": WEEKDAYS, "start": start, "end": end,
            "balances": fund_balances(db), "FUND": FUND, "funds": fund_state(db), "tg": targets(db),
            "prev": start - timedelta(days=days), "next": start + timedelta(days=days), "today": date.today()}
