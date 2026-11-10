"""Звіт «Не перезаписані пацієнти» — по кожному лікарю, по місяцях з початку року.

Пацієнт потрапляє в список лікаря, якщо його ОСТАННІЙ візит у клініці був у цього лікаря (в обраному місяці або раніше
з початку року) і зараз у нього немає дійсного майбутнього запису до жодного спеціаліста.
Кожен пацієнт у списку один раз — у місяці свого останнього візиту.

Показники по лікарю за місяць (когорта «були в лікаря цього місяця»):
  • пацієнтів, з них нових (перший візит у клініці);
  • повернулись або записані (був пізніший візит чи є майбутній запис) — % утримання;
  • нові, що повернулись / записані — % утримання нових (окремий показник KPI лікаря);
  • не перезаписані зараз, з них «гарячі» (до 30 днів), 31–60, 61–90 і втрачені (понад 90 днів);
  • потенціал: залишок бюті-планів цих пацієнтів і їхній середній чек у лікаря.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Doctor, Patient
from . import doctor_kpi as dk

MONTHS = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]
BUCKETS = [("hot", "до 30 днів", 0, 30), ("warm", "31–60 днів", 31, 60), ("cool", "61–90 днів", 61, 90), ("lost", "понад 90 днів", 91, 10 ** 6)]


def bucket(days: int) -> str:
    return next(k for k, _, a, b in BUCKETS if a <= days <= b)


def build(db: Session, year: int | None = None, month: int | None = None, doctor_id: int | None = None, today: date | None = None) -> dict:
    from . import kpi
    today = today or date.today()
    year = year or today.year
    d_from = date(year, 1, 1)
    d_to = min(today, date(year, 12, 31))
    last_m = d_to.month
    doctors = [d for d in db.scalars(select(Doctor).order_by(Doctor.name))]
    agg = dk._collect(db, d_from, d_to, doctors)
    visits: dict[int, list] = defaultdict(list)            # пацієнт → [(час, лікар)]
    for did, x in agg.items():
        for v in x["visits"]:
            if v["patient_id"] and v["at"].date() <= d_to:
                visits[v["patient_id"]].append((v["at"], did))
    for lst in visits.values():
        lst.sort()
    pats = set(visits)
    now = datetime.combine(today, datetime.min.time()) if today != date.today() else datetime.now()
    nb = dk._next_booked(db, pats, now)                      # дійсний майбутній запис
    first = dk._first_activity(db, pats)
    remain: dict[int, float] = defaultdict(float)
    for st in kpi.all_statuses(db):
        if st.plan.status == "active" and st.plan.patient_id in pats:
            remain[st.plan.patient_id] += max(float(st.remaining_sum or 0), 0)
    info = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_(list(pats) or [0])))}

    cells: dict[tuple, dict] = {}                           # (лікар, місяць) → показники когорти

    def cell(did, m):
        return cells.setdefault((did, m), {"patients": set(), "new": set(), "kept": set(), "new_kept": set(), "lost": []})
    rows = []
    for pid, lst in visits.items():
        seen = set()
        for i, (at, did) in enumerate(lst):
            key = (did, at.month)
            if key in seen:
                continue
            seen.add(key)
            c = cell(did, at.month)
            c["patients"].add(pid)
            is_new = bool(first.get(pid) and first[pid].year == year and first[pid].month == at.month)
            kept = pid in nb or any(a2.month > at.month or (a2.date() > at.date() and d2 != did) for a2, d2 in lst[i + 1:])
            if is_new:
                c["new"].add(pid)
            if kept:
                c["kept"].add(pid)
                if is_new:
                    c["new_kept"].add(pid)
        if pid in nb:
            continue
        at, did = lst[-1]                                    # останній візит у клініці — його лікар «веде» пацієнта
        days = (today - at.date()).days
        p = info.get(pid)
        r = {"id": pid, "name": p.full_name if p else f"#{pid}", "phone": (p.phone or "") if p else "", "last": at, "days": days,
             "bucket": bucket(days), "doctor_id": did, "month": at.month, "visits": sum(1 for _, d2 in lst if d2 == did), "visits_all": len(lst),
             "new": bool(first.get(pid) and first[pid] >= d_from and len(lst) == 1), "plan_left": round(remain.get(pid, 0)),
             "first": first.get(pid)}
        rows.append(r)
        cell(did, at.month)["lost"].append(r)

    docs = []
    for d in doctors:
        if doctor_id and d.id != doctor_id:
            continue
        ms = []
        for m in range(1, last_m + 1):
            c = cells.get((d.id, m))
            if not c:
                ms.append({"m": m, "patients": 0, "new": 0, "kept_pct": None, "new_kept_pct": None, "lost": 0, "list": [], "plan_left": 0})
                continue
            n, nn = len(c["patients"]), len(c["new"])
            lost = sorted(c["lost"], key=lambda r: r["last"], reverse=True)
            ms.append({"m": m, "patients": n, "new": nn, "kept": len(c["kept"]), "kept_pct": round(100 * len(c["kept"]) / n, 1) if n else None,
                       "new_kept": len(c["new_kept"]), "new_kept_pct": round(100 * len(c["new_kept"]) / nn, 1) if nn else None,
                       "lost": len(lost), "list": lost, "plan_left": sum(r["plan_left"] for r in lost)})
        if not any(x["patients"] for x in ms):
            continue
        allp = [r for x in ms for r in x["list"]]
        tot_p = len({pid for (did, _), c in cells.items() if did == d.id for pid in c["patients"]})
        docs.append({"doctor": d, "months": ms, "lost": len(allp), "patients": tot_p,
                     "by_bucket": {k: sum(1 for r in allp if r["bucket"] == k) for k, *_ in BUCKETS},
                     "new_lost": sum(1 for r in allp if r["new"]), "plan_left": sum(r["plan_left"] for r in allp),
                     "sel": sorted(([r for r in allp if r["month"] == month] if month else allp), key=lambda r: r["last"], reverse=True)})
    docs.sort(key=lambda x: -x["lost"])
    tot = {"lost": sum(x["lost"] for x in docs), "patients": len(pats), "booked": len([p for p in pats if p in nb]),
           "by_bucket": {k: sum(x["by_bucket"][k] for x in docs) for k, *_ in BUCKETS},
           "new_lost": sum(x["new_lost"] for x in docs), "plan_left": sum(x["plan_left"] for x in docs)}
    tot["booked_pct"] = round(100 * tot["booked"] / tot["patients"], 1) if tot["patients"] else None
    return {"year": year, "month": month, "last_m": last_m, "docs": docs, "tot": tot, "MONTHS": MONTHS, "BUCKETS": BUCKETS, "today": today,
            "doctors": [d for d in doctors if any(k[0] == d.id for k in cells)], "doctor_id": doctor_id}


def to_xlsx(v: dict) -> bytes:
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Не перезаписані"
    ws.append(["Лікар", "Пацієнт", "Телефон", "Останній візит", "Днів без запису", "Візитів у лікаря за рік", "Новий", "Залишок бюті-плану, ₴"])
    for d in v["docs"]:
        for r in d["sel"]:
            ws.append([d["doctor"].name, r["name"], r["phone"], r["last"].strftime("%d.%m.%Y"), r["days"], r["visits"], "так" if r["new"] else "", r["plan_left"] or ""])
    for col, w in zip("ABCDEFGH", (24, 34, 16, 14, 12, 12, 8, 16)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
