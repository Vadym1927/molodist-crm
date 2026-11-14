"""Перезапис за день: хто з пацієнтів, що були на прийомі в обраний день, уже має наступний запис, а хто — ні.

Був на прийомі — запис дня зі статусом «прийшов / оплачено / завершено» або з чеком. Перезаписаний — є будь-який
дійсний (не скасований, не «не прийшов») запис пізніше цього дня, до будь-якого спеціаліста.
«Записали в день візиту» — наступний запис створено того самого дня (адміністратор перезаписав на місці)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, Doctor

VISITED = ("done", "paid", "arrived")
KYIV = ZoneInfo("Europe/Kyiv")


def _local(dt: datetime | None) -> date | None:
    if not dt:
        return None
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(KYIV).date()      # created_at — UTC


def build(db: Session, d: date) -> dict:
    a, b = datetime.combine(d, datetime.min.time()), datetime.combine(d + timedelta(days=1), datetime.min.time())
    xs = [x for x in db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start < b,
                                                          Appointment.status.notin_(("cancelled", "no_show"))).order_by(Appointment.start))
          if x.patient_id and (x.status in VISITED or x.sale_id)]
    docs = {r.id: r.name for r in db.scalars(select(Doctor))}
    pids = {x.patient_id for x in xs}
    nxt: dict[int, Appointment] = {}
    if pids:
        for f in db.scalars(select(Appointment).where(Appointment.patient_id.in_(pids), Appointment.start >= b,
                                                      Appointment.status.notin_(("cancelled", "no_show"))).order_by(Appointment.start)):
            nxt.setdefault(f.patient_id, f)
    rows, seen = [], set()
    for x in xs:
        if x.patient_id in seen:
            continue
        seen.add(x.patient_id)
        n = nxt.get(x.patient_id)
        rows.append({"appt": x, "patient": x.patient, "doctor": docs.get(x.doctor_id, "—"), "doctor_id": x.doctor_id,
                     "services": ", ".join(s.get("name", "") for s in (x.services or []) if isinstance(s, dict)),
                     "next": n, "next_doctor": docs.get(n.doctor_id, "—") if n else None,
                     "same_day": bool(n and _local(n.created_at) == d)})
    by_doc = defaultdict(lambda: {"total": 0, "rebooked": 0, "same_day": 0})
    for r in rows:
        g = by_doc[r["doctor"]]
        g["total"] += 1
        g["rebooked"] += bool(r["next"])
        g["same_day"] += r["same_day"]
    total, reb = len(rows), sum(1 for r in rows if r["next"])
    doc_rows = sorted(({"doctor": k, **v, "pct": round(100 * v["rebooked"] / v["total"]) if v["total"] else 0} for k, v in by_doc.items()),
                      key=lambda z: (-z["total"], z["doctor"]))
    rows.sort(key=lambda r: (bool(r["next"]), r["doctor"], r["appt"].start))
    return {"day": d, "total": total, "rebooked": reb, "not_rebooked": total - reb, "same_day": sum(r["same_day"] for r in rows),
            "pct": round(100 * reb / total) if total else 0, "doctors": doc_rows, "rows": rows}
