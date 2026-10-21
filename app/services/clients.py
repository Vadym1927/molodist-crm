"""Розділ «Клієнти»: сегменти під бюті-плани."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import Patient, Visit, ProductSale, Check
from .kpi import status_by_plan, PlanStatus

SEGMENTS = {
    "all":       "Усі",
    "plan":      "З бюті-планом",
    "attention": "Потребують уваги",
    "no_plan":   "Без плану (потенціал)",
    "sleeping":  "Сплячі > 90 днів",
    "new":       "Нові за 30 днів",
    "birthday":  "ДН найближчі 14 днів",
    "vip":       "VIP / топ за виручкою",
}


@dataclass
class ClientRow:
    patient: Patient
    plan_status: PlanStatus | None
    last_visit: datetime | None
    next_visit: datetime | None
    visits: int
    revenue: float

    @cached_property
    def segment_flags(self) -> set[str]:
        now = datetime.now()
        f = {"all"}
        if self.plan_status:
            f.add("plan")
            if self.plan_status.flag in ("red", "yellow") and not self.plan_status.is_booked:
                f.add("attention")
        elif self.visits:
            f.add("no_plan")
        if self.last_visit and self.last_visit < now - timedelta(days=90) and not self.next_visit:
            f.add("sleeping")
        if self.patient.created_at and self.patient.created_at > now - timedelta(days=30):
            f.add("new")
        b = self.patient.birthday
        if b:
            try:
                nb = b.replace(year=now.year)
            except ValueError:               # 29 лютого
                nb = b.replace(year=now.year, day=28)
            if nb < now.date():
                nb = nb.replace(year=now.year + 1)
            if (nb - now.date()).days <= 14:
                f.add("birthday")
        if self.patient.cb_mark == "vip" or self.vip:
            f.add("vip")
        return f

    vip: bool = False


def client_rows(db: Session, patients: list[Patient]) -> list[ClientRow]:
    now = datetime.now()
    ids = [p.id for p in patients]
    last, nxt, cnt, rev = {}, {}, defaultdict(int), defaultdict(float)
    for pid, mx, n in db.execute(select(Visit.patient_id, func.max(Visit.starts_at), func.count())
                                 .where(Visit.status.in_(("done", "attended")), Visit.starts_at <= now)
                                 .group_by(Visit.patient_id)):
        last[pid], cnt[pid] = mx, n
    for pid, mn in db.execute(select(Visit.patient_id, func.min(Visit.starts_at))
                              .where(Visit.status == "booked", Visit.starts_at > now).group_by(Visit.patient_id)):
        nxt[pid] = mn
    has_checks = False
    for pid, s, mx in db.execute(select(Check.patient_id, func.sum(Check.amount), func.max(Check.date))
                                 .where(Check.deleted.is_(False), Check.patient_id.is_not(None))
                                 .group_by(Check.patient_id)):
        has_checks = True
        rev[pid] = float(s or 0)
        if mx and (pid not in last or mx > last[pid]):
            last[pid] = mx
    if not has_checks:                       # без чеків — оцінка за продажами товарів і сумами візитів
        for pid, s in db.execute(select(ProductSale.patient_id, func.sum(ProductSale.amount)).group_by(ProductSale.patient_id)):
            rev[pid] += float(s or 0)
        for pid, s in db.execute(select(Visit.patient_id, func.sum(Visit.amount)).where(Visit.status == "done")
                                 .group_by(Visit.patient_id)):
            rev[pid] += float(s or 0)
    smap = status_by_plan(db)                # усі статуси одним пакетом (і запамʼятовано в межах запиту)
    rows = []
    for p in patients:
        plan = p.plans[0] if p.plans else None
        # статистика CleverBox (перенос/синк клієнтів) доповнює локальну історію
        lv = max(filter(None, [last.get(p.id), p.cb_last_visit]), default=None)
        rows.append(ClientRow(p, smap.get(plan.id) if plan else None, lv, nxt.get(p.id),
                              max(cnt.get(p.id, 0), p.cb_visits or 0), max(rev.get(p.id, 0.0), p.cb_total or 0.0)))
    # VIP — топ-10% за виручкою (мінімум 1 клієнт, якщо є виручка)
    with_rev = sorted((r for r in rows if r.revenue > 0), key=lambda r: -r.revenue)
    for r in with_rev[:max(1, len(with_rev) // 10)] if with_rev else []:
        r.vip = True
    return rows


def segment_counts(rows: list[ClientRow]) -> dict[str, int]:
    c = defaultdict(int)
    for r in rows:
        for f in r.segment_flags:
            c[f] += 1
    return c
