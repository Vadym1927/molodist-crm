"""JSON-подання сутностей — спільне для REST API і вебхуків."""
from __future__ import annotations

from .kpi import PlanStatus, KpiRow


def iso(dt):
    return dt.isoformat(timespec="minutes") if dt else None


def patient_brief(p) -> dict:
    return {"id": p.id, "full_name": p.full_name, "phone": p.phone, "cleverbox_id": p.cleverbox_id,
            "doctor": p.doctor.name if p.doctor else None}


def plan_dict(ps: PlanStatus, medical: bool = False) -> dict:
    pl = ps.plan
    nxt = ps.next_procedure
    d = {
        "id": pl.id, "patient": patient_brief(pl.patient), "doctor": pl.doctor.name if pl.doctor else None,
        "title": pl.title, "session_at": iso(pl.session_at), "status": pl.status, "flag": ps.flag,
        "complaints": pl.complaints,
        "totals": {"planned": ps.planned_sum, "realized": ps.realized_sum, "realization_pct": ps.realization_pct,
                   "booked": ps.booked_sum, "remaining": ps.remaining_sum},
        "procedures": {"planned": ps.proc_planned, "done": ps.proc_done, "booked": ps.proc_booked},
        "products": {"planned": ps.prod_planned, "bought": ps.prod_bought},
        "is_booked": ps.is_booked, "next_booking": iso(ps.next_booking),
        "next_procedure": nxt.item.name if nxt else None, "due_date": iso(ps.due_date),
        "items": [{
            "id": st.item.id, "kind": st.item.kind, "name": st.item.name, "qty": st.item.qty,
            "unit_price": st.item.unit_price, "total": st.item.total, "volume": st.item.volume,
            "usage": st.item.usage, "url": st.item.url, "interval_days": st.item.interval_days,
            "supply_days": st.item.supply_days, "done": st.done, "booked": st.booked, "remaining": st.remaining,
            "last_done": iso(st.last_done), "next_booked": iso(st.next_booked),
        } for st in ps.items],
    }
    if medical:
        d["medical_notes"] = pl.medical_notes
        d["routine_comment"] = pl.routine_comment
    return d


def kpi_dict(r: KpiRow) -> dict:
    return {"label": r.label, "doctor_id": r.doctor_id, "plans": r.plans, "active": r.active,
            "completed": r.completed, "planned_sum": r.planned_sum, "realized_sum": r.realized_sum,
            "realization_pct": r.realization_pct, "booked_sum": r.booked_sum, "booked_pct": r.booked_pct,
            "conversion_pct": r.conversion_pct, "avg_plan": r.avg_plan, "overdue": r.red,
            "procedures_planned": r.proc_planned, "procedures_done": r.proc_done}
