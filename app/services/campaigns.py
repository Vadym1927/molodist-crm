"""Розсилки за сегментами клієнтів + поштучні повідомлення з задач. Атрибуція: хто після повідомлення записався,
прийшов і скільки приніс.

Правила (захист бази і репутації клініки):
  • відписані (marketing_opt_out) не отримують маркетинг ніколи;
  • одному пацієнту — не частіше ніж раз на MARKETING_FREQUENCY_DAYS (будь-яка розсилка);
  • тихі години: з QUIET_HOURS_START до QUIET_HOURS_END відправка не стартує (переноситься на ранок);
  • перед запуском — превʼю: скільки отримають, ким, скільки відсіяно і чому, орієнтовна вартість, тест собі.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..config import settings
from ..integrations import messaging
from ..models import (Campaign, CampaignRecipient, MessageLog, Patient, Visit, Check, SessionLocal)
from . import clients as clients_svc
from .scripts import first_name, render

log = logging.getLogger("campaigns")

CAMPAIGN_PLACEHOLDERS = {"імʼя": "Імʼя клієнта", "лікар": "Лікар клієнта", "клініка": "MOLODIST"}
SEGMENT_IDEAS = {
    "sleeping": "{імʼя}, ми скучили! 🌿 Давно не бачились у MOLODIST — запрошуємо на безкоштовну діагностику шкіри "
                "на апараті OBSERV і оновлений план догляду. Записатися: відповідайте на це повідомлення.",
    "no_plan": "{імʼя}, добрий день! Запрошуємо на консультацію з діагностикою шкіри OBSERV — лікар складе персональний "
               "бюті-план з чіткими кроками і бюджетом. Підібрати час?",
    "birthday": "{імʼя}, вітаємо з наближенням Дня народження! 🎉 Для вас у MOLODIST подарунок — "
                "знижка на процедуру протягом місяця. Підберемо час?",
    "attention": "{імʼя}, добрий день! Нагадуємо про ваш бюті-план у MOLODIST — наступна процедура вже на часі. "
                 "Підказати вільні вікна?",
}


def rendered_text(c: Campaign, patient: Patient) -> str:
    return render(c.text, {"імʼя": first_name(patient.full_name),
                           "лікар": patient.doctor.name if patient.doctor else "ваш лікар", "клініка": "MOLODIST"})


@dataclass
class Audience:
    rows: list[tuple[Patient, str]] = field(default_factory=list)    # (пацієнт, канал)
    skipped: dict[str, int] = field(default_factory=dict)
    by_channel: dict[str, int] = field(default_factory=dict)
    cost: float = 0.0

    def skip(self, reason: str):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def recent_marketing(db: Session, days: int) -> set[int]:
    since = datetime.utcnow() - timedelta(days=days)
    return {pid for (pid,) in db.execute(select(MessageLog.patient_id).where(
        MessageLog.marketing.is_(True), MessageLog.status != "failed", MessageLog.at >= since).distinct())}


def build_audience(db: Session, c: Campaign) -> Audience:
    from sqlalchemy.orm import selectinload
    f = c.filters or {}
    pats = db.scalars(select(Patient).options(selectinload(Patient.plans), selectinload(Patient.doctor))).all()
    rows = clients_svc.client_rows(db, pats)
    recent = recent_marketing(db, settings.marketing_frequency_days)
    already = {r.patient_id for r in c.recipients} if c.id else set()
    aud = Audience()
    for r in rows:
        p = r.patient
        if c.segment not in r.segment_flags:
            continue
        if f.get("doctor_id") and p.doctor_id != int(f["doctor_id"]) and not any(
                pl.doctor_id == int(f["doctor_id"]) for pl in p.plans):
            continue
        if f.get("source") and (p.source or "").lower() != str(f["source"]).lower():
            continue
        if f.get("min_ltv") and r.revenue < float(f["min_ltv"]):
            continue
        if f.get("max_ltv") and r.revenue > float(f["max_ltv"]):
            continue
        if p.id in already:
            aud.skip("вже в цій розсилці"); continue
        if p.marketing_opt_out:
            aud.skip("відписані"); continue
        if p.id in recent:
            aud.skip(f"отримували розсилку < {settings.marketing_frequency_days} дн."); continue
        avail = messaging.available_channels(p)
        ch = next((x for x in (c.channels or []) if x in avail), None)
        if not ch:
            aud.skip("немає Telegram/телефону для обраних каналів"); continue
        aud.rows.append((p, ch))
        aud.by_channel[ch] = aud.by_channel.get(ch, 0) + 1
        if ch == "sms":
            aud.cost += messaging.sms_parts(rendered_text(c, p) + "\n" + settings.marketing_footer) * settings.sms_price
        elif ch == "viber":
            aud.cost += settings.viber_price
    aud.cost = round(aud.cost, 2)
    return aud


def log_message(db: Session, patient: Patient, channel: str, text: str, res, user_id=None, task_id=None,
                campaign_id=None, marketing=False):
    db.add(MessageLog(patient_id=patient.id, user_id=user_id, task_id=task_id, campaign_id=campaign_id,
                      channel=res.channel or channel, text=text, status="sent" if res.ok else "failed",
                      message_id=res.message_id, error=res.error, marketing=marketing))
    if res.blocked:                       # пацієнт заблокував бота — відвʼязуємо Telegram
        patient.telegram_chat_id = None


def send_single(db: Session, patient: Patient, text: str, channel: str, user, task_id: int | None = None):
    """Повідомлення з задачі/картки (сервісне, не маркетинг: без футера і без перевірки частоти)."""
    prefer = ["telegram", "viber", "sms"] if channel == "auto" else (["viber", "sms"] if channel == "viber" else [channel])
    res = messaging.send_to_patient(patient, text, prefer=prefer, marketing=False)
    log_message(db, patient, channel, text, res, user_id=user.id, task_id=task_id)
    db.commit()
    return res


def start(db: Session, c: Campaign) -> int:
    """Зафіксувати аудиторію (знімок на момент запуску) і поставити в чергу."""
    aud = build_audience(db, c)
    for p, ch in aud.rows:
        db.add(CampaignRecipient(campaign_id=c.id, patient_id=p.id, channel=ch,
                                 address=p.telegram_chat_id if ch == "telegram" else messaging.normalize_phone(p.phone),
                                 text=rendered_text(c, p), status="queued"))
    c.status = "sending"
    c.sent_at = c.sent_at or datetime.now()
    db.commit()
    return len(aud.rows)


def deliver(job=None, campaign_id: int | None = None) -> dict:
    """Фонова відправка черги (з дотриманням тихих годин)."""
    rep = {"sent": 0, "failed": 0}
    with SessionLocal() as db:
        c = db.get(Campaign, campaign_id)
        if not c or c.status != "sending":
            return rep
        queue = db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == c.id,
                                                           CampaignRecipient.status == "queued")).all()
        for i, r in enumerate(queue, 1):
            if messaging.is_quiet_time():
                if job:
                    job.log("Тихі години — продовжимо зранку")
                c.status = "scheduled"
                c.scheduled_at = None
                db.commit()
                return rep
            p = r.patient
            prefer = [r.channel] if r.channel == "telegram" else [x for x in ("viber", "sms") if x in (c.channels or [])]
            res = messaging.send_to_patient(p, r.text, prefer=prefer, marketing=True,
                                            sequence_id=f"crm-c{c.id}-r{r.id}")
            r.status = "sent" if res.ok else "failed"
            r.message_id, r.error, r.sent_at, r.channel = res.message_id, res.error, datetime.now(), res.channel
            log_message(db, p, r.channel, r.text, res, campaign_id=c.id, marketing=True)
            rep["sent" if res.ok else "failed"] += 1
            if i % 25 == 0:
                db.commit()
                if job:
                    job.log(f"Надіслано {rep['sent']}, помилок {rep['failed']} з {len(queue)}")
        c.status, c.finished_at = "done", datetime.now()
        db.commit()
    return rep


def refresh_statuses(db: Session) -> int:
    """Статуси доставки Viber/SMS з TurboSMS за останні 3 дні."""
    if not messaging.turbosms_available():
        return 0
    since = datetime.now() - timedelta(days=3)
    rows = db.scalars(select(CampaignRecipient).where(CampaignRecipient.channel.in_(("viber", "sms")),
                                                      CampaignRecipient.message_id.is_not(None),
                                                      CampaignRecipient.status.in_(("sent", "delivered")),
                                                      CampaignRecipient.sent_at >= since)).all()
    st = messaging.turbosms_status([r.message_id for r in rows])
    n = 0
    for r in rows:
        x = st.get(r.message_id)
        if not x:
            continue
        new = messaging.STATUS_MAP.get(str(x.get("status", "")).lower())
        if new and new != r.status:
            r.status, r.status_at, n = new, datetime.now(), n + 1
            if x.get("channel"):
                r.channel = x["channel"]
    db.commit()
    return n


@dataclass
class Stats:
    total: int = 0
    sent: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    booked: int = 0          # записалися/прийшли протягом вікна атрибуції
    visited: int = 0
    revenue: float = 0.0
    opted_out: int = 0

    @property
    def conversion(self):
        return round(100 * self.booked / self.sent, 1) if self.sent else 0.0


def stats(db: Session, c: Campaign) -> Stats:
    s = Stats()
    window = timedelta(days=c.attribution_days or 14)
    utc_shift = timedelta(hours=round((datetime.now() - datetime.utcnow()).total_seconds() / 3600))  # synced_at — UTC
    for r in c.recipients:
        s.total += 1
        if r.status in ("sent", "delivered", "read"):
            s.sent += 1
            s.delivered += r.status in ("delivered", "read")
        elif r.status == "failed":
            s.failed += 1
        elif r.status == "skipped":
            s.skipped += 1
        if not r.sent_at or r.status in ("failed", "skipped", "queued"):
            continue
        end = r.sent_at + window
        # новий запис, що зʼявився після повідомлення, або візит/надана послуга у вікні
        v = db.scalar(select(Visit).where(Visit.patient_id == r.patient_id, (
            ((Visit.source == "cb_visit") & (Visit.synced_at >= r.sent_at - utc_shift) & (Visit.synced_at <= end - utc_shift)) |
            ((Visit.status == "done") & (Visit.starts_at >= r.sent_at) & (Visit.starts_at <= end)))).limit(1))
        if v:
            s.booked += 1
            s.visited += v.status == "done"
        rev = db.scalar(select(func.sum(Check.amount)).where(Check.patient_id == r.patient_id, Check.deleted.is_(False),
                                                             Check.date >= r.sent_at, Check.date <= end))
        s.revenue += float(rev or 0)
        s.opted_out += bool(r.patient.marketing_opt_out and r.patient.opt_out_at and r.patient.opt_out_at >= r.sent_at)
    return s


def run_due(db: Session) -> list[int]:
    """Планувальник: запустити розсилки, час яких настав (і не тихі години)."""
    if messaging.is_quiet_time():
        return []
    now = datetime.now()                     # scheduled_at — місцевий час (як вводить користувач)
    ids = []
    for c in db.scalars(select(Campaign).where(Campaign.status == "scheduled")):
        if c.scheduled_at is None or c.scheduled_at <= now:
            if not c.recipients:
                start(db, c)
            else:
                c.status = "sending"
                db.commit()
            ids.append(c.id)
    return ids
