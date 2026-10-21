"""Нагадування адмінам: «пацієнт не записаний на наступну процедуру» і «догляд закінчується».
Медичні дані з плану в повідомлення НЕ потрапляють — лише ім'я, телефон, процедура/засіб, посилання."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ReminderLog
from .kpi import all_statuses, PlanStatus


@dataclass
class Reminder:
    kind: str            # not_booked | care_ending
    entity_key: str
    period_key: str
    patient_id: int
    text: str


def _card(pid: int) -> str:
    return f"{settings.public_url.rstrip('/')}/patients/{pid}"


def collect(db: Session, now: datetime | None = None) -> list[Reminder]:
    now = now or datetime.now()
    week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    out: list[Reminder] = []
    for ps in all_statuses(db):
        plan, pat = ps.plan, ps.plan.patient
        if plan.status != "active":
            continue
        who = f"<b>{escape(pat.full_name)}</b>" + (f" · {escape(pat.phone)}" if pat.phone else "")
        doc = f" · лікар {escape(plan.doctor.name)}" if plan.doctor else ""

        # 1) не записаний
        nxt = ps.next_procedure
        if nxt and not ps.is_booked and ps.due_date and ps.due_date <= now:
            overdue = (now - ps.due_date).days
            left = sum(i.unbooked for i in ps.items if i.item.kind == "procedure")
            out.append(Reminder(
                "not_booked", f"plan:{plan.id}", week, pat.id,
                f"📅 {who}{doc}\nНе записана. Наступна: <b>{escape(nxt.item.name)}</b> "
                f"(залишилось процедур: {left}, план виконано на {ps.realization_pct}%)"
                + (f"\nПрострочено на {overdue} дн." if overdue > 0 else "")
                + f"\n<a href=\"{_card(pat.id)}\">Картка пацієнта</a>"))

        # 2) догляд закінчується
        for st in ps.items:
            it = st.item
            if it.kind != "product" or not st.last_done or not it.supply_days or st.done < it.qty:
                continue
            ends = st.last_done + timedelta(days=it.supply_days)
            if now >= ends - timedelta(days=settings.remind_care_days_before_end) and now <= ends + timedelta(days=30):
                link = f"\n<a href=\"{escape(it.url)}\">Товар у магазині</a>" if it.url else ""
                out.append(Reminder(
                    "care_ending", f"item:{it.id}", st.last_done.strftime("%Y-%m-%d"), pat.id,
                    f"🧴 {who}\nЗакінчується <b>{escape(it.name)}</b> (≈ до {ends:%d.%m}). "
                    f"Запропонувати повторну покупку.{link}\n<a href=\"{_card(pat.id)}\">Картка пацієнта</a>"))
    return out


def pending(db: Session, now: datetime | None = None) -> list[Reminder]:
    sent = {(r.kind, r.entity_key, r.period_key) for r in db.scalars(select(ReminderLog))}
    return [r for r in collect(db, now) if (r.kind, r.entity_key, r.period_key) not in sent]


def mark_sent(db: Session, rems: list[Reminder]):
    for r in rems:
        db.add(ReminderLog(kind=r.kind, entity_key=r.entity_key, period_key=r.period_key))
    db.commit()


def digest_messages(rems: list[Reminder], limit: int = 3800) -> list[str]:
    """Склеює нагадування у повідомлення ≤ 4096 символів (ліміт Telegram)."""
    groups = [("not_booked", "<b>Не записані на наступну процедуру</b>"),
              ("care_ending", "<b>Закінчується домашній догляд</b>")]
    msgs, cur = [], ""
    for kind, title in groups:
        items = [r.text for r in rems if r.kind == kind]
        if not items:
            continue
        block = f"{title} ({len(items)})\n\n"
        for t in items:
            if len(cur) + len(block) + len(t) > limit:
                msgs.append(cur + block)
                cur, block = "", ""
            block += t + "\n\n"
        cur += block
    if cur:
        msgs.append(cur)
    return msgs
