"""KPI лікарів — звіт ефективності за період (місяць) з порівнянням із попереднім.

Блоки по кожному лікарю (як у таблиці власника «KPI лікарі», але все з CRM):
  • виручка: послуги, товари (догляд), к-сть, середній чек, по напрямках (категоріях) з собівартістю й маржею;
  • пацієнти: унікальні, нові / повторні, постійні (за статусом), поверненість, конверсія нових у повтор,
    з рекомендацій, LTV 6 / 12 міс (виручка пацієнтів лікаря за останні 6 / 12 міс — разом і на пацієнта);
  • час: години за графіком, фактично в роботі, завантаження, простій, робочих днів, оборот за годину / день;
  • фінанси: з/п (оклад + комісійні, як у «Нарахуванні зарплати»), % косметика, податки клініки (з налаштувань),
    собівартість (техкарти, закупівля), амортизація апаратів (розділ «Амортизація»), прибуток, маржа, ROI,
    рентабельність і собівартість години;
  • бюті-плани: планів, сума, реалізовано, % реалізації, конверсія, записані далі;
  • медична дисципліна: % візитів з нотаткою, % пацієнтів з фото до/після;
  • зони покращення — автоматичні підказки за правилами; цілі й премія — з налаштувань (SyncState doctor_kpi);
  • пацієнти, які не перезаписані: були в лікаря за період і не мають наступного запису.
Джерела: чеки CleverBox + каса CRM (ініціатор позиції = лікар), візити CleverBox і записи CRM, графік змін,
оклади, техкарти, амортизація, бюті-плани, нотатки візитів, фото пацієнтів."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import (Check, Visit, Appointment, Doctor, Patient, PatientFile, VisitNote, StockMove, AmortAsset, SyncState,
                      SalaryAdj, Product)
from ..parsers.observ import norm

KEY = "doctor_kpi"
PREMIUM_ARTICLE = "Премія KPI"
DEFAULT_VISIT_MIN = 60          # тривалість візиту CleverBox, якщо кінець невідомий

# цілі (однакові для всіх, можна перевизначити по лікарю) і шкала премії
DEFAULT = {
    "targets": {"load_pct": 70, "products_share_pct": 15, "retention_pct": 80, "cancel_pct": 10, "discipline_pct": 85,
                "rebook_pct": 60, "margin_pct": 40, "plan_pct": 50},
    "ladder": [{"metric": "card_pct", "min": 85, "amount": 1000}, {"metric": "card_pct", "min": 90, "amount": 2000}],
    "doctors": {},          # doctor_id → {"taxes": сума/міс, "targets": {...перевизначення}, "revenue_target": ₴}
}
TARGET_LABELS = {"load_pct": "Завантаження, %", "products_share_pct": "Частка догляду в обороті, %", "retention_pct": "Поверненість, %",
                 "cancel_pct": "Скасувань не більше, %", "discipline_pct": "Медична дисципліна, %", "rebook_pct": "Перезаписані, %",
                 "margin_pct": "Чиста маржа, %", "plan_pct": "Реалізація бюті-планів, %"}
LADDER_METRICS = {"card_pct": "Заповнення карти (нотатки візитів), %", "photo_pct": "Фото до/після, %", "discipline_pct": "Дисципліна разом, %",
                  "load_pct": "Завантаження, %", "retention_pct": "Поверненість, %", "rebook_pct": "Перезаписані, %",
                  "revenue_pct": "Виконання плану обороту, %", "products_share_pct": "Частка догляду, %", "plan_pct": "Реалізація бюті-планів, %"}


# ---------------- налаштування ----------------
def settings(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    out = json.loads(json.dumps(DEFAULT))
    if st and st.value:
        try:
            v = json.loads(st.value)
            out["targets"].update(v.get("targets") or {})
            if isinstance(v.get("ladder"), list):
                out["ladder"] = v["ladder"]
            out["doctors"] = {str(k): x for k, x in (v.get("doctors") or {}).items()}
        except ValueError:
            pass
    return out


def save_settings(db: Session, data: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(data, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def targets_for(cfg: dict, doctor_id: int) -> dict:
    t = dict(cfg["targets"])
    t.update((cfg["doctors"].get(str(doctor_id)) or {}).get("targets") or {})
    return t


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def month_bounds(d_from: date, d_to: date):
    return datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())


def prev_period(d_from: date, d_to: date):
    from .reports import prev_period as pp
    return pp(d_from, d_to)


# ---------------- збір даних ----------------
def _blank() -> dict:
    return {"services": 0.0, "products": 0.0, "n_services": 0.0, "n_products": 0.0, "cost_services": 0.0, "cost_products": 0.0,
            "patients": set(), "pat_products": set(), "cats": defaultdict(lambda: {"n": 0.0, "sum": 0.0, "cost": 0.0}),
            "checks": set(), "visits": [], "done_keys": set(), "cancelled": 0, "booked": 0, "days": set(), "worked_min": 0.0,
            "amort": 0.0, "amort_procs": 0, "no_show": 0, "certs": 0.0, "n_certs": 0.0, "subs": 0.0, "n_subs": 0.0,
            "prods": defaultdict(lambda: {"qty": 0.0, "sum": 0.0, "cost": 0.0, "brand": ""})}


def _collect(db: Session, d_from: date, d_to: date, doctors: list[Doctor]) -> dict[int, dict]:
    """Усе по лікарях за період: позиції чеків, візити, амортизація."""
    from ..integrations.cleverbox import match_doctor
    from .finops import revenue_checks
    from .service_costs import Costs
    from .catalog import product_rows
    from .cb_services_import import service_index
    from . import autonomy
    a, b = month_bounds(d_from, d_to)
    agg: dict[int, dict] = defaultdict(_blank)
    cache: dict[str, Doctor | None] = {}

    def doc_of(name: str | None) -> Doctor | None:
        nm = (name or "").strip()
        if nm not in cache:
            cache[nm] = match_doctor(nm, doctors)
        return cache[nm]
    ref = db.info.get("_kpi_ref")                 # довідники — один раз на запит (звіт збирає кілька місяців)
    if not ref:
        ref = db.info["_kpi_ref"] = {"costs": Costs(db), "idx": service_index(db),
                                     "pcost": {norm(pr.product.name): (pr.cost or 0.0) for pr in product_rows(db, [])},
                                     "brand": {norm(p.name): (p.brand or "") for p in db.scalars(select(Product))}}
    costs, idx, pcost, brands = ref["costs"], ref["idx"], ref["pcost"], ref["brand"]
    for c in revenue_checks(db, a, b):
        for it in c.items or []:
            kind = it.get("item_type") or "other"
            if kind not in ("service", "product", "certificate", "subscription"):
                continue
            d = doc_of(it.get("initiator_name") or c.author)
            if not d:
                continue
            x = agg[d.id]
            name = (it.get("item_name") or it.get("name") or "—").strip()
            qty = _f(it.get("quantity")) or 1
            paid = _f(it.get("payd_amount") or it.get("total"))
            if kind in ("certificate", "subscription"):          # сертифікати й абонементи — окремо, не в оборот лікаря
                x["certs" if kind == "certificate" else "subs"] += paid
                x["n_certs" if kind == "certificate" else "n_subs"] += qty
                continue
            x["checks"].add(c.id)
            if c.patient_id:
                x["patients"].add(c.patient_id)
            if kind == "service":
                tc, fx = costs.of(name)
                cost = (tc or fx) * qty
                sv = idx.get(norm(name))
                cat = (it.get("category_name") or it.get("category") or (sv.category if sv else "") or "Без категорії").strip()
                x["services"] += paid
                x["n_services"] += qty
                x["cost_services"] += cost
                k = x["cats"][cat]
                k["n"] += qty
                k["sum"] += paid
                k["cost"] += cost
            else:
                cost = pcost.get(norm(name), 0.0) * qty
                x["products"] += paid
                x["n_products"] += qty
                x["cost_products"] += cost
                if c.patient_id:
                    x["pat_products"].add(c.patient_id)
                k = x["cats"]["Косметика"]
                k["n"] += qty
                k["sum"] += paid
                k["cost"] += cost
                pp = x["prods"][name]
                pp["qty"] += qty
                pp["sum"] += paid
                pp["cost"] += cost
                pp["brand"] = brands.get(norm(name), "")
    # візити: CleverBox до дати автономії + записи CRM
    now = datetime.now()
    for v in db.scalars(select(Visit).where(Visit.starts_at >= a, Visit.starts_at <= autonomy.cb_until(db, b))):
        d = doc_of(v.master_name)
        if not d:
            continue
        x = agg[d.id]
        if v.status == "cancelled":
            x["cancelled"] += 1
            if "не прийш" in (v.status_label or "").lower() or "no" in (v.status_label or "").lower():
                x["no_show"] += 1
        elif v.status == "done" or (v.source != "cb_service_sale" and v.starts_at < now):
            key = (v.patient_id, v.starts_at.date())
            if key not in x["done_keys"]:
                x["done_keys"].add(key)
                mins = (v.ends_at - v.starts_at).total_seconds() / 60 if v.ends_at and v.ends_at > v.starts_at else DEFAULT_VISIT_MIN
                x["visits"].append({"patient_id": v.patient_id, "at": v.starts_at, "min": mins, "note_key": f"cb:{v.id}"})
                x["worked_min"] += mins
                x["days"].add(v.starts_at.date())
                x["patients"].add(v.patient_id)
        else:
            x["booked"] += 1
    s_ = autonomy.since(db)
    if s_:
        a2 = max(a, datetime.combine(s_, datetime.min.time()))
        for ap in db.scalars(select(Appointment).where(Appointment.start >= a2, Appointment.start <= b)):
            if not ap.doctor_id:
                continue
            x = agg[ap.doctor_id]
            if ap.status in ("cancelled", "no_show"):
                x["cancelled"] += 1
                x["no_show"] += ap.status == "no_show"
            elif ap.status == "done" or ap.start < now:
                key = (ap.patient_id, ap.start.date())
                if key not in x["done_keys"]:
                    x["done_keys"].add(key)
                    mins = max((ap.end - ap.start).total_seconds() / 60, 0) if ap.end else DEFAULT_VISIT_MIN
                    x["visits"].append({"patient_id": ap.patient_id, "at": ap.start, "min": mins, "note_key": f"appt:{ap.id}"})
                    x["worked_min"] += mins
                    x["days"].add(ap.start.date())
                    if ap.patient_id:
                        x["patients"].add(ap.patient_id)
            else:
                x["booked"] += 1
    # амортизація апаратів: списання позицій амортизації → візит (proc:visit_id) або пацієнт + день → лікар
    assets = {aa.product_id: aa for aa in db.scalars(select(AmortAsset).where(AmortAsset.active.is_(True))) if aa.product_id}
    if assets:
        by_pd: dict[tuple, int] = {}
        for did, x in agg.items():
            for v in x["visits"]:
                by_pd.setdefault((v["patient_id"], v["at"].date()), did)
        vis_master: dict[int, str] = {}
        rows = db.execute(select(StockMove.product_id, StockMove.qty, StockMove.ref, StockMove.patient_id, StockMove.at)
                          .where(StockMove.product_id.in_(list(assets)), StockMove.qty < 0, StockMove.at >= a, StockMove.at <= b,
                                 StockMove.kind.in_(("procedure", "writeoff")))).all()
        vids = {int(m.group(1)) for _, _, ref, _, _ in rows if ref and (m := re.match(r"proc:(\d+):", ref))}
        if vids:
            vis_master = {v.id: v.master_name for v in db.scalars(select(Visit).where(Visit.id.in_(list(vids))))}
        seen_proc: dict[int, set] = defaultdict(set)
        for pid, qty, ref, patient_id, at in rows:
            did = None
            m = re.match(r"proc:(\d+):", ref or "")
            if m and int(m.group(1)) in vis_master:
                d = doc_of(vis_master[int(m.group(1))])
                did = d.id if d else None
            if did is None:
                did = by_pd.get((patient_id, at.date()))
            if did is None:
                continue
            agg[did]["amort"] += -float(qty or 0) * float(assets[pid].rate or 0)
            seen_proc[did].add((ref or "").split("#")[0].rsplit(":", 1)[0] or (patient_id, at.date()))
        for did, s in seen_proc.items():
            agg[did]["amort_procs"] = len(s)
    return agg


def _first_activity(db: Session, patient_ids: set[int]) -> dict[int, date]:
    """Перша активність пацієнта в клініці (чек або візит) — щоб визначити «новий»."""
    ids = [i for i in patient_ids if i]
    out: dict[int, date] = {}
    if not ids:
        return out
    for pid, dt in db.execute(select(Check.patient_id, func.min(Check.date)).where(Check.patient_id.in_(ids), Check.deleted.is_(False))
                              .group_by(Check.patient_id)):
        if dt:
            out[pid] = dt.date() if isinstance(dt, datetime) else datetime.fromisoformat(str(dt)).date()
    for pid, dt in db.execute(select(Visit.patient_id, func.min(Visit.starts_at)).where(Visit.patient_id.in_(ids), Visit.status != "cancelled")
                              .group_by(Visit.patient_id)):
        if dt:
            d = dt.date() if isinstance(dt, datetime) else datetime.fromisoformat(str(dt)).date()
            out[pid] = min(out.get(pid, d), d)
    for pid, dt in db.execute(select(Appointment.patient_id, func.min(Appointment.start))
                              .where(Appointment.patient_id.in_(ids), Appointment.status.notin_(("cancelled", "no_show"))).group_by(Appointment.patient_id)):
        if dt:
            d = dt.date() if isinstance(dt, datetime) else datetime.fromisoformat(str(dt)).date()
            out[pid] = min(out.get(pid, d), d)
    return out


def _visit_counts(db: Session, patient_ids: set[int], until: datetime) -> dict[int, int]:
    """Скільки візитів у пацієнта за весь час (до дати) — для статусу «постійний»."""
    ids = [i for i in patient_ids if i]
    out: dict[int, int] = defaultdict(int)
    if not ids:
        return out
    for pid, n in db.execute(select(Visit.patient_id, func.count(func.distinct(func.date(Visit.starts_at))))
                             .where(Visit.patient_id.in_(ids), Visit.status == "done", Visit.starts_at <= until).group_by(Visit.patient_id)):
        out[pid] += int(n or 0)
    for pid, n in db.execute(select(Appointment.patient_id, func.count(func.distinct(func.date(Appointment.start))))
                             .where(Appointment.patient_id.in_(ids), Appointment.status == "done", Appointment.start <= until).group_by(Appointment.patient_id)):
        out[pid] += int(n or 0)
    return out


def _ltv(db: Session, patient_ids: set[int], d_to: date, months: int) -> float:
    ids = [i for i in patient_ids if i]
    if not ids:
        return 0.0
    a = datetime.combine(d_to - timedelta(days=30 * months), datetime.min.time())
    b = datetime.combine(d_to, datetime.max.time())
    s = db.scalar(select(func.sum(Check.amount)).where(Check.patient_id.in_(ids), Check.deleted.is_(False), Check.date >= a, Check.date <= b)) or 0
    from ..models import Sale
    s2 = db.scalar(select(func.sum(Sale.total)).where(Sale.patient_id.in_(ids), Sale.status == "paid", Sale.at >= a, Sale.at <= b)) or 0
    return round(float(s) + float(s2), 2)


def _next_booked(db: Session, patient_ids: set[int], after: datetime) -> dict[int, datetime]:
    ids = [i for i in patient_ids if i]
    out: dict[int, datetime] = {}
    if not ids:
        return out
    for pid, dt in db.execute(select(Visit.patient_id, func.min(Visit.starts_at))
                              .where(Visit.patient_id.in_(ids), Visit.starts_at > after, Visit.status.notin_(("cancelled",))).group_by(Visit.patient_id)):
        if dt:
            out[pid] = dt if isinstance(dt, datetime) else datetime.fromisoformat(str(dt))
    for pid, dt in db.execute(select(Appointment.patient_id, func.min(Appointment.start))
                              .where(Appointment.patient_id.in_(ids), Appointment.start > after, Appointment.status.notin_(("cancelled", "no_show")))
                              .group_by(Appointment.patient_id)):
        if dt:
            d = dt if isinstance(dt, datetime) else datetime.fromisoformat(str(dt))
            out[pid] = min(out.get(pid, d), d)
    return out


# ---------------- розрахунок ----------------
def _pct(a: float, b: float) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def _metrics(db: Session, d: Doctor, x: dict, d_from: date, d_to: date, cfg: dict, sched: dict, salary: dict, plans: dict,
             prev_new: set[int] | None = None, with_lists: bool = True) -> dict:
    a, b = month_bounds(d_from, d_to)
    tg = targets_for(cfg, d.id)
    dcfg = cfg["doctors"].get(str(d.id)) or {}
    pats = {p for p in x["patients"] if p}
    first = _first_activity(db, pats)
    new = {p for p in pats if first.get(p) and first[p] >= d_from}
    old = pats - new
    from .patient_tags import tags as _tags
    levels = _tags(db)
    reg_min = next((t["min"] for t in levels if "постійн" in (t.get("name") or "").lower()), 2)
    vc = _visit_counts(db, pats, b)
    regular = {p for p in pats if vc.get(p, 0) >= reg_min}
    src = {p.id: (p.source or "") for p in db.scalars(select(Patient).where(Patient.id.in_(list(pats) or [0])))}
    recommended = {p for p in pats if "реком" in src.get(p, "").lower() or "recom" in src.get(p, "").lower()}
    conv = len(pats & prev_new) if prev_new else 0
    total = x["services"] + x["products"]
    n_checks = len(x["checks"])
    done = len(x["done_keys"])
    sched_min = sched.get("minutes", 0)
    sched_days = sched.get("days", 0)
    worked_h = round(x["worked_min"] / 60, 2)
    sched_h = round(sched_min / 60, 2)
    load = _pct(worked_h, sched_h) if sched_h else None
    sal = salary.get("calc", 0.0)
    comm_products = (salary.get("detail") or {}).get("comm_products", 0.0)
    taxes = _f(dcfg.get("taxes"))
    cost = x["cost_services"] + x["cost_products"]
    gross = total - cost
    profit = gross - sal - taxes - x["amort"]
    # дисципліна
    keys = [v["note_key"] for v in x["visits"]]
    noted = set()
    if keys:
        noted = {k for (k,) in db.execute(select(VisitNote.key).where(VisitNote.key.in_(keys)).distinct())}
    card_pct = _pct(len(noted), len(keys)) if keys else None
    photo_pats = set()
    if pats:
        photo_pats = {p for (p,) in db.execute(select(PatientFile.patient_id).where(PatientFile.patient_id.in_(list(pats)), PatientFile.kind == "photo",
                                                                                   PatientFile.created_at >= a, PatientFile.created_at <= b).distinct())}
    photo_pct = _pct(len(photo_pats), len(pats)) if pats else None
    disc_parts = [v for v in (card_pct, photo_pct) if v is not None]
    discipline = round(sum(disc_parts) / len(disc_parts), 1) if disc_parts else None
    # перезапис
    nb = _next_booked(db, pats, b) if with_lists else {}
    rebooked = {p for p in pats if p in nb}
    rebook_pct = _pct(len(rebooked), len(pats)) if pats else None
    cats = []
    for name, k in x["cats"].items():
        cats.append({"name": name, "n": k["n"], "sum": round(k["sum"], 2), "cost": round(k["cost"], 2),
                     "margin": round(100 * (k["sum"] - k["cost"]) / k["sum"], 1) if k["sum"] else None,
                     "share": _pct(k["sum"], total)})
    cats.sort(key=lambda z: -z["sum"])
    pl = plans.get(d.id)
    m = {
        "doctor": d, "doctor_id": d.id, "name": d.name,
        "services": round(x["services"], 2), "products": round(x["products"], 2), "total": round(total, 2),
        "n_services": x["n_services"], "n_products": x["n_products"], "checks": n_checks,
        "avg_check": round(total / n_checks, 2) if n_checks else 0.0,
        "avg_patient": round(total / len(pats), 2) if pats else 0.0,
        "avg_service": round(x["services"] / x["n_services"], 2) if x["n_services"] else 0.0,
        "products_share": _pct(x["products"], total), "pat_products": len(x["pat_products"]),
        "pat_products_pct": _pct(len(x["pat_products"]), len(pats)),
        "patients": len(pats), "new": len(new), "old": len(old), "regular": len(regular), "retention": _pct(len(old), len(pats)),
        "recommended": len(recommended), "conversion": conv, "conversion_pct": _pct(conv, len(prev_new)) if prev_new else None,
        "ltv6": _ltv(db, pats, d_to, 6), "ltv12": _ltv(db, pats, d_to, 12),
        "done": done, "cancelled": x["cancelled"], "booked": x["booked"],
        "cancel_pct": _pct(x["cancelled"], done + x["cancelled"]),
        "work_days": len(x["days"]), "sched_days": sched_days, "sched_h": sched_h, "worked_h": worked_h,
        "idle_h": round(max(sched_h - worked_h, 0), 2), "load": load,
        "rev_per_worked_h": round(total / worked_h, 2) if worked_h else 0.0,
        "rev_per_sched_h": round(total / sched_h, 2) if sched_h else 0.0,
        "rev_per_day": round(total / len(x["days"]), 2) if x["days"] else 0.0,
        "salary": round(sal, 2), "comm_products": round(comm_products, 2), "taxes": round(taxes, 2),
        "staff_cost": round(sal + taxes, 2), "cost": round(cost, 2), "cost_services": round(x["cost_services"], 2),
        "cost_products": round(x["cost_products"], 2), "amort": round(x["amort"], 2), "amort_procs": x["amort_procs"],
        "gross": round(gross, 2), "profit": round(profit, 2), "margin": _pct(profit, total) if total else None,
        "roi": round(profit / (sal + taxes), 2) if (sal + taxes) else None,
        "profit_per_h": round(profit / worked_h, 2) if worked_h else 0.0,
        "cost_per_h": round((cost + sal + taxes + x["amort"]) / worked_h, 2) if worked_h else 0.0,
        "card_pct": card_pct, "photo_pct": photo_pct, "discipline": discipline,
        "rebooked": len(rebooked), "rebook_pct": rebook_pct, "not_rebooked": len(pats) - len(rebooked),
        "cats": cats, "targets": tg, "no_show": x["no_show"], "certs": round(x["certs"], 2), "n_certs": x["n_certs"],
        "subs": round(x["subs"], 2), "n_subs": x["n_subs"],
        "visits_per_patient": round(done / len(pats), 2) if pats else 0.0,
        "contrib": round(x["services"] - x["cost_services"] - sal, 2),
        "contrib_pct": _pct(x["services"] - x["cost_services"] - sal, x["services"]) if x["services"] else None,
        "prods": sorted(({"name": k_, **v_, "profit": round(v_["sum"] - v_["cost"], 2)} for k_, v_ in x["prods"].items()), key=lambda z: -z["sum"]), "revenue_target": _f(dcfg.get("revenue_target")),
        "plans": {"n": pl.plans, "sum": round(pl.planned_sum, 2), "realized": round(pl.realized_sum, 2), "pct": pl.realization_pct,
                  "conv": pl.conversion_pct, "booked": pl.booked_plans, "red": pl.red, "started": pl.started_plans} if pl else
                 {"n": 0, "sum": 0.0, "realized": 0.0, "pct": 0.0, "conv": 0.0, "booked": 0, "red": 0, "started": 0},
        "_new": new, "_pats": pats,
    }
    m["plan_cover_pct"] = _pct(m["plans"]["n"], len(pats)) if pats else None
    m["revenue_pct"] = _pct(total, m["revenue_target"]) if m["revenue_target"] else None
    if with_lists:
        m["not_rebooked_list"] = _not_rebooked(db, x, pats, nb)
    return m


def _not_rebooked(db: Session, x: dict, pats: set[int], nb: dict[int, datetime]) -> list[dict]:
    last: dict[int, dict] = {}
    for v in sorted(x["visits"], key=lambda z: z["at"]):
        if v["patient_id"]:
            last[v["patient_id"]] = v
    ids = [p for p in pats if p not in nb]
    if not ids:
        return []
    out = []
    for p in db.scalars(select(Patient).where(Patient.id.in_(ids))):
        lv = last.get(p.id)
        out.append({"id": p.id, "name": p.full_name, "phone": p.phone or "", "last": lv["at"] if lv else None,
                    "days": (date.today() - lv["at"].date()).days if lv else None})
    out.sort(key=lambda z: (z["last"] or datetime.min), reverse=True)
    return out


def _zones(m: dict, prev: dict | None) -> list[dict]:
    """Зони покращення: правила → підказки з пріоритетом (1 — найважливіше)."""
    t = m["targets"]
    z: list[dict] = []

    def add(p, text, how=""):
        z.append({"p": p, "text": text, "how": how})
    if m["load"] is not None and m["load"] < t["load_pct"]:
        add(1, f"Завантаження {m['load']:.0f}% при цілі {t['load_pct']}% — {m['idle_h']:.0f} год простою за графіком",
            "Запропонувати вільні вікна пацієнтам без перезапису, скоротити графік або додати напрямок")
    if m["patients"] and m["products_share"] < t["products_share_pct"]:
        add(2, f"Догляд — лише {m['products_share']:.0f}% обороту (ціль {t['products_share_pct']}%); купили товари {m['pat_products']} з {m['patients']} пацієнтів",
            "Рекомендувати домашній догляд після кожної процедури, бюті-план з доглядом")
    if m["rebook_pct"] is not None and m["rebook_pct"] < t["rebook_pct"]:
        add(1, f"Перезаписано лише {m['rebook_pct']:.0f}% пацієнтів (ціль {t['rebook_pct']}%) — {m['not_rebooked']} без наступного візиту",
            "Записувати на наступний візит у кабінеті; список нижче — для адміністраторів")
    if m["patients"] and m["retention"] < t["retention_pct"]:
        add(2, f"Поверненість {m['retention']:.0f}% при цілі {t['retention_pct']}%",
            "Контроль нових пацієнтів: нагадування, бюті-план на першому візиті")
    if m["cancel_pct"] > t["cancel_pct"] and (m["done"] + m["cancelled"]) >= 5:
        add(2, f"Скасувань {m['cancel_pct']:.0f}% (ціль не більше {t['cancel_pct']}%) — {m['cancelled']} візитів",
            "Підтвердження за день, передоплата на дорогі процедури")
    if m["discipline"] is not None and m["discipline"] < t["discipline_pct"]:
        add(3, f"Медична дисципліна {m['discipline']:.0f}% (нотатки {m['card_pct'] or 0:.0f}%, фото {m['photo_pct'] or 0:.0f}%) при цілі {t['discipline_pct']}%",
            "Нотатка в кожному візиті, фото до/після в картці")
    if m["margin"] is not None and m["total"] and m["margin"] < t["margin_pct"]:
        add(1, f"Чиста маржа {m['margin']:.0f}% при цілі {t['margin_pct']}%", "Переглянути витрати матеріалів і ціни на напрямки з низькою маржею")
    low = [c for c in m["cats"] if c["margin"] is not None and c["sum"] >= 3000 and c["margin"] < 40]
    if low:
        add(2, "Низька маржа: " + ", ".join(f"{c['name']} {c['margin']:.0f}%" for c in low[:3]), "Перевірити норми списання й ціну послуги")
    if m["plans"]["n"] and m["plans"]["pct"] < t["plan_pct"]:
        add(2, f"Бюті-плани реалізовані на {m['plans']['pct']:.0f}% (ціль {t['plan_pct']}%), без руху — {m['plans']['red']}",
            "Нагадування за планом, запис на наступну процедуру плану")
    if m["patients"] and m["plan_cover_pct"] is not None and m["plan_cover_pct"] < 30:
        add(3, f"Бюті-план є лише у {m['plan_cover_pct']:.0f}% пацієнтів періоду", "Пропонувати план після консультації / OBSERV")
    if m["revenue_pct"] is not None and m["revenue_pct"] < 100:
        add(1, f"План обороту виконано на {m['revenue_pct']:.0f}% ({m['total']:,.0f} з {m['revenue_target']:,.0f} ₴)".replace(",", " "), "")
    if prev:
        if prev["avg_check"] and m["avg_check"] < prev["avg_check"] * 0.9:
            add(2, f"Середній чек впав на {100 - 100 * m['avg_check'] / prev['avg_check']:.0f}% проти минулого періоду", "Комплексні процедури, пакети, догляд у чек")
        if prev["total"] and m["total"] < prev["total"] * 0.85:
            add(1, f"Оборот менший за минулий період на {100 - 100 * m['total'] / prev['total']:.0f}%", "")
        if prev["patients"] and m["new"] < max(1, prev["new"] * 0.7) and prev["new"] >= 3:
            add(3, f"Нових пацієнтів {m['new']} проти {prev['new']} у минулому періоді", "Маркетинг на напрямки лікаря")
    z.sort(key=lambda q: q["p"])
    return z


def _premium(m: dict, cfg: dict) -> dict:
    """Премія за шкалою: по кожній метриці береться найвищий досягнутий поріг; разом — сума по метриках."""
    best: dict[str, dict] = {}
    for step in cfg.get("ladder") or []:
        met = step.get("metric")
        val = m.get(met) if met != "discipline_pct" else m.get("discipline")
        if met == "load_pct":
            val = m.get("load")
        if met == "retention_pct":
            val = m.get("retention")
        if met == "products_share_pct":
            val = m.get("products_share")
        if met == "plan_pct":
            val = m["plans"]["pct"]
        if val is None or _f(val) < _f(step.get("min")):
            continue
        if met not in best or _f(step.get("amount")) > _f(best[met]["amount"]):
            best[met] = {"metric": met, "label": LADDER_METRICS.get(met, met), "min": _f(step.get("min")), "amount": _f(step.get("amount")), "value": val}
    return {"steps": list(best.values()), "total": round(sum(_f(v["amount"]) for v in best.values()), 2)}


def _goals(m: dict) -> list[dict]:
    """Виконання цілей для шкали «ціль / факт»."""
    t = m["targets"]
    rows = [("load_pct", "Завантаження", m["load"], t["load_pct"], False), ("products_share_pct", "Догляд у обороті", m["products_share"], t["products_share_pct"], False),
            ("retention_pct", "Поверненість", m["retention"] if m["patients"] else None, t["retention_pct"], False),
            ("rebook_pct", "Перезаписані", m["rebook_pct"], t["rebook_pct"], False),
            ("cancel_pct", "Скасування", m["cancel_pct"] if (m["done"] + m["cancelled"]) else None, t["cancel_pct"], True),
            ("discipline_pct", "Дисципліна", m["discipline"], t["discipline_pct"], False),
            ("margin_pct", "Чиста маржа", m["margin"], t["margin_pct"], False),
            ("plan_pct", "Реалізація планів", m["plans"]["pct"] if m["plans"]["n"] else None, t["plan_pct"], False)]
    if m["revenue_target"]:
        rows.insert(0, ("revenue_pct", "План обороту", m["revenue_pct"], 100, False))
    out = []
    for key, label, val, target, inverse in rows:
        ok = None if val is None else (val <= target if inverse else val >= target)
        pct = None if val is None or not target else (round(100 * target / max(val, 0.01), 0) if inverse else round(100 * val / target, 0))
        out.append({"key": key, "label": label, "value": val, "target": target, "ok": ok, "pct": min(pct, 150) if pct is not None else None, "inverse": inverse})
    return out


def compute(db: Session, d_from: date, d_to: date, with_prev: bool = True, only: int | None = None) -> dict:
    """Повний звіт: rows по лікарях (із prev, зонами, премією), total."""
    from .schedule import shifts as _shifts, totals as _tot
    from .payroll_docs import compute as _payroll
    from .kpi import all_statuses, kpi_summary
    cfg = settings(db)
    doctors = [d for d in db.scalars(select(Doctor).order_by(Doctor.name)) if d.active or True]
    agg = _collect(db, d_from, d_to, doctors)
    sh = _shifts(db, d_from, d_to)
    sched = _tot(sh, [d.id for d in doctors])
    try:
        pay = {r["doctor"].id: r for r in _payroll(db, d_from, d_to)}
    except Exception:  # noqa: BLE001
        pay = {}
    _, by_doc = kpi_summary(all_statuses(db, d_from, d_to))
    plans = {k.doctor_id: k for k in by_doc if k.doctor_id}
    prev_rows: dict[int, dict] = {}
    if with_prev:
        p_from, p_to = prev_period(d_from, d_to)
        pagg = _collect(db, p_from, p_to, doctors)
        psh = _tot(_shifts(db, p_from, p_to), [d.id for d in doctors])
        try:
            ppay = {r["doctor"].id: r for r in _payroll(db, p_from, p_to)}
        except Exception:  # noqa: BLE001
            ppay = {}
        _, pby = kpi_summary(all_statuses(db, p_from, p_to))
        pplans = {k.doctor_id: k for k in pby if k.doctor_id}
        for d in doctors:
            if d.id in pagg and (only is None or d.id == only):
                prev_rows[d.id] = _metrics(db, d, pagg[d.id], p_from, p_to, cfg, psh.get(d.id, {}), ppay.get(d.id, {}), pplans, None, with_lists=False)
    rows = []
    for d in doctors:
        if d.id not in agg or (only is not None and d.id != only):
            continue
        x = agg[d.id]
        if not (x["services"] or x["products"] or x["done_keys"]):
            continue
        pv = prev_rows.get(d.id)
        m = _metrics(db, d, x, d_from, d_to, cfg, sched.get(d.id, {}), pay.get(d.id, {}), plans, pv["_new"] if pv else None)
        m["prev"] = {k: v for k, v in pv.items() if not k.startswith("_") and k not in ("doctor", "cats", "targets", "prods")} if pv else None
        m["zones"] = _zones(m, pv)
        m["premium"] = _premium(m, cfg)
        m["goals"] = _goals(m)
        m["score"] = _score(m)
        rows.append(m)
    rows.sort(key=lambda r: -r["total"])
    tot = _total(rows)
    prev_tot = None
    if prev_rows:
        pr_ = [dict(v, premium={"total": 0}) for v in prev_rows.values()]
        prev_tot = _total(pr_)
    return {"rows": rows, "total": tot, "prev_total": prev_tot, "d_from": d_from, "d_to": d_to, "cfg": cfg,
            "prev_range": prev_period(d_from, d_to) if with_prev else None}


def _score(m: dict) -> int:
    """Індекс ефективності 0–100: частка виконаних цілей."""
    g = [x for x in m["goals"] if x["ok"] is not None]
    return round(100 * sum(1 for x in g if x["ok"]) / len(g)) if g else 0


def _total(rows: list[dict]) -> dict:
    keys = ["services", "products", "total", "n_services", "n_products", "checks", "patients", "new", "old", "regular", "recommended",
            "done", "cancelled", "sched_h", "worked_h", "idle_h", "salary", "taxes", "cost", "amort", "gross", "profit", "ltv6", "ltv12",
            "not_rebooked", "rebooked", "comm_products"]
    t = {k: round(sum(_f(r.get(k)) for r in rows), 2) for k in keys}
    t["avg_check"] = round(t["total"] / t["checks"], 2) if t["checks"] else 0.0
    t["load"] = _pct(t["worked_h"], t["sched_h"]) if t["sched_h"] else None
    t["margin"] = _pct(t["profit"], t["total"]) if t["total"] else None
    t["products_share"] = _pct(t["products"], t["total"])
    t["retention"] = _pct(t["old"], t["patients"])
    t["rebook_pct"] = _pct(t["rebooked"], t["patients"]) if t["patients"] else None
    t["premium"] = round(sum(_f(r["premium"]["total"]) for r in rows), 2)
    pl_n = sum(r["plans"]["n"] for r in rows)
    pl_s = sum(r["plans"]["sum"] for r in rows)
    pl_r = sum(r["plans"]["realized"] for r in rows)
    t["plans"] = {"n": pl_n, "sum": round(pl_s, 2), "realized": round(pl_r, 2), "pct": _pct(pl_r, pl_s)}
    return t


# ---------------- премія → нарахування ----------------
def accrue_premium(db: Session, month: str, rows: list[dict], user_id: int | None) -> int:
    """Створює коригування «Премія KPI» за місяць (YYYY-MM) по лікарях із премією; повторно — не дублює."""
    n = 0
    for r in rows:
        amt = _f(r["premium"]["total"])
        if amt <= 0:
            continue
        ex = db.scalar(select(SalaryAdj).where(SalaryAdj.doctor_id == r["doctor_id"], SalaryAdj.month == month, SalaryAdj.article == PREMIUM_ARTICLE))
        txt = "; ".join(f"{i['label']} {_f(i['value']):.0f}% ≥ {i['min']:.0f}% → {i['amount']:,.0f} ₴".replace(",", " ") for i in r["premium"]["steps"])[:300]
        if ex:
            if abs(ex.amount - amt) > 0.5:
                ex.amount, ex.comment = amt, txt
                n += 1
            continue
        db.add(SalaryAdj(doctor_id=r["doctor_id"], month=month, amount=amt, comment=txt, article=PREMIUM_ARTICLE, created_by=user_id))
        n += 1
    from . import accrual_articles as aa
    if not any((x.get("name") or "") == PREMIUM_ARTICLE for x in aa.items(db)):
        try:
            aa.save(db, None, PREMIUM_ARTICLE, "income")
        except Exception:  # noqa: BLE001
            pass
    db.commit()
    return n


# ---------------- динаміка по місяцях ----------------
def series_all(db: Session, d_to: date, months: int = 6) -> dict[int, list[dict]]:
    """Оборот, пацієнти, завантаження по місяцях для всіх лікарів — міні-графік динаміки (один збір на місяць)."""
    from .schedule import shifts as _shifts, totals as _tot
    doctors = db.scalars(select(Doctor)).all()
    out: dict[int, list[dict]] = defaultdict(list)
    y, mth = d_to.year, d_to.month
    for i in range(months - 1, -1, -1):
        yy, mm = y, mth - i
        while mm < 1:
            yy, mm = yy - 1, mm + 12
        a = date(yy, mm, 1)
        b = date(yy + (mm == 12), (mm % 12) + 1, 1) - timedelta(days=1)
        agg = _collect(db, a, b, doctors)
        sh = _tot(_shifts(db, a, b), [d.id for d in doctors])
        for d in doctors:
            x = agg.get(d.id)
            tot = (x["services"] + x["products"]) if x else 0.0
            wh = (x["worked_min"] / 60) if x else 0.0
            sm = sh.get(d.id, {}).get("minutes", 0)
            out[d.id].append({"label": a.strftime("%m.%y"), "month": a, "total": round(tot, 2),
                              "services": round(x["services"] if x else 0, 2), "products": round(x["products"] if x else 0, 2),
                              "patients": len({p for p in x["patients"] if p}) if x else 0,
                              "load": _pct(wh, sm / 60) if sm else None})
    return out


# ---------------- історія з таблиці власника + CRM ----------------
HIST_FIELDS = [("total", "Оборот", "uah"), ("services", "Послуги", "uah"), ("products", "Догляд", "uah"), ("patients", "Пацієнтів", "int"),
               ("new", "Нових", "int"), ("load", "Завантаження", "pct"), ("worked_h", "Год у роботі", "num"),
               ("rev_per_worked_h", "Оборот / год", "uah"), ("salary", "З/П", "uah"), ("cost", "Собівартість", "uah"),
               ("profit", "Прибуток", "uah"), ("margin", "Маржа", "pct"), ("retention", "Поверненість", "pct"), ("ltv12", "LTV 12 міс", "uah")]


def _hist_seed() -> list[dict]:
    from pathlib import Path
    try:
        return json.loads((Path(__file__).with_name("doctor_kpi_history.json")).read_text("utf-8"))["rows"]
    except (OSError, ValueError, KeyError):
        return []


def _hist_key(db: Session, name: str, doctors: list[Doctor]) -> tuple[int | None, str]:
    from ..integrations.cleverbox import match_doctor
    d = match_doctor(name, doctors)
    if not d:                                           # «Дар'я Колган» ↔ «Колган Дар'я»
        parts = set(norm(name).split())
        d = next((x for x in doctors if parts and parts <= set(norm(x.name).split())), None)
    return (d.id if d else None), (d.name if d else name)


def sheet_history(db: Session) -> dict:
    """{ключ: {"doctor_id", "name", "months": {"YYYY-MM": row}}} — ключ doctor_id або імʼя з таблиці."""
    doctors = db.scalars(select(Doctor)).all()
    out: dict = {}
    for r in _hist_seed():
        did, nm = _hist_key(db, r["doctor"], doctors)
        k = did or nm
        x = out.setdefault(k, {"doctor_id": did, "name": nm, "months": {}})
        x["months"][f"{r['year']:04d}-{r['month']:02d}"] = {**r, "source": "sheet"}
    return out


def _month_range(a: date, b: date):
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def history(db: Session, key, until: date) -> dict:
    """Історія KPI лікаря по місяцях: таблиця власника, далі — CRM (місяці, яких у таблиці немає)."""
    sh = sheet_history(db)
    did = int(key) if str(key).isdigit() else None
    h = sh.get(did) if did else sh.get(key)
    if not h and did:
        d = db.get(Doctor, did)
        h = {"doctor_id": did, "name": d.name if d else str(key), "months": {}}
    if not h:
        return {"name": str(key), "rows": [], "doctor_id": None}
    months = dict(h["months"])
    y0, m0 = until.year, until.month - 11
    while m0 < 1:
        y0, m0 = y0 - 1, m0 + 12
    start = date(y0, m0, 1)                                   # CRM — останні 12 місяців
    if months:
        first = min(months)
        start = min(start, date(int(first[:4]), int(first[5:]), 1))
    if h["doctor_id"]:
        for y, m in _month_range(start, until):
            k = f"{y:04d}-{m:02d}"
            if k in months:
                continue
            a = date(y, m, 1)
            b = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
            r = compute(db, a, min(b, until), with_prev=False, only=h["doctor_id"])
            if r["rows"]:
                months[k] = {**{f: r["rows"][0].get(f) for f, _, _ in HIST_FIELDS}, "source": "crm"}
    rows = [{"key": k, "label": f"{k[5:]}.{k[2:4]}", **v} for k, v in sorted(months.items())]
    return {"name": h["name"], "doctor_id": h["doctor_id"], "rows": rows}


def archive(db: Session, d_from: date, active_ids: set, months: int = 12) -> list[dict]:
    """Спеціалісти з фактом за останні N місяців (таблиця + CRM), але без активності в поточному періоді."""
    since = d_from - timedelta(days=31 * months)
    sk = f"{since.year:04d}-{since.month:02d}"
    cur = f"{d_from.year:04d}-{d_from.month:02d}"
    out = []
    for k, h in sheet_history(db).items():
        if h["doctor_id"] in active_ids:
            continue
        ms = {m: r for m, r in h["months"].items() if sk <= m < cur}
        if not ms:
            continue
        last = max(ms)
        d = db.get(Doctor, h["doctor_id"]) if h["doctor_id"] else None
        out.append({"key": k, "name": h["name"], "position": (d.position if d else "") or "", "photo": d.photo_url if d else None,
                    "last": last, "last_label": f"{last[5:]}.{last[:4]}", "services": round(sum(_f(r.get("services")) for r in ms.values()), 2),
                    "profit": round(sum(_f(r.get("profit")) for r in ms.values()), 2), "months": len(ms)})
    out.sort(key=lambda z: z["last"], reverse=True)
    return out


def clinic_ltv(db: Session) -> dict:
    """LTV пацієнта клініки за всю історію CRM: середній і медіана виручки на пацієнта."""
    from ..models import Sale
    sums: dict[int, float] = defaultdict(float)
    for pid, s_ in db.execute(select(Check.patient_id, func.sum(Check.amount)).where(Check.patient_id.is_not(None), Check.deleted.is_(False))
                              .group_by(Check.patient_id)):
        sums[pid] += float(s_ or 0)
    for pid, s_ in db.execute(select(Sale.patient_id, func.sum(Sale.total)).where(Sale.patient_id.is_not(None), Sale.status == "paid")
                              .group_by(Sale.patient_id)):
        sums[pid] += float(s_ or 0)
    vals = sorted(v for v in sums.values() if v > 0)
    first = db.scalar(select(func.min(Check.date)).where(Check.deleted.is_(False)))
    if not vals:
        return {"avg": 0.0, "median": 0.0, "patients": 0, "total": 0.0, "since": first}
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {"avg": round(sum(vals) / n, 2), "median": round(med, 2), "patients": n, "total": round(sum(vals), 2), "since": first}


def cosmetics(rows: list[dict]) -> dict:
    """Косметика за період по всіх лікарях: продажі, валовий прибуток, маржа, SKU, топ / слабкі, топ по відповідальному."""
    allp: dict = {}
    for r in rows:
        for p in r.get("prods") or []:
            x = allp.setdefault(p["name"], {"name": p["name"], "brand": p["brand"], "qty": 0.0, "sum": 0.0, "cost": 0.0})
            x["qty"] += p["qty"]
            x["sum"] += p["sum"]
            x["cost"] += p["cost"]
    items = [{**x, "profit": round(x["sum"] - x["cost"], 2)} for x in allp.values() if x["sum"] > 0]
    items.sort(key=lambda z: -z["sum"])
    tot = round(sum(z["sum"] for z in items), 2)
    cost = round(sum(z["cost"] for z in items), 2)
    by_doc = [{"name": r["name"], "doctor_id": r["doctor_id"], "sum": r["products"], "qty": r["n_products"],
               "profit": round(r["products"] - r["cost_products"], 2), "top": (r.get("prods") or [])[:2]}
              for r in sorted(rows, key=lambda z: -z["products"]) if r["products"] > 0]
    return {"sum": tot, "cost": cost, "profit": round(tot - cost, 2), "margin": _pct(tot - cost, tot) if tot else None,
            "sku": len(items), "units": round(sum(z["qty"] for z in items), 2), "top": items[:5],
            "weak": sorted(items, key=lambda z: z["sum"])[:5] if len(items) > 5 else [], "by_doc": by_doc}
