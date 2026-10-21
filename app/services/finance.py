"""Розділ «Фінанси»: факт із чеків CleverBox + внесок бюті-планів + прогноз."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Check, BeautyPlan
from .kpi import all_statuses, forecast


@dataclass
class FinanceSummary:
    revenue: float = 0.0
    discount: float = 0.0
    checks: int = 0
    services: float = 0.0
    products: float = 0.0
    other: float = 0.0
    via_plans: float = 0.0                 # виручка від пацієнтів після їхнього бюті-плану
    by_staff: dict = field(default_factory=lambda: defaultdict(lambda: {"services": 0.0, "products": 0.0, "count": 0}))
    by_payment: dict = field(default_factory=lambda: defaultdict(float))
    months: list = field(default_factory=list)
    has_data: bool = False

    @property
    def avg_check(self):
        return round(self.revenue / self.checks) if self.checks else 0

    @property
    def plans_share(self):
        return round(100 * self.via_plans / self.revenue, 1) if self.revenue else 0.0


def _month_keys(end: date, n: int) -> list[str]:
    y, m = end.year, end.month
    keys = []
    for _ in range(n):
        keys.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def summary(db: Session, d_from: date, d_to: date) -> FinanceSummary:
    fs = FinanceSummary()
    start, end = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    plan_start = {}
    for p in db.scalars(select(BeautyPlan).where(BeautyPlan.status != "cancelled")):
        if p.session_at:
            plan_start[p.patient_id] = min(plan_start.get(p.patient_id, p.session_at), p.session_at)

    for c in db.scalars(select(Check).where(Check.deleted.is_(False), Check.date >= start, Check.date <= end)):
        fs.has_data = True
        fs.revenue += c.amount
        fs.discount += c.discount or 0
        fs.checks += 1
        if c.patient_id in plan_start and c.date >= plan_start[c.patient_id] - timedelta(days=1):
            fs.via_plans += c.amount
        for it in c.items or []:
            amt = float(it.get("payd_amount") or it.get("total") or 0)
            t = it.get("item_type")
            staff = it.get("initiator_name") or c.author or "—"
            if t == "service":
                fs.services += amt
                fs.by_staff[staff]["services"] += amt
                fs.by_staff[staff]["count"] += int(it.get("quantity") or 1)
            elif t == "product":
                fs.products += amt
                fs.by_staff[staff]["products"] += amt
            else:
                fs.other += amt
        for pay in c.payments or []:
            fs.by_payment[pay.get("payment_name") or "—"] += float(pay.get("amount") or 0)

    # помісячно: останні 12 місяців факту
    keys = _month_keys(d_to, 12)
    m_start = datetime(int(keys[0][:4]), int(keys[0][5:]), 1)
    agg = {k: {"month": k, "revenue": 0.0, "services": 0.0, "products": 0.0, "via_plans": 0.0} for k in keys}
    for c in db.scalars(select(Check).where(Check.deleted.is_(False), Check.date >= m_start, Check.date <= end)):
        k = f"{c.date.year}-{c.date.month:02d}"
        if k not in agg:
            continue
        agg[k]["revenue"] += c.amount
        for it in c.items or []:
            amt = float(it.get("payd_amount") or it.get("total") or 0)
            if it.get("item_type") == "service":
                agg[k]["services"] += amt
            elif it.get("item_type") == "product":
                agg[k]["products"] += amt
        if c.patient_id in plan_start and c.date >= plan_start[c.patient_id] - timedelta(days=1):
            agg[k]["via_plans"] += c.amount
    fs.months = list(agg.values())
    fs.by_staff = dict(sorted(fs.by_staff.items(), key=lambda kv: -(kv[1]["services"] + kv[1]["products"])))
    fs.by_payment = dict(sorted(fs.by_payment.items(), key=lambda kv: -kv[1]))
    return fs


def plan_forecast(db: Session, months: int = 6):
    return forecast(all_statuses(db), months=months)
