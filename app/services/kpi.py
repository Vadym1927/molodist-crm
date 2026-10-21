"""Реалізація бюті-планів, KPI та фінансовий прогноз.

Зіставлення: візит/продаж CleverBox -> позиція плану
  1) через Alias (ручне зіставлення назви плану з id послуги/товару CleverBox), або
  2) автоматично за збігом слів у назві (>= 60% значущих слів).
Кожна позиція «набирає» візити хронологічно, поки не набере qty.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, joinedload

from ..models import BeautyPlan, PlanItem, Alias, Visit, ProductSale, Patient, Doctor
from ..parsers.observ import norm
from ..config import settings

STOP = {"ДЛЯ", "З", "ТА", "І", "НА", "ОБЛИЧЧЯ", "МЛ", "ML", "ПРОЦЕДУРА", "ТЕРАПІЯ", "THE", "AND"}


def _words(s: str) -> set[str]:
    return {w for w in norm(s).split() if len(w) > 1 and w not in STOP and not w.isdigit()}


def name_match(plan_name: str, cb_name: str) -> float:
    a, b = _words(plan_name), _words(cb_name)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass
class ItemStatus:
    item: PlanItem
    done: int = 0
    booked: int = 0
    done_amount: float = 0.0
    last_done: datetime | None = None
    next_booked: datetime | None = None
    events: list = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(self.item.qty - self.done, 0)

    @property
    def unbooked(self) -> int:
        return max(self.item.qty - self.done - self.booked, 0)

    @property
    def realized_sum(self) -> float:
        return min(self.done, self.item.qty) * self.item.unit_price


@dataclass
class PlanStatus:
    plan: BeautyPlan
    items: list[ItemStatus]

    def _sum(self, kind=None, attr="realized_sum"):
        return sum(getattr(i, attr) for i in self.items if kind is None or i.item.kind == kind)

    @property
    def planned_sum(self):
        return sum(i.item.total for i in self.items)

    @property
    def realized_sum(self):
        return self._sum()

    @property
    def realization_pct(self):
        return round(100 * self.realized_sum / self.planned_sum, 1) if self.planned_sum else 0.0

    @property
    def booked_sum(self):
        return sum(min(i.booked, max(i.item.qty - i.done, 0)) * i.item.unit_price for i in self.items)

    @property
    def remaining_sum(self):
        return self.planned_sum - self.realized_sum

    @property
    def proc_planned(self):
        return sum(i.item.qty for i in self.items if i.item.kind == "procedure")

    @property
    def proc_done(self):
        return sum(min(i.done, i.item.qty) for i in self.items if i.item.kind == "procedure")

    @property
    def proc_booked(self):
        return sum(i.booked for i in self.items if i.item.kind == "procedure")

    @property
    def prod_planned(self):
        return sum(i.item.qty for i in self.items if i.item.kind == "product")

    @property
    def prod_bought(self):
        return sum(min(i.done, i.item.qty) for i in self.items if i.item.kind == "product")

    @property
    def next_booking(self) -> datetime | None:
        ds = [i.next_booked for i in self.items if i.next_booked]
        return min(ds) if ds else None

    @property
    def is_booked(self) -> bool:
        return self.next_booking is not None

    @property
    def is_complete(self) -> bool:
        return all(i.remaining == 0 for i in self.items if i.item.kind == "procedure") and self.proc_planned > 0

    @property
    def next_procedure(self) -> ItemStatus | None:
        for i in self.items:
            if i.item.kind == "procedure" and i.unbooked > 0:
                return i
        return None

    @property
    def last_activity(self) -> datetime | None:
        ds = [i.last_done for i in self.items if i.last_done]
        base = self.plan.session_at
        return max(ds + ([base] if base else [])) if (ds or base) else None

    @property
    def flag(self) -> str:
        """green — записана/все ок, yellow — без запису, але ще в межах інтервалу, red — прострочено, done."""
        if self.is_complete:
            return "done"
        if self.is_booked:
            return "green"
        nxt = self.next_procedure
        if not nxt:
            return "green"
        due = self.due_date
        if due and due < datetime.now() - timedelta(days=7):
            return "red"
        return "yellow"

    @property
    def due_date(self) -> datetime | None:
        """Коли має бути наступна процедура, якщо пацієнт не записаний."""
        nxt = self.next_procedure
        if not nxt:
            return None
        anchor = self.last_activity or datetime.now()
        if nxt.done == 0 and not any(i.done for i in self.items if i.item.kind == "procedure"):
            return anchor + timedelta(days=settings.remind_not_booked_after_days)
        return anchor + timedelta(days=nxt.item.interval_days or settings.default_procedure_interval_days)


def _aliases(db: Session) -> dict[tuple[str, str], Alias]:
    return {(a.kind, a.plan_name_norm): a for a in db.scalars(select(Alias))}


def compute_plan(db: Session, plan: BeautyPlan, aliases=None, now: datetime | None = None) -> PlanStatus:
    now = now or datetime.now()
    aliases = aliases if aliases is not None else _aliases(db)
    patient = plan.patient
    since = (plan.session_at or plan.created_at) - timedelta(days=1)

    # інші активні плани пацієнта з пізнішою датою «забирають» візити після своєї дати
    # (плани того самого дня ділять вікно — інакше план, створений о 00:00, «втрачає» візити плану о 17:22)
    later_plans = [p.session_at for p in patient.plans
                   if p.id != plan.id and p.status != "cancelled" and p.session_at and plan.session_at
                   and p.session_at.date() > plan.session_at.date()]
    until = min(later_plans) - timedelta(days=1) if later_plans else None

    statuses = [ItemStatus(item=it) for it in plan.items]

    def matches(st: ItemStatus, cb_id, cb_name) -> bool:
        al = aliases.get((st.item.kind, norm(st.item.name)))
        if al and al.cb_ids:
            return cb_id in al.cb_ids
        return bool(cb_name) and name_match(st.item.name, cb_name) >= 0.6

    used_visits: set[int] = set()
    visits = [v for v in patient.visits if v.starts_at >= since and (until is None or v.starts_at < until)]
    # дедуп: надана послуга (service_sale) і завершений візит (visit) можуть описувати те саме —
    # враховуємо «done» з service_sale, а з візитів беремо лише майбутні записи
    done_rows = [v for v in visits if v.status == "done"]
    booked_rows = [v for v in visits if v.status == "booked" and v.starts_at >= now - timedelta(hours=2)]

    for st in statuses:
        if st.item.kind != "procedure":
            continue
        for v in sorted(done_rows, key=lambda v: v.starts_at):
            if v.id in used_visits or st.done >= st.item.qty:
                continue
            if v.plan_item_id == st.item.id or (v.plan_item_id is None and matches(st, v.service_id, v.service_name)):
                used_visits.add(v.id)
                st.done += 1
                st.done_amount += v.amount or st.item.unit_price
                st.last_done = max(filter(None, [st.last_done, v.starts_at]))
                st.events.append(("done", v.starts_at, v.service_name, v.master_name))
        for v in sorted(booked_rows, key=lambda v: v.starts_at):
            if v.id in used_visits or st.done + st.booked >= st.item.qty:
                continue
            if v.plan_item_id == st.item.id or (v.plan_item_id is None and matches(st, v.service_id, v.service_name)):
                used_visits.add(v.id)
                st.booked += 1
                st.next_booked = min(filter(None, [st.next_booked, v.starts_at]))
                st.events.append(("booked", v.starts_at, v.service_name, v.master_name))
        if st.item.manual_done and st.done < st.item.manual_done:
            st.done = min(st.item.manual_done, st.item.qty)

    used_sales: set[int] = set()
    sales = [s for s in patient.sales if s.sold_at >= since and (until is None or s.sold_at < until)]
    for st in statuses:
        if st.item.kind != "product":
            continue
        for s in sorted(sales, key=lambda s: s.sold_at):
            if s.id in used_sales or st.done >= st.item.qty:
                continue
            if s.plan_item_id == st.item.id or (s.plan_item_id is None and matches(st, s.product_id, s.product_name)):
                used_sales.add(s.id)
                st.done += s.quantity
                st.done_amount += s.amount
                st.last_done = max(filter(None, [st.last_done, s.sold_at]))
                st.events.append(("bought", s.sold_at, s.product_name, None))
        if st.item.manual_done and st.done < st.item.manual_done:
            st.done = min(st.item.manual_done, st.item.qty)
            st.last_done = st.last_done or plan.session_at

    return PlanStatus(plan=plan, items=statuses)


# ---------------- Агрегати ----------------

@dataclass
class KpiRow:
    label: str
    doctor_id: int | None = None
    plans: int = 0
    active: int = 0
    completed: int = 0
    planned_sum: float = 0
    realized_sum: float = 0
    booked_sum: float = 0
    booked_plans: int = 0
    started_plans: int = 0           # хоч одна процедура виконана або засіб куплено
    red: int = 0
    proc_planned: int = 0
    proc_done: int = 0

    @property
    def realization_pct(self):
        return round(100 * self.realized_sum / self.planned_sum, 1) if self.planned_sum else 0.0

    @property
    def booked_pct(self):
        return round(100 * self.booked_plans / self.active, 1) if self.active else 0.0

    @property
    def conversion_pct(self):
        return round(100 * self.started_plans / self.plans, 1) if self.plans else 0.0

    @property
    def avg_plan(self):
        return round(self.planned_sum / self.plans) if self.plans else 0

    def add(self, ps: PlanStatus):
        self.plans += 1
        if ps.plan.status == "cancelled":
            return
        done = ps.is_complete or ps.plan.status == "completed"
        self.completed += int(done)
        self.active += int(not done)
        self.planned_sum += ps.planned_sum
        self.realized_sum += ps.realized_sum
        self.booked_sum += ps.booked_sum
        self.booked_plans += int(ps.is_booked and not done)
        self.started_plans += int(ps.realized_sum > 0)
        self.red += int(ps.flag == "red")
        self.proc_planned += ps.proc_planned
        self.proc_done += ps.proc_done


PLAN_LOAD = (selectinload(BeautyPlan.items), joinedload(BeautyPlan.doctor),
             selectinload(BeautyPlan.patient).selectinload(Patient.visits),
             selectinload(BeautyPlan.patient).selectinload(Patient.sales),
             selectinload(BeautyPlan.patient).selectinload(Patient.plans),
             selectinload(BeautyPlan.patient).joinedload(Patient.doctor))


def all_statuses(db: Session, date_from: date | None = None, date_to: date | None = None,
                 doctor_id: int | None = None) -> list[PlanStatus]:
    """Статуси всіх планів. Дані вантажаться пакетно (≈7 запитів замість тисяч), результат
    запамʼятовується в межах сесії/запиту — повторні виклики на одній сторінці безкоштовні."""
    memo = db.info.setdefault("plan_status_memo", {})
    if "all" not in memo or memo.get("version") != _data_version():
        plans = db.scalars(select(BeautyPlan).options(*PLAN_LOAD)).unique().all()
        aliases = _aliases(db)
        memo.clear()
        memo["version"] = _data_version()
        memo["all"] = [compute_plan(db, p, aliases) for p in plans]
    res = memo["all"]
    if doctor_id:
        res = [s for s in res if s.plan.doctor_id == doctor_id]
    if date_from:
        res = [s for s in res if s.plan.session_at and s.plan.session_at.date() >= date_from]
    if date_to:
        res = [s for s in res if s.plan.session_at and s.plan.session_at.date() <= date_to]
    return res


def status_by_plan(db: Session) -> dict[int, PlanStatus]:
    return {s.plan.id: s for s in all_statuses(db)}


# Лічильник змін даних: будь-який flush з планами/візитами/продажами інвалідує запамʼятовані статуси
_VERSION = {"n": 0}
_TRACKED = None


def _data_version() -> int:
    return _VERSION["n"]


def _install_listener():
    from sqlalchemy import event
    from ..models import SessionLocal

    @event.listens_for(SessionLocal, "after_flush")
    def _bump(session, ctx):
        tracked = (BeautyPlan, PlanItem, Visit, ProductSale, Patient, Alias, Doctor)
        if any(isinstance(o, tracked) for o in list(session.new) + list(session.dirty) + list(session.deleted)):
            _VERSION["n"] += 1


_install_listener()


def kpi_summary(statuses: list[PlanStatus]) -> tuple[KpiRow, list[KpiRow]]:
    total = KpiRow("Усі плани")
    by_doc: dict[str, KpiRow] = {}
    for ps in statuses:
        total.add(ps)
        name = ps.plan.doctor.name if ps.plan.doctor else "Без лікаря"
        by_doc.setdefault(name, KpiRow(name, ps.plan.doctor_id)).add(ps)
    return total, sorted(by_doc.values(), key=lambda r: -r.planned_sum)


# ---------------- Фінансовий прогноз ----------------

def forecast(statuses: list[PlanStatus], months: int = 6, include_care_rebuy: bool = True,
             now: datetime | None = None) -> list[dict]:
    """По місяцях: confirmed (вже є запис), expected (за інтервалами плану, без запису),
    care (повторна покупка догляду за тривалістю засобу)."""
    now = now or datetime.now()
    start = date(now.year, now.month, 1)
    keys = []
    y, m = start.year, start.month
    for _ in range(months):
        keys.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    buckets = {k: {"month": k, "confirmed": 0.0, "expected": 0.0, "care": 0.0} for k in keys}

    def put(dt: datetime, kind: str, amount: float):
        k = f"{dt.year}-{dt.month:02d}"
        if k in buckets:
            buckets[k][kind] += amount

    for ps in statuses:
        if ps.plan.status == "cancelled":
            continue
        for st in ps.items:
            it = st.item
            if it.kind == "procedure":
                for kind, dt, *_ in st.events:
                    if kind == "booked":
                        put(dt, "confirmed", it.unit_price)
                if st.unbooked:
                    interval = timedelta(days=it.interval_days or settings.default_procedure_interval_days)
                    anchor = max(filter(None, [st.last_done, st.next_booked, ps.plan.session_at, now - interval]))
                    # перша незабронована — не раніше ніж «завтра»
                    t = max(anchor + interval if (st.done or st.booked) else anchor + timedelta(days=7),
                            now + timedelta(days=1))
                    for _ in range(st.unbooked):
                        put(t, "expected", it.unit_price)
                        t += interval
            elif it.kind == "product":
                if st.done < it.qty:      # ще не купили — очікуємо в найближчі 2 тижні
                    put(now + timedelta(days=14), "expected", it.unit_price * (it.qty - st.done))
                elif include_care_rebuy and st.last_done and it.supply_days:
                    t = st.last_done + timedelta(days=it.supply_days)
                    while t < now + timedelta(days=31 * months):
                        if t >= now:
                            put(t, "care", it.unit_price)
                        t += timedelta(days=it.supply_days)
    rows = list(buckets.values())
    for r in rows:
        r["total"] = r["confirmed"] + r["expected"] + r["care"]
    return rows
