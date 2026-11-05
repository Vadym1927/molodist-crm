"""Сповіщення співробітникам у Telegram за ролями (Налаштування → Співробітники → Сповіщення в Telegram).

Кожен тип: кому (адміністратори / лікарі / головний лікар / власник), увімкнено, о котрій (щодня / у день тижня / 1-го числа).
Надсилається особисто кожному користувачу CRM цієї ролі, у якого в «Мій кабінет» вказано Telegram.
Головний лікар — користувачі, обрані в налаштуваннях (роль лікар або власник).
Планувальник перевіряє кожні 5 хв (job_tg_notify); журнал надісланого — SyncState tg_notify_log (один раз на день на людину й тип)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Appointment, Patient, SyncState, Task, User, Doctor

KEY = "tg_notify"
LOG = "tg_notify_log"
ROLES = {"admin": "Адміністратори", "doctor": "Лікарі", "chief": "Головний лікар", "owner": "Власник"}
DOW = {"mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт", "fri": "Пт", "sat": "Сб", "sun": "Нд"}
# key, назва, опис, періодичність (daily | weekly | monthly), ролі → (on, time)
TYPES = [
    ("today", "Записи на сьогодні", "Хто записаний сьогодні: час, пацієнт, послуги (лікар — лише свої записи)", "daily",
     {"admin": (True, "08:30"), "doctor": (True, "08:30"), "chief": (True, "08:30"), "owner": (False, "08:30")}),
    ("tomorrow", "Записи на завтра — підтвердити", "Записи на завтра без підтвердження: кому зателефонувати", "daily",
     {"admin": (True, "17:00"), "chief": (False, "17:00")}),
    ("plans", "Нагадування по бюті-планах", "Не записані на наступну процедуру, закінчується домашній догляд (лікар — лише свої плани)", "daily",
     {"admin": (True, "10:00"), "doctor": (True, "10:00"), "chief": (False, "10:00")}),
    ("plan_control", "Контроль бюті-планів", "Зведення: активних планів, прострочених, без запису, % реалізації — по лікарях", "weekly",
     {"chief": (True, "09:00"), "owner": (True, "09:00"), "doctor": (False, "09:00")}),
    ("birthdays", "Дні народження пацієнтів", "Сьогодні й за 3 дні: кого привітати (лікар — лише свої пацієнти); текст привітання нижче", "daily",
     {"admin": (True, "09:00"), "doctor": (True, "09:00"), "chief": (False, "09:00"), "owner": (False, "09:00")}),
    ("no_rebook", "Були вчора — не перезаписані", "Пацієнти після вчорашнього візиту без наступного запису: запропонувати запис", "daily",
     {"admin": (True, "10:30"), "doctor": (False, "10:30"), "chief": (False, "10:30")}),
    ("tasks", "Мої задачі", "Задачі на сьогодні й прострочені (особисті й черги ролі)", "daily",
     {"admin": (True, "09:00"), "doctor": (True, "09:00"), "chief": (True, "09:00"), "owner": (True, "09:00")}),
    ("day_close", "Підсумок дня", "Візити, нові пацієнти, чеки, виручка, оплати; лікар — свій день, власник і головний лікар — по лікарях", "daily",
     {"admin": (True, "20:00"), "doctor": (True, "20:00"), "chief": (True, "20:30"), "owner": (True, "20:30")}),
    ("week", "Підсумок тижня", "Виручка, візити, нові пацієнти, скасування за тиждень — порівняно з попереднім", "weekly",
     {"chief": (True, "09:30"), "owner": (True, "09:30"), "doctor": (False, "09:30")}),
    ("month", "Підсумок місяця", "1-го числа: виручка, візити, нові пацієнти за місяць, по лікарях", "monthly",
     {"owner": (True, "10:00"), "chief": (False, "10:00")}),
]
TYPE = {t[0]: t for t in TYPES}
GREETING_DEFAULT = "Вітаємо з Днем народження! 🎉 Бажаємо здоровʼя, краси й гарного настрою. Ваш медичний простір MOLODIST 💜"


# ---------------- налаштування ----------------
def _get(db: Session, key: str, default):
    st = db.get(SyncState, key)
    try:
        return json.loads(st.value) if st and st.value else default
    except ValueError:
        return default


def _put(db: Session, key: str, val) -> None:
    st = db.get(SyncState, key) or SyncState(key=key)
    st.value, st.updated_at = json.dumps(val, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def cfg(db: Session) -> dict:
    """{roles: {role: {key: {on, time, dow}}}, chiefs: [user_id], greeting: str, bday_days: int}"""
    c = _get(db, KEY, {}) or {}
    roles = c.get("roles") or {}
    out = {}
    for role in ROLES:
        out[role] = {}
        for key, _t, _d, per, defaults in TYPES:
            if role not in defaults:
                continue
            d_on, d_time = defaults[role]
            v = (roles.get(role) or {}).get(key) or {}
            out[role][key] = {"on": bool(v.get("on", d_on)), "time": v.get("time") or d_time, "dow": v.get("dow") or "mon", "per": per}
    return {"roles": out, "chiefs": [int(x) for x in c.get("chiefs") or []], "greeting": c.get("greeting") or GREETING_DEFAULT,
            "bday_days": int(c.get("bday_days") or 3)}


def save(db: Session, roles: dict, chiefs: list[int], greeting: str, bday_days: int) -> None:
    c = _get(db, KEY, {}) or {}
    c["roles"] = roles
    c["chiefs"] = [int(x) for x in chiefs]
    c["greeting"] = (greeting or "").strip()[:500] or GREETING_DEFAULT
    c["bday_days"] = max(0, min(int(bday_days or 3), 14))
    _put(db, KEY, c)


def recipients(db: Session, role: str) -> list[User]:
    """Користувачі CRM цієї ролі з Telegram. Головний лікар — обрані в налаштуваннях."""
    users = db.scalars(select(User).where(User.active.is_(True), User.telegram_chat_id.is_not(None))).all()
    if role == "chief":
        ids = set(cfg(db)["chiefs"])
        return [u for u in users if u.id in ids]
    return [u for u in users if u.role == role]


# ---------------- тексти ----------------
_B = f"{settings.public_url.rstrip('/')}"


def _pat(p: Patient | None, phone: bool = True) -> str:
    if not p:
        return "—"
    s = f'<a href="{_B}/patients/{p.id}">{escape(p.full_name)}</a>'
    return s + (f" · {escape(p.phone)}" if phone and p.phone else "")


def _t(dt: datetime) -> str:
    return f"{dt:%H:%M}"


def _uah(v) -> str:
    return f"{(v or 0):,.0f} ₴".replace(",", " ")


def _appts(db: Session, d: date, doctor_id: int | None = None) -> list[Appointment]:
    a, b = datetime.combine(d, datetime.min.time()), datetime.combine(d + timedelta(days=1), datetime.min.time())
    q = select(Appointment).where(Appointment.start >= a, Appointment.start < b, Appointment.status.notin_(("cancelled", "no_show")))
    if doctor_id:
        q = q.where(Appointment.doctor_id == doctor_id)
    return db.scalars(q.order_by(Appointment.start)).all()


def _docs(db: Session) -> dict:
    return {d.id: d.name for d in db.scalars(select(Doctor))}


def text_today(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    d = d or date.today()
    did = u.doctor_id if role == "doctor" else None
    xs = _appts(db, d, did)
    if not xs:
        return f"📅 <b>{d:%d.%m}</b>: записів немає" + (" (ваших)" if did else "") + "."
    docs = _docs(db)
    lines = [f"📅 <b>Записи на {d:%d.%m}</b> · {len(xs)}"]
    cur = None
    for x in xs:
        if not did and x.doctor_id != cur:
            cur = x.doctor_id
            lines.append(f"\n👩‍⚕️ <b>{escape(docs.get(cur, '—'))}</b>")
        sv = ", ".join(s.get("name", "") for s in x.services or []) or (x.comment or "")
        lines.append(f"{_t(x.start)} {_pat(x.patient, role != 'doctor')}" + (f" — {escape(sv[:80])}" if sv else "")
                     + (" ✅" if x.status in ("confirmed", "arrived", "paid", "done") else ""))
    lines.append(f'\n<a href="{_B}/calendar?start={d}">Календар</a>')
    return "\n".join(lines)


def text_tomorrow(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    d = (d or date.today()) + timedelta(days=1)
    xs = [x for x in _appts(db, d) if x.status == "booked"]
    if not xs:
        return f"✅ На {d:%d.%m} усі записи підтверджені."
    docs = _docs(db)
    lines = [f"📞 <b>Підтвердити записи на {d:%d.%m}</b> · {len(xs)}"]
    for x in xs:
        lines.append(f"{_t(x.start)} {_pat(x.patient)} — {escape(docs.get(x.doctor_id, '—'))}")
    lines.append(f'\n<a href="{_B}/calendar?start={d}">Календар</a>')
    return "\n".join(lines)


def text_plans(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    from . import reminders
    rems = reminders.collect(db)
    if role == "doctor" and u.doctor_id:
        from ..models import BeautyPlan
        own = {p for (p,) in db.execute(select(BeautyPlan.patient_id).where(BeautyPlan.doctor_id == u.doctor_id))}
        rems = [r for r in rems if r.patient_id in own]
    if not rems:
        return None
    msgs = reminders.digest_messages(rems)
    return "\n\n".join(msgs)[:4000]


def text_plan_control(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    from ..services.kpi import all_statuses
    now = datetime.now()
    by: dict = {}
    for ps in all_statuses(db):
        pl = ps.plan
        if pl.status != "active":
            continue
        if role == "doctor" and u.doctor_id and pl.doctor_id != u.doctor_id:
            continue
        k = pl.doctor.name if pl.doctor else "без лікаря"
        g = by.setdefault(k, {"n": 0, "over": 0, "nobook": 0, "pct": []})
        g["n"] += 1
        g["pct"].append(ps.realization_pct or 0)
        if ps.next_procedure and not ps.is_booked:
            g["nobook"] += 1
            if ps.due_date and ps.due_date <= now:
                g["over"] += 1
    if not by:
        return "📋 Активних бюті-планів немає."
    lines = ["📋 <b>Контроль бюті-планів</b>"]
    tot = {"n": 0, "over": 0, "nobook": 0}
    for k, g in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
        avg = round(sum(g["pct"]) / len(g["pct"])) if g["pct"] else 0
        lines.append(f"• <b>{escape(k)}</b>: {g['n']} план., без запису {g['nobook']}" + (f" (прострочено {g['over']})" if g["over"] else "")
                     + f", реалізація {avg}%")
        for z in tot:
            tot[z] += g[z]
    lines.append(f"\nРазом: {tot['n']} активних, без запису {tot['nobook']}, прострочено {tot['over']}")
    lines.append(f'<a href="{_B}/plans">Бюті-плани</a>')
    return "\n".join(lines)


def _age(b: date, on: date) -> int:
    return on.year - b.year - ((on.month, on.day) < (b.month, b.day))


def text_birthdays(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    d = d or date.today()
    c = cfg(db)
    days = c["bday_days"]
    q = select(Patient).where(Patient.birthday.is_not(None))
    if role == "doctor" and u.doctor_id:
        q = q.where(Patient.doctor_id == u.doctor_id)
    today, soon = [], []
    for p in db.scalars(q):
        b = p.birthday
        try:
            nxt = b.replace(year=d.year)
        except ValueError:
            nxt = b.replace(year=d.year, day=28)
        if nxt < d:
            try:
                nxt = b.replace(year=d.year + 1)
            except ValueError:
                nxt = b.replace(year=d.year + 1, day=28)
        dd = (nxt - d).days
        if dd == 0:
            today.append(p)
        elif 0 < dd <= days:
            soon.append((dd, p))
    if not today and not soon:
        return None
    lines = []
    if today:
        lines.append(f"🎂 <b>Сьогодні день народження</b> · {len(today)}")
        for p in today[:40]:
            lines.append(f"• {_pat(p)} — {_age(p.birthday, d)} р.")
        lines.append("\nТекст привітання (скопіюйте):\n<code>" + escape(c["greeting"]) + "</code>")
    if soon:
        lines.append(f"\n🎁 <b>Найближчі {days} дн.</b>")
        for dd, p in sorted(soon)[:40]:
            lines.append(f"• {p.birthday:%d.%m} {_pat(p)}")
    return "\n".join(lines)


def text_no_rebook(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    d = (d or date.today()) - timedelta(days=1)
    did = u.doctor_id if role == "doctor" else None
    xs = [x for x in _appts(db, d, did) if x.status in ("done", "paid", "arrived") or x.sale_id]
    if not xs:
        return None
    now = datetime.now()
    pids = {x.patient_id for x in xs}
    booked = {p for (p,) in db.execute(select(Appointment.patient_id).where(Appointment.patient_id.in_(pids), Appointment.start > now,
                                                                          Appointment.status.notin_(("cancelled", "no_show"))))}
    docs = _docs(db)
    rows = [x for x in xs if x.patient_id not in booked]
    if not rows:
        return f"✅ Усі пацієнти після {d:%d.%m} уже перезаписані."
    lines = [f"🔁 <b>Були {d:%d.%m}, без наступного запису</b> · {len(rows)}"]
    seen = set()
    for x in rows:
        if x.patient_id in seen:
            continue
        seen.add(x.patient_id)
        sv = ", ".join(s.get("name", "") for s in x.services or [])
        lines.append(f"• {_pat(x.patient)} — {escape(docs.get(x.doctor_id, '—'))}" + (f": {escape(sv[:60])}" if sv else ""))
    return "\n".join(lines)


def text_tasks(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    d = d or date.today()
    q = select(Task).where(Task.status == "open", (Task.assignee_id == u.id) | ((Task.assignee_id.is_(None)) & (Task.assignee_role == u.role)))
    xs = [t for t in db.scalars(q.order_by(Task.due_date)) if t.due_date and t.due_date <= d]
    if not xs:
        return None
    over = [t for t in xs if t.due_date < d]
    today = [t for t in xs if t.due_date == d]
    lines = ["✅ <b>Мої задачі</b>"]
    if over:
        lines.append(f"\n⚠️ <b>Прострочені</b> · {len(over)}")
        lines += [f"• {t.due_date:%d.%m} {escape(t.title)}" + (f" — {_pat(t.patient, False)}" if t.patient_id and t.patient else "") for t in over[:25]]
    if today:
        lines.append(f"\n📌 <b>На сьогодні</b> · {len(today)}")
        lines += [f"• {escape(t.title)}" + (f" — {_pat(t.patient, False)}" if t.patient_id and t.patient else "") for t in today[:25]]
    lines.append(f'\n<a href="{_B}/tasks?f=mine">Задачі</a>')
    return "\n".join(lines)


def text_day_close(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    from . import day_stats
    d = d or date.today()
    did = u.doctor_id if role == "doctor" else None
    s = day_stats.compute(db, d, did)
    lines = [f"📊 <b>Підсумок дня {d:%d.%m}</b>" + (" (ваш)" if did else "")]
    lines.append(f"Візитів: {s['visits']['done']} з {s['visits']['n']} · нових пацієнтів: {s['patients']['new']}"
                 + (f" · неявки: {s['no_show']['n']}" if s["no_show"]["n"] else "") + (f" · скасовано: {s['cancel_today']['n']}" if s["cancel_today"]["n"] else ""))
    lines.append(f"Виручка: <b>{_uah(s['income']['fact'])}</b> · чеків {s['checks']['n']} · середній {_uah(s['income']['avg'])}")
    k = s["kinds"]
    parts = [f"{lbl} {_uah(k[key]['sum'])}" for key, lbl in (("service", "послуги"), ("product", "косметика"), ("cert", "сертифікати"), ("sub", "абонементи")) if k.get(key)]
    if parts:
        lines.append(" · ".join(parts))
    if role != "doctor" and s["pay"]:
        lines.append("Оплати: " + " · ".join(f"{escape(str(m))} {_uah(v)}" for m, v in list(s["pay"].items())[:5]))
    if role in ("owner", "chief") and s["doctors"]:
        lines.append("\n<b>По лікарях</b>")
        for x in s["doctors"][:10]:
            lines.append(f"• {escape(x['name'])}: {x['visits']} віз. · послуги {_uah(x['services'])} · косметика {_uah(x['products'])}")
    if s["created"]["n"]:
        lines.append(f"\nНових записів за день: {s['created']['n']}" + (f" (онлайн {s['created']['online']})" if s["created"]["online"] else ""))
    lines.append(f'<a href="{_B}/finance/day?d={d}">Продажі за день</a>')
    return "\n".join(lines)


def _period(db: Session, a: date, b: date, did: int | None) -> dict:
    from . import day_stats
    tot = {"rev": 0.0, "visits": 0, "done": 0, "new": 0, "canc": 0, "checks": 0, "docs": {}}
    d = a
    while d <= b:
        s = day_stats.compute(db, d, did)
        tot["rev"] += s["income"]["fact"]
        tot["visits"] += s["visits"]["n"]
        tot["done"] += s["visits"]["done"]
        tot["new"] += s["patients"]["new"]
        tot["canc"] += s["cancel_day"]["n"] + s["no_show"]["n"]
        tot["checks"] += s["checks"]["n"]
        for x in s["doctors"]:
            g = tot["docs"].setdefault(x["name"], {"v": 0, "s": 0.0, "p": 0.0})
            g["v"] += x["visits"]
            g["s"] += x["services"]
            g["p"] += x["products"]
        d += timedelta(days=1)
    return tot


def _cmp(cur: float, prev: float) -> str:
    if not prev:
        return ""
    d = round(100 * (cur - prev) / prev)
    return f" ({'+' if d > 0 else ''}{d}%)"


def text_week(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    d = d or date.today()
    b = d - timedelta(days=d.weekday() + 1)          # минула неділя
    a = b - timedelta(days=6)
    did = u.doctor_id if role == "doctor" else None
    cur, prev = _period(db, a, b, did), _period(db, a - timedelta(days=7), b - timedelta(days=7), did)
    lines = [f"📈 <b>Тиждень {a:%d.%m}–{b:%d.%m}</b>" + (" (ваш)" if did else "")]
    lines.append(f"Виручка: <b>{_uah(cur['rev'])}</b>{_cmp(cur['rev'], prev['rev'])} · чеків {cur['checks']}")
    lines.append(f"Візитів: {cur['done']}{_cmp(cur['done'], prev['done'])} · нових пацієнтів: {cur['new']}{_cmp(cur['new'], prev['new'])} · скасувань і неявок: {cur['canc']}")
    if role in ("owner", "chief") and cur["docs"]:
        lines.append("\n<b>По лікарях</b>")
        for n, g in sorted(cur["docs"].items(), key=lambda kv: -(kv[1]["s"] + kv[1]["p"]))[:10]:
            lines.append(f"• {escape(n)}: {g['v']} віз. · {_uah(g['s'] + g['p'])}")
    lines.append(f'<a href="{_B}/reports?r=revenue">Звіти</a>')
    return "\n".join(lines)


def text_month(db: Session, u: User, role: str, d: date | None = None) -> str | None:
    d = d or date.today()
    b = d.replace(day=1) - timedelta(days=1)
    a = b.replace(day=1)
    pa = (a - timedelta(days=1)).replace(day=1)
    did = u.doctor_id if role == "doctor" else None
    cur, prev = _period(db, a, b, did), _period(db, pa, a - timedelta(days=1), did)
    lines = [f"📆 <b>Місяць {a:%m.%Y}</b>"]
    lines.append(f"Виручка: <b>{_uah(cur['rev'])}</b>{_cmp(cur['rev'], prev['rev'])} · чеків {cur['checks']}")
    lines.append(f"Візитів: {cur['done']}{_cmp(cur['done'], prev['done'])} · нових пацієнтів: {cur['new']}{_cmp(cur['new'], prev['new'])} · скасувань і неявок: {cur['canc']}")
    if cur["docs"]:
        lines.append("\n<b>По лікарях</b>")
        for n, g in sorted(cur["docs"].items(), key=lambda kv: -(kv[1]["s"] + kv[1]["p"]))[:12]:
            lines.append(f"• {escape(n)}: {g['v']} віз. · послуги {_uah(g['s'])} · косметика {_uah(g['p'])}")
    lines.append(f'<a href="{_B}/reports/doctors-kpi">KPI лікарів</a>')
    return "\n".join(lines)


BUILDERS = {"today": text_today, "tomorrow": text_tomorrow, "plans": text_plans, "plan_control": text_plan_control, "birthdays": text_birthdays,
            "no_rebook": text_no_rebook, "tasks": text_tasks, "day_close": text_day_close, "week": text_week, "month": text_month}


# ---------------- надсилання ----------------
def send_text(chat: str, text: str) -> bool:
    from . import alerts
    return alerts._api("sendMessage", data={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"})


def send_one(db: Session, key: str, role: str, u: User, d: date | None = None) -> bool:
    text = BUILDERS[key](db, u, role, d)
    if not text:
        return False
    return send_text(u.telegram_chat_id, text)


def _due(v: dict, now: datetime, per: str) -> bool:
    try:
        hh, mm = (int(x) for x in str(v.get("time") or "09:00").split(":")[:2])
    except ValueError:
        hh, mm = 9, 0
    at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if not (at <= now < at + timedelta(hours=3)):         # не пізніше 3 год після часу (після перезапуску — не серед ночі)
        return False
    if per == "weekly":
        return list(DOW)[now.weekday()] == (v.get("dow") or "mon")
    if per == "monthly":
        return now.day == 1
    return True


def run(db: Session, now: datetime | None = None) -> int:
    """Надіслати все, що настало за часом і ще не надсилалось сьогодні. Повертає кількість повідомлень."""
    now = now or datetime.now()
    c = cfg(db)
    log = _get(db, LOG, {}) or {}
    today = now.date().isoformat()
    n = 0
    for role, items in c["roles"].items():
        users = None
        for key, v in items.items():
            if not v["on"] or not _due(v, now, v["per"]):
                continue
            if users is None:
                users = recipients(db, role)
            for u in users:
                lk = f"{role}:{key}:{u.id}"
                if log.get(lk) == today:
                    continue
                try:
                    send_one(db, key, role, u, now.date())
                except Exception:  # noqa: BLE001
                    import logging
                    logging.getLogger("tg_notify").exception("%s %s", key, u.id)
                log[lk] = today
                n += 1
    if n:
        _put(db, LOG, {k: v for k, v in log.items() if v == today})
    return n
