"""Бот турботи (@molodist_assistant_bot, OpenClaw на VPS) ↔ CRM.

Бот сам отримує оновлення Telegram (polling/webhook) — CRM лише:
• віддає дані для «кабінету» в боті (API v1, scope bot): записи, баланс, абонементи й сертифікати, знижки, бюті-план, аналізи;
• приймає від бота: аналізи (у картку, категорія «Аналізи»), повідомлення адміну / головному лікарю, замовлення сертифіката,
  відповіді на нагадування (підтвердити / перенести / скасувати);
• шле нагадування про візит токеном цього ж бота з кнопками (callback_data «mcrm:<дія>:<id запису>» — обробляє бот і кличе API).

Усе вимкнено, доки власник не ввімкне в «Налаштування → Бот турботи». Повідомлення пацієнтам додатково блокує загальна пауза
(«Налаштування → Канали»)."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, Doctor, GiftCertificate, Patient, PatientFile, Subscription, SyncState, Task

KEY = "care_bot"
BOT_URL = "https://t.me/molodist_assistant_bot"
FEATURES = {
    "cabinet": "Кабінет у боті: мої записи, баланс, абонементи й сертифікати, знижки, бюті-план",
    "files": "Аналізи: пацієнт надсилає й переглядає свої аналізи (потрапляють у картку)",
    "messages": "«Написати адміністратору» / «Написати головному лікарю» — задача й сповіщення в CRM",
    "certs": "Купити подарунковий сертифікат або абонемент у боті (посилання на оплату)",
    "reminders": "Нагадування про візит напередодні з кнопками «Підтвердити / Перенести / Скасувати»",
    "login": "Код для входу в кабінет на сайті — у бот, потім SMS (готується разом із кабінетом)",
}
DEFAULT = {"features": {k: False for k in FEATURES}, "remind_hour": 10, "remind_days": 1, "test_only": True, "testers": "",
           "remind_text": "Нагадуємо про ваш візит у MOLODIST: {when}\n{service}{doctor}\n\nАдреса: вул. Князя Романа Мстиславича, 28А (ЖК «Автограф»), "
                          "домофон 15#.\nБудь ласка, підтвердьте візит кнопкою нижче."}
ACTIONS = {"confirm": "Підтвердити", "reschedule": "Перенести", "cancel": "Скасувати"}
CB_PREFIX = "mcrm"
WD = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]
MON = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]


class BotError(ValueError):
    pass


# ---------------- налаштування ----------------
def cfg(db: Session) -> dict:
    c = json.loads(json.dumps(DEFAULT))
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            saved = json.loads(st.value)
            c.update({k: v for k, v in saved.items() if k != "features"})
            c["features"].update({k: bool(v) for k, v in (saved.get("features") or {}).items() if k in FEATURES})
        except ValueError:
            pass
    return c


def save_cfg(db: Session, c: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(c, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def on(db: Session, feature: str) -> bool:
    return bool(cfg(db)["features"].get(feature))


def save_form(db: Session, f) -> dict:
    c = cfg(db)
    c["features"] = {k: bool(f.get(f"f_{k}")) for k in FEATURES}
    try:
        c["remind_hour"] = max(8, min(20, int(f.get("remind_hour") or 10)))
    except ValueError:
        pass
    c["remind_text"] = str(f.get("remind_text") or DEFAULT["remind_text"]).strip()[:1000]
    c["test_only"] = bool(f.get("test_only"))
    c["testers"] = str(f.get("testers") or "").strip()[:500]
    save_cfg(db, c)
    return c


def stats(db: Session) -> dict:
    from ..models import AuditLog
    linked = db.scalars(select(Patient).where(Patient.telegram_chat_id.is_not(None))).all()
    since = datetime.utcnow() - timedelta(days=30)
    acts = db.scalars(select(AuditLog).where(AuditLog.action.like("bot.%"), AuditLog.at >= since).order_by(AuditLog.at.desc()).limit(30)).all()
    return {"linked": len(linked), "opted_out": sum(1 for p in linked if p.marketing_opt_out), "recent": acts}


# ---------------- пацієнт за chat_id ----------------
def patient(db: Session, chat_id: str) -> Patient:
    p = db.scalar(select(Patient).where(Patient.telegram_chat_id == str(chat_id)))
    if not p:
        raise BotError("Пацієнта не привʼязано — спершу /bot/link")
    return p


def _when(dt: datetime) -> str:
    return f"{WD[dt.weekday()]}, {dt.day} {MON[dt.month - 1]} о {dt:%H:%M}"


def _svc(a: Appointment) -> str:
    return ", ".join(str(s.get("name") or "") for s in a.services or [] if s.get("name")) or "Візит"


def _doc_name(db: Session, did: int | None) -> str:
    d = db.get(Doctor, did) if did else None
    if not d:
        return ""
    parts = (d.name or "").split()
    return " ".join(parts[:2][::-1]) if len(parts) >= 2 else d.name


# ---------------- кабінет ----------------
def records(db: Session, p: Patient, now: datetime | None = None) -> dict:
    """Мої записи: майбутні (з можливістю підтвердити / перенести / скасувати) і останні візити."""
    from . import appointments as aps
    now = now or datetime.now()
    rows = db.scalars(select(Appointment).where(Appointment.patient_id == p.id).order_by(Appointment.start.desc()).limit(60)).all()
    up = [a for a in rows if a.start >= now and a.status in ("booked", "confirmed")]
    past = [a for a in rows if a.start < now and a.status in ("arrived", "paid", "done")][:10]
    j = lambda a: {"id": a.id, "start": a.start.isoformat(timespec="minutes"), "when": _when(a.start), "service": _svc(a),  # noqa: E731
                   "doctor": _doc_name(db, a.doctor_id), "status": a.status, "status_text": aps.STATUSES.get(a.status, a.status),
                   "actions": list(ACTIONS) if a in up else []}
    return {"upcoming": [j(a) for a in sorted(up, key=lambda a: a.start)], "past": [j(a) for a in past]}


def wallet(db: Session, p: Patient, today: date | None = None) -> dict:
    """Баланс (реальні гроші на рахунку, не бонуси), абонементи, сертифікати, знижки й бонуси."""
    from . import loyalty as ly
    today = today or date.today()
    subs = [s for s in db.scalars(select(Subscription).where(Subscription.patient_id == p.id)) if s.state(today) in ("active", "expiring")]
    certs = [c for c in db.scalars(select(GiftCertificate).where(GiftCertificate.patient_id == p.id))
             if (c.balance or 0) > 0.005 and (not c.valid_to or c.valid_to >= today)]
    return {
        "balance": round(float(p.cb_balance or 0), 2),
        "subscriptions": [{"name": s.type_name, "visits_left": s.visits_left, "visits_total": s.visits_total,
                           "money_left": None if s.visits_total is not None else round(float(s.balance or 0), 2),
                           "valid_to": s.valid_to.isoformat() if s.valid_to else None, "services": s.services} for s in subs],
        "certificates": [{"code": c.code, "name": c.type_name, "balance": round(float(c.balance or 0), 2),
                          "valid_to": c.valid_to.isoformat() if c.valid_to else None} for c in certs],
        "discount_pct": float(p.discount_pct or 0) or None,
        "bonuses": round(ly.balance(db, p.id), 2),
    }


def files(db: Session, p: Patient, category: str | None = None) -> dict:
    """«Мої документи» в боті: файли картки за категоріями CRM (Обличчя, Тіло, Згоди, Документи, Аналізи)."""
    from . import patient_photos as pp
    rows = [f for f in pp.all_files(db, p.id) if not category or pp.cat(f) == category]
    allf = pp.all_files(db, p.id)
    return {"categories": [{"key": k, "title": t, "count": sum(1 for f in allf if pp.cat(f) == k)} for k, t in pp.CATS.items()],
            "files": [{"id": f.id, "category": pp.cat(f), "name": (f.caption or Path(f.path).name)[:200],
                       "date": (f.taken_at or f.created_at.date()).isoformat(), "type": Path(f.path).suffix.lower().lstrip(".")} for f in rows]}


def file_of(db: Session, p: Patient, fid: int) -> PatientFile:
    f = db.get(PatientFile, fid)
    if not f or f.patient_id != p.id or f.kind == "plan_pdf":
        raise BotError("Файл не знайдено")
    return f


def add_file(db: Session, p: Patient, data: bytes, filename: str, caption: str | None = None, category: str = "lab") -> dict:
    """Файл від пацієнта → картка (за замовчуванням «Аналізи»). Той самий файл удруге не додається."""
    from . import patient_photos as pp
    category = category if category in pp.CATS else "lab"
    from . import ingest
    if not data:
        raise BotError("Порожній файл")
    if len(data) > 20 * 1024 * 1024:
        raise BotError("Файл більший за 20 МБ")
    h = hashlib.sha256(data).hexdigest()
    for f in db.scalars(select(PatientFile).where(PatientFile.patient_id == p.id)):
        try:
            if Path(f.path).exists() and Path(f.path).stat().st_size == len(data) and hashlib.sha256(Path(f.path).read_bytes()).hexdigest() == h:
                return {"ok": True, "duplicate": True, "file_id": f.id}
        except OSError:
            continue
    path = ingest.store(data, filename or "analysis")
    is_img = Path(filename or "").suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".heic")
    f = PatientFile(patient_id=p.id, kind="photo" if is_img else "doc", path=path, category=category, tg_chat=p.telegram_chat_id,
                    caption=((caption or "").strip() or f"{pp.CATS[category]} від пацієнта (бот) · {Path(filename or '').name}")[:500],
                    taken_at=date.today())
    db.add(f)
    db.flush()
    t = Task(title=f"{'Нові аналізи' if category == 'lab' else 'Новий файл'} від пацієнта в боті — {p.full_name}"[:300], kind="bot", priority="normal", patient_id=p.id,
             assignee_role="admin", due_date=date.today(), source_key=f"botfile:{f.id}",
             description=f"Пацієнт надіслав файл через Бот турботи. Файл — у картці, вкладка «Фото й документи» → «{pp.CATS[category]}». "
                         "Передати лікарю.")
    db.add(t)
    db.commit()
    return {"ok": True, "duplicate": False, "file_id": f.id}


def message(db: Session, p: Patient, to: str, text: str, kind: str = "message") -> dict:
    """«Написати адміністратору / головному лікарю» → задача в CRM + Telegram адресатам (без медичних подробиць у сповіщенні)."""
    from . import tg_notify
    from html import escape
    text = (text or "").strip()
    if len(text) < 2:
        raise BotError("Порожнє повідомлення")
    role = "chief" if to in ("chief", "head_doctor", "doctor") else "admin"
    who = "головному лікарю" if role == "chief" else "адміністратору"
    chiefs = tg_notify.recipients(db, "chief") if role == "chief" else []
    what = "Відгук" if kind == "review" else "Повідомлення"
    t = Task(title=f"{what} {who} з бота — {p.full_name}"[:300], kind="bot", priority="high", patient_id=p.id,
             assignee_id=chiefs[0].id if chiefs else None, assignee_role=None if chiefs else ("owner" if role == "chief" else "admin"),
             due_date=date.today(), description=text[:4000])
    db.add(t)
    db.commit()
    from ..config import settings
    note = (f"💬 <b>{'Відгук' if kind == 'review' else 'Пацієнт написав'} {who} в Бот турботи</b>\n{escape(p.full_name)}\n"
            f"{settings.public_url.rstrip('/')}/patients/{p.id}")
    n = 0
    for u in tg_notify.recipients(db, role):
        try:
            n += bool(tg_notify.send_text(u.telegram_chat_id, note))
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "task_id": t.id, "notified": n}


def order(db: Session, p: Patient, data: dict, base_url: str) -> dict:
    """Сертифікат / абонемент з бота: той самий процес, що на сайті (звернення + посилання на оплату; після оплати — продаж сам)."""
    from . import online_booking as ob
    d = {"kind": data.get("kind"), "id": data.get("id"), "name": p.full_name, "phone": p.phone, "to": data.get("to"),
         "to_phone": data.get("to_phone"), "note": data.get("note"), "utm_source": "telegram", "utm_medium": "care_bot"}
    try:
        r = ob.order(db, d, base_url)
    except ob.BookingError as e:
        raise BotError(str(e)) from e
    return {"ok": r["ok"], "title": r["title"], "price": r["price"], "pay_url": r.get("pay_url")}


# ---------------- нагадування з кнопками ----------------
def keyboard(aid: int) -> dict:
    return {"inline_keyboard": [[{"text": "✅ " + ACTIONS["confirm"], "callback_data": f"{CB_PREFIX}:confirm:{aid}"}],
                                [{"text": "🔁 " + ACTIONS["reschedule"], "callback_data": f"{CB_PREFIX}:reschedule:{aid}"},
                                 {"text": "❌ " + ACTIONS["cancel"], "callback_data": f"{CB_PREFIX}:cancel:{aid}"}]]}


def remind_text(db: Session, a: Appointment, c: dict | None = None) -> str:
    c = c or cfg(db)
    doc = _doc_name(db, a.doctor_id)
    return (c.get("remind_text") or DEFAULT["remind_text"]).replace("{when}", _when(a.start)).replace("{service}", _svc(a)) \
        .replace("{doctor}", f"\nЛікар: {doc}" if doc else "")


def tester_ids(db: Session, c: dict | None = None) -> set[int]:
    """Тестовий режим: пацієнти (за телефонами з налаштувань), яким бот показує нові функції. Решта бачить бот як раніше."""
    import re
    from . import appeals as ap
    c = c or cfg(db)
    out = set()
    for ph in re.split(r"[\s,;]+", c.get("testers") or ""):
        if len(re.sub(r"\D", "", ph)) >= 9:
            p = ap.find_patient(db, ph)
            if p:
                out.add(p.id)
    return out


def allowed(db: Session, p: Patient, c: dict | None = None) -> bool:
    c = c or cfg(db)
    return not c.get("test_only") or p.id in tester_ids(db, c)


def due_reminders(db: Session, now: datetime | None = None) -> list[Appointment]:
    c = cfg(db)
    now = now or datetime.now()
    day = (now + timedelta(days=int(c.get("remind_days") or 1))).date()
    testers = tester_ids(db, c) if c.get("test_only") else set()
    rows = db.scalars(select(Appointment).where(Appointment.start >= datetime.combine(day, datetime.min.time()),
                                                Appointment.start <= datetime.combine(day, datetime.max.time()),
                                                Appointment.status == "booked")).all()
    out = []
    for a in rows:
        p = a.patient
        if p and p.telegram_chat_id and not (a.bot_remind_at if hasattr(a, "bot_remind_at") else None) and (not c.get("test_only") or p.id in testers):
            out.append(a)
    return out


def send_reminders(db: Session, now: datetime | None = None, force: bool = False) -> dict:
    """Щодня о remind_hour: нагадування на завтра з кнопками. Лише якщо функцію ввімкнено (і не стоїть загальна пауза)."""
    from ..integrations import messaging
    c = cfg(db)
    now = now or datetime.now()
    if not force and (not c["features"].get("reminders") or now.hour != int(c.get("remind_hour") or 10)):
        return {"skipped": True}
    sent = failed = 0
    for a in due_reminders(db, now):
        res = messaging.send_telegram(a.patient.telegram_chat_id, remind_text(db, a, c), reply_markup=keyboard(a.id))
        if res.ok:
            a.bot_remind_at = datetime.utcnow()
            sent += 1
        else:
            failed += 1
            if res.blocked:
                a.patient.telegram_chat_id = None
        db.commit()
    return {"sent": sent, "failed": failed}


def answer(db: Session, chat_id: str, aid: int, action: str, reason: str | None = None, wish: str | None = None) -> dict:
    """Відповідь пацієнта на нагадування (кнопка в боті): підтвердити — статус «Підтверджено»; скасувати — «Скасовано» з причиною;
    перенести — задача адміністратору з побажаним часом (запис лишається, доки адміністратор не перенесе)."""
    from . import appointments as aps, tg_notify
    from html import escape
    from ..config import settings
    p = patient(db, chat_id)
    a = db.get(Appointment, aid)
    if not a or a.patient_id != p.id:
        raise BotError("Запис не знайдено")
    if action not in ACTIONS:
        raise BotError("Невідома дія")
    if a.status not in ("booked", "confirmed") or a.start < datetime.now():
        return {"ok": False, "status": a.status, "reply": "Цей запис уже не активний. Якщо потрібна допомога — напишіть адміністратору."}
    when = _when(a.start)
    if action == "confirm":
        aps.set_status(db, a, "confirmed")
        reply, head = f"Дякуємо! Візит підтверджено: {when}. Чекаємо на вас 💜", "✅ Пацієнт підтвердив візит у боті"
    elif action == "cancel":
        why = (reason or "").strip()[:200] or "без пояснення"
        aps.set_status(db, a, "cancelled", f"Скасовано пацієнтом у боті: {why}"[:200])
        reply, head = "Запис скасовано. Якщо захочете записатися знову — ми на звʼязку.", "❌ Пацієнт скасував візит у боті"
    else:
        db.add(Task(title=f"Перенести запис — {p.full_name}, {a.start:%d.%m %H:%M}"[:300], kind="bot", priority="high", patient_id=p.id,
                    assignee_role="admin", due_date=date.today(), source_key=f"botmove:{a.id}:{datetime.now():%Y%m%d%H%M}",
                    description=f"Пацієнт просить перенести візит ({when}, {_svc(a)}) через Бот турботи. "
                                f"Бажаний час: {(wish or '').strip()[:300] or 'не вказано'}. Звʼязатися й перенести."))
        db.commit()
        reply, head = "Прийняли! Адміністратор звʼяжеться з вами, щоб узгодити новий час.", "🔁 Пацієнт просить перенести візит"
    from ..models import AuditLog
    if action in ("confirm", "cancel"):              # історія візиту: як зміна статусу, але «через Бот турботи»
        db.add(AuditLog(action="appointment.status", entity="patient", entity_id=p.id,
                        details={"appt": a.id, "status": a.status, "reason": a.cancel_reason, "via": "Бот турботи (пацієнт)"}))
    db.add(AuditLog(action="bot.answer", entity="patient", entity_id=p.id,
                    details={"appt": a.id, "answer": action, "status": a.status, "via": "Бот турботи", "reason": reason, "wish": wish}))
    db.commit()
    note = f"<b>{head}</b>\n{escape(p.full_name)} · {escape(when)} · {escape(_svc(a))}" + \
           (f"\nПричина: {escape(reason)}" if action == "cancel" and reason else "") + \
           (f"\nБажаний час: {escape(wish)}" if action == "reschedule" and wish else "") + \
           f"\n{settings.public_url.rstrip('/')}/patients/{p.id}"
    for u in tg_notify.recipients(db, "admin"):
        try:
            tg_notify.send_text(u.telegram_chat_id, note)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "status": a.status, "reply": reply}


def config_json(db: Session) -> dict:
    """Для бота: які функції ввімкнено (вимкнені — не показувати в меню) і формат кнопок нагадувань."""
    c = cfg(db)
    tids = tester_ids(db, c) if c.get("test_only") else set()
    chats = [str(x) for (x,) in db.execute(select(Patient.telegram_chat_id).where(Patient.id.in_(list(tids) or [0]))) if x]
    return {"features": c["features"], "test_only": bool(c.get("test_only")), "testers": chats, "callback_format": f"{CB_PREFIX}:<confirm|reschedule|cancel>:<appointment_id>",
            "actions": ACTIONS}


def guides(db: Session, base_url: str) -> list[dict]:
    """«Корисні матеріали»: ті самі гайди, що на сторінці запису (PDF із описом)."""
    from . import online_booking as ob
    b = base_url.rstrip("/")
    return [{"title": g["title"], "tag": g.get("tag") or "", "text": g.get("text") or "",
             "url": (b + g["url"]) if (g.get("url") or "").startswith("/") else g.get("url") or ""}
            for g in ob.cfg(db).get("guides") or [] if g.get("on", True) and g.get("title")]


def doctors(db: Session) -> list[dict]:
    """«Про лікарів»: як на сайті (головна лікарка першою), без контактів."""
    from . import online_booking as ob
    return [{"name": d["name"], "role": d["role"], "bio": d["bio"]} for d in ob.team_cards(db, "")]
