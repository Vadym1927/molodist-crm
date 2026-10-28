"""Розділ «Спеціалісти»: картки (CleverBox + ручні дані), статистика, колонки календаря."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations.cleverbox import same_person
from ..models import Doctor, Visit, Patient, SyncState
from .kpi import all_statuses, KpiRow

# кольори фахівців у календарі: приглушені, у гамі брендбуку, але добре розрізнювані
PALETTE = ["#5c4b82", "#2f7a8a", "#a05a7c", "#b07a3e", "#4a6fa5", "#5f8a5a", "#8e7cc3", "#7a6a5c"]


def color(d: Doctor | None) -> str:
    if not d:
        return "#9b948b"
    return d.color or PALETTE[(d.id or 0) % len(PALETTE)]


def initials(name: str) -> str:
    parts = [p for p in (name or "").replace("#", " ").split() if p[:1].isalpha()]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


class DoctorIndex:
    """Швидке зіставлення імені майстра з візиту → картка спеціаліста (з кешем)."""

    def __init__(self, doctors: list[Doctor]):
        self.doctors = doctors
        self.by_id = {d.id: d for d in doctors}
        self._cache: dict[str, Doctor | None] = {}

    def find(self, name: str | None) -> Doctor | None:
        if not name:
            return None
        if name not in self._cache:
            exact = [d for d in self.doctors if same_person(d.name, name)]
            exact.sort(key=lambda d: (not d.active, d.id))          # тезки: спершу чинний
            self._cache[name] = exact[0] if exact else None
        return self._cache[name]


UA = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"


def ua_key(name: str) -> list[int]:
    """Сортування за українською абеткою (І після И, а не перед Д)."""
    return [UA.index(ch) if ch in UA else 100 + ord(ch) for ch in (name or "").lower()]


def all_doctors(db: Session) -> list[Doctor]:
    return sorted(db.scalars(select(Doctor)).all(), key=lambda d: (not d.active, ua_key(d.name)))


def calendar_doctors(db: Session) -> list[Doctor]:
    """Хто за замовчуванням має колонку в календарі: працює і не прихований."""
    return [d for d in all_doctors(db) if d.active and d.in_calendar is not False]


@dataclass
class Card:
    doctor: Doctor
    color: str
    initials: str
    month_visits: int = 0
    month_revenue: float = 0.0
    prev_month_visits: int = 0
    upcoming: int = 0
    next_visit: datetime | None = None
    last_visit: datetime | None = None
    patients: int = 0
    kpi: KpiRow | None = None
    top_services: list[tuple[str, int]] = field(default_factory=list)


def cards(db: Session, doctor_ids: list[int] | None = None) -> list[Card]:
    doctors = all_doctors(db)
    if doctor_ids is not None:
        doctors = [d for d in doctors if d.id in doctor_ids]
    idx = DoctorIndex(all_doctors(db))
    now = datetime.now()
    m0 = datetime(now.year, now.month, 1)
    pm0 = (m0 - timedelta(days=1)).replace(day=1)
    horizon = now + timedelta(days=14)
    out = {d.id: Card(d, color(d), initials(d.name)) for d in doctors}
    services: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    rows = db.execute(select(Visit.master_name, Visit.starts_at, Visit.status, Visit.amount, Visit.service_name,
                             Visit.source, Visit.patient_id)
                      .where(Visit.master_name.is_not(None), Visit.starts_at >= pm0 - timedelta(days=150),
                             Visit.starts_at <= horizon)).all()
    for master, at, status, amount, svc, source, _pid in rows:
        d = idx.find(master)
        if not d or d.id not in out or not at:
            continue
        c = out[d.id]
        if status in ("done", "attended") and at <= now:
            c.last_visit = max(filter(None, [c.last_visit, at]))
            if source == "cb_service_sale":            # надані послуги — один рядок = одна послуга
                if at >= m0:
                    c.month_visits += 1
                    c.month_revenue += float(amount or 0)
                    if svc:
                        services[d.id][svc] += 1
                elif at >= pm0:
                    c.prev_month_visits += 1
        elif status == "booked" and now < at <= horizon:
            c.upcoming += 1
            c.next_visit = min(filter(None, [c.next_visit, at]))

    for did, n in db.execute(select(Patient.doctor_id, Patient.id).where(Patient.doctor_id.is_not(None))):
        if did in out:
            out[did].patients += 1
    for ps in all_statuses(db):
        did = ps.plan.doctor_id
        if did in out:
            out[did].kpi = out[did].kpi or KpiRow(out[did].doctor.name, did)
            out[did].kpi.add(ps)
    for did, svc in services.items():
        out[did].top_services = sorted(svc.items(), key=lambda x: -x[1])[:5]
    return list(out.values())


def cb_staff_fields(db: Session) -> str | None:
    st = db.get(SyncState, "cleverbox_staff_fields")
    return st.value if st else None
