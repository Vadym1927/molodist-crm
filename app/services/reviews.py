"""Відгуки й NPS: через N хвилин після закритого візиту пацієнт отримує посилання на коротке опитування.
Усі оцінки «5» → пропонуємо продублювати відгук у Google (текст копіюється, відкривається форма відгуку Google).
Хоч одна оцінка нижча за 5 → рекламація: задача адміністраторам і сповіщення власнику, головному лікарю й адміністраторам.
У Telegram іде лише факт і оцінки — текст відгуку читають у картці пацієнта."""
from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Appointment, Doctor, Patient, Review, SyncState, Task, User

KEY = "reviews"
QUESTIONS = [{"key": "doctor", "title": "Лікар: увага й пояснення"}, {"key": "result", "title": "Процедура й результат"},
             {"key": "admin", "title": "Адміністратори: запис і зустріч"}, {"key": "space", "title": "Атмосфера й чистота"}]
ORDERS = {"tg": ("Telegram-бот → Viber → SMS", ["telegram", "viber", "sms"]), "viber": ("Viber → Telegram-бот → SMS", ["viber", "telegram", "sms"]),
          "sms": ("Лише SMS", ["sms"])}
DEFAULT = {"on": False, "delay_min": 30, "min_days": 7, "google_url": "", "questions": QUESTIONS, "order": "tg",
           "ttl_hours": 24,                 # посилання діє стільки годин після надсилання
           "base_count": 0, "base_avg": 5.0,   # відгуки, зібрані до CRM (CleverBox): кількість і середня оцінка — для рейтингу в шапці
           "text": "{name}, дякуємо, що були в MOLODIST сьогодні 🤍 Будь ласка, оцініть візит — це займе хвилину: {link}"}
DONE = ("done", "paid")


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    out = {**DEFAULT, **d}
    out["questions"] = [q for q in out.get("questions") or [] if q.get("key") and q.get("title")] or QUESTIONS
    return out


def save_cfg(db: Session, d: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def _loc(v: datetime | None) -> datetime | None:
    return v.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None) if v else v


def link(r: Review) -> str:
    return f"{settings.public_url.rstrip('/')}/r/{r.token}"


def due(db: Session, now: datetime | None = None) -> list[Appointment]:
    """Візити, після яких час надіслати опитування: відбулись, закінчились понад delay_min хв тому (але не раніше доби),
    за цей візит запиту ще не було, і пацієнту не писали останні min_days днів."""
    c = cfg(db)
    now = now or datetime.now()
    a, b = now - timedelta(hours=24), now - timedelta(minutes=int(c["delay_min"] or 30))
    out = []
    for ap in db.scalars(select(Appointment).where(Appointment.end >= a, Appointment.end <= b).order_by(Appointment.end)):
        if not (ap.status in DONE or ap.sale_id) or ap.status in ("cancelled", "no_show"):
            continue
        if db.scalar(select(Review.id).where(Review.appointment_id == ap.id)):
            continue
        since = datetime.utcnow() - timedelta(days=int(c["min_days"] or 0))
        if c["min_days"] and db.scalar(select(Review.id).where(Review.patient_id == ap.patient_id, Review.created_at >= since)):
            continue
        out.append(ap)
    return out


def request(db: Session, ap: Appointment, send: bool = True) -> Review:
    """Створити запит на відгук і надіслати пацієнту посилання (Telegram → Viber → SMS)."""
    from ..integrations import messaging
    c = cfg(db)
    r = Review(token=secrets.token_urlsafe(12), patient_id=ap.patient_id, appointment_id=ap.id, doctor_id=ap.doctor_id)
    db.add(r)
    db.flush()
    p = db.get(Patient, ap.patient_id)
    if send and p:
        first = (p.full_name or "").split()[1] if len((p.full_name or "").split()) > 1 else (p.full_name or "").strip()
        text = (c["text"] or DEFAULT["text"]).replace("{name}", first or "Вітаємо").replace("{link}", link(r))
        res = messaging.send_to_patient(p, text, prefer=list(ORDERS.get(c.get("order"), ORDERS["tg"])[1]))
        r.channel, r.send_error = res.channel, (None if res.ok else (res.error or "не надіслано")[:300])
        r.sent_at = datetime.utcnow() if res.ok else None
    db.commit()
    return r


def run(db: Session) -> dict:
    if not cfg(db)["on"]:
        return {"sent": 0, "off": True}
    n = ok = 0
    for ap in due(db):
        r = request(db, ap)
        n += 1
        ok += bool(r.sent_at)
    return {"requests": n, "sent": ok}


def by_token(db: Session, token: str) -> Review | None:
    return db.scalar(select(Review).where(Review.token == token)) if token else None


def expired(db: Session, r: Review, now: datetime | None = None) -> bool:
    """Посилання індивідуальне й діє обмежений час (за замовчуванням 24 години від надсилання)."""
    ttl = float(cfg(db).get("ttl_hours") or 0)
    if not ttl:
        return False
    return (now or datetime.utcnow()) > (r.sent_at or r.created_at) + timedelta(hours=ttl)


def rating(db: Session) -> dict:
    """Рейтинг для шапки опитування: відгуки до CRM (кількість і середня з налаштувань) + відгуки, зібрані в CRM."""
    from sqlalchemy import func
    c = cfg(db)
    n, sm = db.execute(select(func.count(Review.id), func.sum(Review.avg)).where(Review.answered_at.is_not(None))).one()
    bn, ba = int(c.get("base_count") or 0), float(c.get("base_avg") or 5)
    tot = bn + int(n or 0)
    avg = (bn * ba + float(sm or 0)) / tot if tot else None
    return {"count": tot, "avg": round(avg, 1) if avg is not None else None}


def submit(db: Session, r: Review, scores: dict, nps: int | None, text: str) -> Review:
    """Зберегти відповідь (один раз). Не всі «5» → рекламація: задача й сповіщення."""
    if r.answered_at:
        return r
    qs = {q["key"] for q in cfg(db)["questions"]}
    sc = {k: max(1, min(5, int(v))) for k, v in scores.items() if k in qs and str(v).isdigit()}
    if not sc:
        raise ValueError("Поставте, будь ласка, оцінки")
    r.scores, r.avg = sc, round(sum(sc.values()) / len(sc), 2)
    r.nps = max(0, min(10, int(nps))) if nps is not None else None
    r.text = (text or "").strip()[:3000] or None
    r.answered_at = datetime.utcnow()
    r.complaint = any(v < 5 for v in sc.values())
    if r.complaint:
        p = db.get(Patient, r.patient_id)
        t = Task(title=f"Рекламація: відгук {r.avg:g} з 5 — {p.full_name if p else ''}".strip()[:300], kind="complaint", priority="high",
                 patient_id=r.patient_id, assignee_role="admin", due_date=date.today(), source_key=f"review:{r.id}",
                 description="Пацієнт оцінив візит не на всі «5». Відгук — у картці пацієнта, вкладка «Відгуки». "
                             "Звʼязатися, зʼясувати причину, записати результат.")
        db.add(t)
        db.flush()
        r.task_id = t.id
    db.commit()
    if r.complaint:
        notify(db, r)
    return r


def stars(r: Review) -> str:
    return " · ".join(f"{v}" for v in (r.scores or {}).values())


def notify(db: Session, r: Review) -> int:
    """Рекламація → Telegram власнику, головному лікарю й адміністраторам. Без тексту відгуку."""
    from . import tg_notify
    from html import escape
    p = db.get(Patient, r.patient_id)
    d = db.get(Doctor, r.doctor_id) if r.doctor_id else None
    qt = {q["key"]: q["title"] for q in cfg(db)["questions"]}
    lines = [f"⚠️ <b>Рекламація: відгук {r.avg:g} з 5</b>", escape(p.full_name if p else "Пацієнт") + (f" · лікар {escape(d.name)}" if d else "")]
    lines += [f"{escape(qt.get(k, k))}: {v} з 5" for k, v in (r.scores or {}).items() if v < 5]
    if r.nps is not None:
        lines.append(f"Порадить друзям: {r.nps} з 10")
    lines.append(f"{settings.public_url.rstrip('/')}/patients/{r.patient_id}#t-reviews")
    text, chats = "\n".join(lines), set()
    for role in ("owner", "chief", "admin"):
        chats |= {u.telegram_chat_id for u in tg_notify.recipients(db, role)}
    n = 0
    for ch in chats:
        try:
            n += bool(tg_notify.send_text(ch, text))
        except Exception:      # noqa: BLE001
            pass
    return n


def mark_google(db: Session, r: Review) -> None:
    if r.answered_at and not r.complaint and not r.google_at:
        r.google_at = datetime.utcnow()
        db.commit()


def for_patient(db: Session, patient_id: int) -> list[Review]:
    return db.scalars(select(Review).where(Review.patient_id == patient_id, Review.answered_at.is_not(None)).order_by(Review.answered_at.desc())).all()


def bell(db: Session, since: datetime) -> list[dict]:
    """Сповіщення в дзвіночку: нові відгуки."""
    out = []
    for r in db.scalars(select(Review).where(Review.answered_at >= since).order_by(Review.answered_at.desc()).limit(40)):
        p = r.patient
        out.append({"kind": "review", "id": r.id, "at": r.answered_at, "level": "bad" if r.complaint else "ok",
                    "title": ("Рекламація · " if r.complaint else "Відгук · ") + "★" * round(r.avg or 0) + f" {r.avg:g}",
                    "text": f"{p.full_name if p else '—'}" + (f": «{(r.text or '')[:90]}»" if r.text else "") +
                            (" · продубльовано в Google" if r.google_at else ""),
                    "url": f"/patients/{r.patient_id}#t-reviews"})
    return out


def nps_of(rs: list[Review]) -> float | None:
    v = [r.nps for r in rs if r.nps is not None]
    if not v:
        return None
    return round(100 * (sum(1 for x in v if x >= 9) - sum(1 for x in v if x <= 6)) / len(v), 1)


def doctor_stats(db: Session, doctor_id: int, a: datetime, b: datetime) -> dict:
    """Відгуки на візити лікаря за період (за датою візиту): кількість, середня оцінка, NPS, рекламації — для KPI лікаря."""
    rs = [r for r in db.scalars(select(Review).where(Review.doctor_id == doctor_id, Review.answered_at.is_not(None),
                                                    Review.created_at >= a - timedelta(hours=4), Review.created_at <= b + timedelta(hours=4)))
          if a <= (_loc(r.created_at) or a) <= b]
    asked = db.scalars(select(Review.id).where(Review.doctor_id == doctor_id, Review.created_at >= a - timedelta(hours=4),
                                               Review.created_at <= b + timedelta(hours=4))).all()
    return {"review_n": len(rs), "review_asked": len(asked), "review_avg": round(sum(r.avg or 0 for r in rs) / len(rs), 2) if rs else None,
            "review_nps": nps_of(rs), "complaints": sum(1 for r in rs if r.complaint)}
