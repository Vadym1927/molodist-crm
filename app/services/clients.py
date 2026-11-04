"""Розділ «Пацієнти»: сегменти під бюті-плани."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import Patient, Visit, ProductSale, Check, Appointment, SyncState
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
        if self.is_new(now):
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
    first_visit: datetime | None = None

    def is_new(self, now: datetime) -> bool:
        """Нова = перший візит за останні 30 днів; без візитів — записана на перший візит або додана за 30 днів
        (дата з CleverBox, а для карток, створених у CRM вручну, — дата створення).
        Дата переносу з CleverBox (created_at) для клієнтів CleverBox не враховується."""
        since = now - timedelta(days=30)
        if self.first_visit or self.visits:
            return bool(self.first_visit and self.first_visit >= since and self.first_visit <= now)
        if self.next_visit:                  # ще не була, але вже записана на перший візит
            return True
        p = self.patient
        reg = p.cb_registered_at or (p.created_at if p.cleverbox_id is None else None)
        return bool(reg and reg >= since)


def client_rows(db: Session, patients: list[Patient]) -> list[ClientRow]:
    now = datetime.now()
    ids = [p.id for p in patients]
    last, nxt, cnt, rev, first = {}, {}, defaultdict(int), defaultdict(float), {}
    for pid, mx, n, mn in db.execute(select(Visit.patient_id, func.max(Visit.starts_at), func.count(),
                                             func.min(Visit.starts_at))
                                 .where(Visit.status.in_(("done", "attended")), Visit.starts_at <= now)
                                 .group_by(Visit.patient_id)):
        last[pid], cnt[pid], first[pid] = mx, n, mn
    for pid, mn in db.execute(select(Visit.patient_id, func.min(Visit.starts_at))
                              .where(Visit.status == "booked", Visit.starts_at > now).group_by(Visit.patient_id)):
        nxt[pid] = mn
    # записи CRM (календар) — найближчий майбутній і останній завершений
    for pid, mn in db.execute(select(Appointment.patient_id, func.min(Appointment.start))
                              .where(Appointment.status.in_(("booked", "confirmed", "arrived")), Appointment.start > now)
                              .group_by(Appointment.patient_id)):
        if mn and (pid not in nxt or mn < nxt[pid]):
            nxt[pid] = mn
    for pid, mx in db.execute(select(Appointment.patient_id, func.max(Appointment.start))
                              .where(Appointment.status == "done", Appointment.start <= now).group_by(Appointment.patient_id)):
        if mx and (pid not in last or mx > last[pid]):
            last[pid] = mx
    has_checks = False
    for pid, s, mx, mn in db.execute(select(Check.patient_id, func.sum(Check.amount), func.max(Check.date),
                                             func.min(Check.date))
                                 .where(Check.deleted.is_(False), Check.patient_id.is_not(None))
                                 .group_by(Check.patient_id)):
        has_checks = True
        rev[pid] = float(s or 0)
        if mx and (pid not in last or mx > last[pid]):
            last[pid] = mx
        if mn and (pid not in first or mn < first[pid]):
            first[pid] = mn
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
        row = ClientRow(p, smap.get(plan.id) if plan else None, lv, nxt.get(p.id),
                        max(cnt.get(p.id, 0), p.cb_visits or 0), max(rev.get(p.id, 0.0), p.cb_total or 0.0))
        row.first_visit = first.get(p.id)
        rows.append(row)
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


# ---------------- колонки списку й фільтри ----------------
COLUMNS = {"phone": "Телефон", "tag": "Статус пацієнта", "groups": "Групи", "plan": "Бюті-план", "real": "Реалізація плану",
           "visits": "Візитів", "last": "Останній візит", "next": "Наступний запис", "revenue": "Покупки (LTV)",
           "status": "Статус плану", "bonus": "Бонуси", "balance": "Баланс", "birthday": "День народження", "age": "Вік",
           "source": "Джерело", "doctor": "Лікар", "registered": "Дата додавання", "channel": "Канал звʼязку", "mark": "VIP / BAD"}
DEFAULT_COLS = ["phone", "tag", "plan", "real", "visits", "last", "next", "revenue", "status"]
COLS_KEY = "clients_columns"


def columns(db: Session) -> list[str]:
    import json
    st = db.get(SyncState, COLS_KEY)
    try:
        v = json.loads(st.value) if st and st.value else None
    except ValueError:
        v = None
    return [c for c in (v or DEFAULT_COLS) if c in COLUMNS]


def save_columns(db: Session, cols: list[str]) -> None:
    import json
    st = db.get(SyncState, COLS_KEY) or SyncState(key=COLS_KEY)
    st.value, st.updated_at = json.dumps([c for c in cols if c in COLUMNS]), datetime.utcnow()
    db.merge(st)
    db.commit()


def age(p: Patient) -> int | None:
    b = p.birthday
    if not b:
        return None
    t = datetime.now().date()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


def _num(v):
    try:
        return float(str(v).replace(",", ".").replace(" ", "")) if str(v or "").strip() else None
    except ValueError:
        return None


def _day(v):
    try:
        return datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


FILTER_KEYS = ("tag", "grp", "mark", "gender", "src", "dirs", "ch", "bmonth", "age_from", "age_to", "vis_from", "vis_to",
               "lv_from", "lv_to", "nv", "ltv_from", "ltv_to", "plan", "bonus", "bal", "tg", "optout", "reg_from", "reg_to", "noph")


def apply_filters(rows: list[ClientRow], f, tags_of: dict) -> list[ClientRow]:
    """f — query params (getlist для мульти). tags_of: pid → статус пацієнта."""
    from .patient_tags import group_name
    gl = lambda k: [x for x in f.getlist(k) if x]            # noqa: E731
    tags, grps, srcs, dirs = set(gl("tag")), {x.lower() for x in gl("grp")}, set(gl("src")), set(gl("dirs"))
    mark, gender, ch, nv, plan = f.get("mark", ""), f.get("gender", ""), f.get("ch", ""), f.get("nv", ""), f.get("plan", "")
    bm = int(f.get("bmonth")) if str(f.get("bmonth") or "").isdigit() else None
    a0, a1, v0, v1 = _num(f.get("age_from")), _num(f.get("age_to")), _num(f.get("vis_from")), _num(f.get("vis_to"))
    l0, l1, r0, r1 = _num(f.get("ltv_from")), _num(f.get("ltv_to")), _day(f.get("lv_from")), _day(f.get("lv_to"))
    g0, g1 = _day(f.get("reg_from")), _day(f.get("reg_to"))
    out = []
    for r in rows:
        p = r.patient
        if tags and tags_of.get(p.id) not in tags:
            continue
        if grps and not grps & {group_name(g).lower() for g in (p.client_groups or [])}:
            continue
        if mark and (p.cb_mark or "") != (mark if mark != "none" else ""):
            continue
        if gender and (p.gender or "") != gender:
            continue
        if srcs and (p.source or "") not in srcs:
            continue
        if dirs and not dirs & set(p.directions or []):
            continue
        if ch and (p.communication_channel or "") != ch:
            continue
        if bm and not (p.birthday and p.birthday.month == bm):
            continue
        ag = age(p)
        if (a0 is not None and (ag is None or ag < a0)) or (a1 is not None and (ag is None or ag > a1)):
            continue
        if (v0 is not None and r.visits < v0) or (v1 is not None and r.visits > v1):
            continue
        if (l0 is not None and r.revenue < l0) or (l1 is not None and r.revenue > l1):
            continue
        if r0 and not (r.last_visit and r.last_visit >= r0) or r1 and not (r.last_visit and r.last_visit < r1 + timedelta(days=1)):
            continue
        if nv == "yes" and not r.next_visit or nv == "no" and r.next_visit:
            continue
        if plan == "yes" and not r.plan_status or plan == "no" and r.plan_status:
            continue
        if f.get("bonus") and not (p.cb_bonus or 0) > 0:
            continue
        if f.get("bal") and not (p.cb_balance or 0) > 0:
            continue
        if f.get("tg") and not p.telegram_chat_id:
            continue
        if f.get("optout") == "1" and not p.marketing_opt_out or f.get("optout") == "0" and p.marketing_opt_out:
            continue
        if f.get("noph") and p.phone:
            continue
        reg = p.cb_registered_at or p.created_at
        if g0 and not (reg and reg >= g0) or g1 and not (reg and reg < g1 + timedelta(days=1)):
            continue
        out.append(r)
    return out
