"""Відгуки й NPS: через N хвилин після закритого візиту пацієнт отримує посилання на коротке опитування.
Усі оцінки «5» → пропонуємо продублювати відгук у Google (текст копіюється, відкривається форма відгуку Google).
Хоч одна оцінка нижча за 5 → рекламація: задача адміністраторам і сповіщення власнику, головному лікарю й адміністраторам.
У Telegram іде лише факт і оцінки — текст відгуку читають у картці пацієнта."""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Appointment, Doctor, Patient, Review, ReviewArchive, SyncState, Task, User

KEY = "reviews"
QUESTIONS = [{"key": "doctor", "title": "Лікар: увага й пояснення"}, {"key": "result", "title": "Процедура й результат"},
             {"key": "admin", "title": "Адміністратори: запис і зустріч"}, {"key": "space", "title": "Атмосфера й чистота"}]
ORDERS = {"tg": ("Telegram-бот → Viber → SMS", ["telegram", "viber", "sms"]), "viber": ("Viber → Telegram-бот → SMS", ["viber", "telegram", "sms"]),
          "sms": ("Лише SMS", ["sms"])}
DEFAULT = {"on": False, "delay_min": 30, "min_days": 7, "google_url": "", "questions": QUESTIONS, "order": "tg",
           "ttl_hours": 24,                 # посилання діє стільки годин після надсилання
           "base_count": 0, "base_avg": 5.0,   # відгуки, зібрані до CRM (CleverBox): кількість і середня оцінка — для рейтингу в шапці
           "text": "{name}, дякуємо, що були в MOLODIST сьогодні 🤍 Будь ласка, оцініть візит — це займе хвилину: {link}",
           # шаблони для ручного надсилання адміністратором (кнопка «Надіслати опитування» у закритому візиті)
           "templates": [
               {"name": "Після візиту", "text": "{name}, дякуємо, що були в MOLODIST сьогодні 🤍 Будь ласка, оцініть візит — це займе хвилину: {link}"},
               {"name": "Після курсу процедур", "text": "{name}, ви завершили курс у MOLODIST — дякуємо за довіру 🤍 Поділіться, будь ласка, враженнями: {link}"},
               {"name": "Коротко", "text": "{name}, оцініть, будь ласка, ваш візит у MOLODIST: {link} Дякуємо 🤍"},
           ]}
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


def templates_of(c: dict) -> list[dict]:
    return [t for t in (c.get("templates") or []) if (t.get("text") or "").strip()] or DEFAULT["templates"]


def first_name(p: Patient | None) -> str:
    """ПІБ пишеться «Прізвище Ім'я По батькові» — звертаємось на ім'я."""
    parts = (p.full_name or "").split() if p else []
    return parts[1] if len(parts) > 1 else (parts[0] if parts else "")


def manual(db: Session, ap: Appointment) -> tuple[Review, list[dict]]:
    """Ручне надсилання адміністратором: індивідуальне посилання для візиту (одне на візит, поки не відповіли)
    і тексти за шаблонами. Надсилання — вручну обраним каналом; позначка «надіслано» — mark_sent."""
    c = cfg(db)
    r = db.scalar(select(Review).where(Review.appointment_id == ap.id).order_by(Review.id.desc()))
    if not r or r.answered_at or (r.sent_at and expired(db, r)):
        r = Review(token=secrets.token_urlsafe(12), patient_id=ap.patient_id, appointment_id=ap.id, doctor_id=ap.doctor_id)
        db.add(r)
        db.commit()
    name = first_name(db.get(Patient, ap.patient_id)) or "Вітаємо"
    texts = [{"name": t.get("name") or f"Шаблон {i + 1}", "text": t["text"].replace("{name}", name).replace("{link}", link(r))}
             for i, t in enumerate(templates_of(c))]
    return r, texts


def mark_sent(db: Session, r: Review, channel: str) -> None:
    r.channel, r.send_error = (channel or "manual")[:20], None
    r.sent_at = r.sent_at or datetime.utcnow()
    db.commit()


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


def general(db: Session, phone: str, days: int = 14) -> Review:
    """Загальне посилання на опитування (одне для всіх): пацієнт вводить телефон → картка → опитування за останнім візитом."""
    from .appeals import find_patient
    d = re.sub(r"\D", "", phone or "")
    p = find_patient(db, d) if len(d) >= 9 else None
    if not p:
        raise ValueError("Не знайшли цей номер серед пацієнтів. Перевірте номер або напишіть нам — ми допоможемо.")
    return survey_for(db, p, days)


def by_cleverbox(db: Session, cid: int, days: int = 14) -> Review:
    """Персональне посилання без введення телефону: /r/c/{ID клієнта CleverBox} (змінна шаблону повідомлення CleverBox).
    Працює лише якщо в пацієнта був візит за останні `days` днів — інакше номер у посиланні нічого не відкриває."""
    p = db.scalar(select(Patient).where(Patient.cleverbox_id == cid))
    if not p:
        raise ValueError("none")
    since = datetime.now() - timedelta(days=days)
    if not any(a.status not in ("cancelled", "no_show") for a in db.scalars(select(Appointment).where(
            Appointment.patient_id == p.id, Appointment.start >= since, Appointment.start <= datetime.now()))):
        raise ValueError("none")
    return survey_for(db, p, days)


def survey_for(db: Session, p: Patient, days: int = 14) -> Review:
    """Опитування за останнім візитом пацієнта за `days` днів. Невідповідене — те саме, повторно не створюємо."""
    since = datetime.now() - timedelta(days=days)
    ap = next((a for a in db.scalars(select(Appointment).where(Appointment.patient_id == p.id, Appointment.start >= since,
                                                                Appointment.start <= datetime.now()).order_by(Appointment.start.desc()))
               if a.status not in ("cancelled", "no_show")), None)
    q = select(Review).where(Review.patient_id == p.id, Review.answered_at.is_(None), Review.created_at >= datetime.utcnow() - timedelta(days=days))
    r = next((x for x in db.scalars(q.order_by(Review.id.desc())) if not ap or x.appointment_id in (None, ap.id)), None)
    if not r:
        if ap and db.scalar(select(Review.id).where(Review.appointment_id == ap.id, Review.answered_at.is_not(None))):
            raise ValueError("Дякуємо, відгук про цей візит ви вже залишили.")
        r = Review(token=secrets.token_urlsafe(12), patient_id=p.id, appointment_id=ap.id if ap else None, doctor_id=ap.doctor_id if ap else None)
        db.add(r)
    r.sent_at, r.channel, r.send_error = datetime.utcnow(), "link", None       # строк дії посилання — від цього моменту
    db.commit()
    return r


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
    an, asm = db.execute(select(func.count(ReviewArchive.id), func.sum(ReviewArchive.avg))).one()
    if an:                                     # архів завантажено — рахуємо з нього, а не з цифр у налаштуваннях
        bn, bs = int(an), float(asm or 0)
    else:
        bn = int(c.get("base_count") or 0)
        bs = bn * float(c.get("base_avg") or 5)
    tot = bn + int(n or 0)
    avg = (bs + float(sm or 0)) / tot if tot else None
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


# ---------------- відгуки до CRM (звіт CleverBox «Feedback») ----------------

def _sheet_rows(data: bytes) -> list[list]:
    import io
    import xlrd
    sh = xlrd.open_workbook(file_contents=data, ignore_workbook_corruption=True, logfile=io.StringIO()).sheet_by_index(0)
    return [sh.row_values(i) for i in range(sh.nrows)]


def import_feedback(db: Session, data: bytes) -> dict:
    """Файл Feedback_*.xls з CleverBox: №, дата відгуку, філія, клієнт, дата візиту, хто надавав, хто оформлював, оцінки, коментар, середня.
    Повторне завантаження оновлює за №. Пацієнт привʼязується лише за точним збігом ПІБ з одним пацієнтом."""
    import re
    from ..integrations.cleverbox import match_doctor
    rows = _sheet_rows(data)
    if not rows:
        raise ValueError("Файл порожній")
    head = [str(x).strip().lower() for x in rows[0]]

    def col(*names):
        return next((i for i, h in enumerate(head) if any(n in h for n in names)), None)
    c_no, c_at, c_pat, c_vis, c_doc = col("№"), col("дата створення"), col("клієнт", "пацієнт"), col("дата візиту"), col("хто надавав")
    c_sc, c_txt, c_avg = col("оцінки"), col("коментар"), col("середня")
    if None in (c_no, c_pat, c_avg):
        raise ValueError("Це не звіт «Відгуки» з CleverBox: немає колонок №, Клієнт, Середня оцінка")
    doctors = db.scalars(select(Doctor)).all()
    by_name: dict[str, list[int]] = {}
    for pid, nm in db.execute(select(Patient.id, Patient.full_name)):
        by_name.setdefault(" ".join((nm or "").lower().split()), []).append(pid)
    have = {x.ext: x for x in db.scalars(select(ReviewArchive))}
    new = upd = linked = 0

    def day(v, fmt):
        try:
            return datetime.strptime(str(v).strip(), fmt)
        except ValueError:
            return None
    for row in rows[1:]:
        no = str(row[c_no]).strip().split(".")[0]
        if not no.isdigit():
            continue
        ext = f"cb:{no}"
        x = have.get(ext)
        if x is None:
            x = ReviewArchive(ext=ext)
            db.add(x)
            have[ext] = x
            new += 1
        else:
            upd += 1
        nm = " ".join(str(row[c_pat]).split())
        ids = by_name.get(nm.lower(), [])
        d_at = day(row[c_at], "%d.%m.%Y") if c_at is not None else None
        x.at = d_at.date() if d_at else None
        x.visit_at = day(row[c_vis], "%d.%m.%y %H:%M") if c_vis is not None else None
        x.patient_name, x.patient_id = nm[:200] or None, (ids[0] if len(ids) == 1 else None)
        linked += x.patient_id is not None
        dn = " ".join(str(row[c_doc]).split()) if c_doc is not None else ""
        doc = match_doctor(dn, doctors) if dn else None
        x.doctor_name, x.doctor_id = dn[:200] or None, (doc.id if doc else None)
        sc = [[m.group(1).strip(), int(m.group(2))] for m in re.finditer(r"([^\n=]+?)\s*=\s*(\d+)", str(row[c_sc]))] if c_sc is not None else []
        x.scores = sc or None
        try:
            x.avg = float(str(row[c_avg]).replace(",", "."))
        except ValueError:
            x.avg = round(sum(v for _, v in sc) / len(sc), 2) if sc else None
        x.text = (" ".join(str(row[c_txt]).split()) or None) if c_txt is not None else None
    db.commit()
    return {"new": new, "updated": upd, "linked": linked, "total": len(have)}


def archive_public(db: Session, limit: int = 12) -> dict:
    """Для сторінки опитування: анонімні відгуки з текстом на «5» (найсвіжіші) і скільки ще оцінок без коментаря."""
    from sqlalchemy import func
    q = select(ReviewArchive).where(ReviewArchive.text.is_not(None), ReviewArchive.avg >= 5, ReviewArchive.hidden.is_not(True))
    rows = db.scalars(q.order_by(ReviewArchive.at.desc().nullslast(), ReviewArchive.id.desc()).limit(limit)).all()
    total = db.scalar(select(func.count(ReviewArchive.id))) or 0
    with_text = db.scalar(select(func.count(ReviewArchive.id)).where(ReviewArchive.text.is_not(None))) or 0
    return {"rows": rows, "total": total, "no_text": total - with_text}


def archive_for_patient(db: Session, patient_id: int) -> list[ReviewArchive]:
    return db.scalars(select(ReviewArchive).where(ReviewArchive.patient_id == patient_id).order_by(ReviewArchive.at.desc())).all()
