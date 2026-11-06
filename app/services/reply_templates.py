"""Шаблони відповідей пацієнтам: для Instagram, Facebook Messenger, SMS, Viber і Telegram.

Зберігаються в SyncState `reply_templates` (JSON-список). Змінні в тексті — у фігурних дужках ({імʼя}, {запис}…):
підставляються з картки пацієнта (якщо діалог привʼязаний) або з імені співрозмовника.
Налаштування: Налаштування → Комунікації → «Шаблони відповідей» (/settings/reply-templates).
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, Patient, SyncState, Visit

KEY = "reply_templates"
CHANNELS = {"instagram": "Instagram", "facebook": "Facebook", "sms": "SMS", "viber": "Viber", "telegram": "Telegram"}
VARS = {
    "імʼя": "Імʼя пацієнта (без прізвища)",
    "адмін": "Імʼя адміністратора, що відповідає",
    "запис": "Найближчий запис: дата, час, лікар",
    "дата_запису": "Дата найближчого запису",
    "час_запису": "Час найближчого запису",
    "лікар": "Лікар найближчого запису або з бюті-плану",
    "процедура": "Наступна процедура з бюті-плану",
    "клініка": "Назва клініки",
    "онлайн_запис": "Посилання на онлайн-запис",
}
ONLINE = "https://cbox.mobi/go/molodist"
DEFAULT = [
    {"title": "Вітання", "cat": "Перше повідомлення", "text": "Вітаємо, {імʼя}! 💜 Дякуємо, що написали в MOLODIST. Мене звати {адмін}, я з радістю допоможу. Підкажіть, будь ласка, що саме вас цікавить?"},
    {"title": "Вартість — консультація", "cat": "Ціни", "text": "{імʼя}, вартість залежить від стану шкіри й обраного протоколу. Лікар підбере процедуру на консультації — так ви отримаєте результат саме під ваш запит. Підібрати зручний час?"},
    {"title": "Онлайн-запис", "cat": "Запис", "text": "Записатися можна онлайн у зручний для вас час: {онлайн_запис}\nАбо напишіть бажаний день — підберемо вільне віконце 🌸"},
    {"title": "Підтвердження запису", "cat": "Запис", "text": "{імʼя}, нагадуємо: ви записані {запис}. Чекаємо на вас! Якщо плани зміняться — напишіть, будь ласка, ми перенесемо запис."},
    {"title": "Адреса", "cat": "Інформація", "text": "Ми знаходимося в Києві. Перед візитом надішлемо точну адресу й підказку, як нас знайти 💜"},
    {"title": "Неробочий час", "cat": "Інформація", "text": "Дякуємо за повідомлення! Зараз команда відпочиває — відповімо на початку робочого дня. Записатися онлайн: {онлайн_запис}"},
    {"title": "Після процедури", "cat": "Після візиту", "text": "{імʼя}, дякуємо, що були в нас! Як ви почуваєтеся після процедури? Якщо будуть питання щодо догляду — пишіть, ми на звʼязку."},
]


def _norm(t: dict) -> dict:
    ch = [c for c in (t.get("channels") or []) if c in CHANNELS]
    return {"id": str(t.get("id") or secrets.token_hex(4)), "title": (t.get("title") or "").strip()[:80] or "Без назви",
            "cat": (t.get("cat") or "").strip()[:40], "text": (t.get("text") or "").strip()[:2000], "channels": ch}


def all_(db: Session) -> list[dict]:
    st = db.get(SyncState, KEY)
    if not st or not st.value:
        return [_norm(dict(t, id=f"d{i}")) for i, t in enumerate(DEFAULT)]
    try:
        return [_norm(t) for t in json.loads(st.value)]
    except ValueError:
        return []


def save(db: Session, rows: list[dict]) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps([_norm(r) for r in rows if (r.get("text") or "").strip()], ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def for_channel(db: Session, channel: str) -> list[dict]:
    """Шаблони для каналу: без вибраних каналів — для всіх."""
    return [t for t in all_(db) if not t["channels"] or channel in t["channels"]]


def upcoming(db: Session, patient_id: int, limit: int = 3) -> list[dict]:
    """Майбутні записи пацієнта: CRM (Appointment) + CleverBox (Visit booked), без дублів за часом."""
    now = datetime.now()
    out: list[dict] = []
    from ..models import Doctor
    docs = {d.id: d.name for d in db.scalars(select(Doctor))}
    for a in db.scalars(select(Appointment).where(Appointment.patient_id == patient_id, Appointment.start >= now,
                                                  Appointment.status.in_(("booked", "confirmed"))).order_by(Appointment.start).limit(limit)):
        out.append({"at": a.start, "doctor": docs.get(a.doctor_id) or "", "what": ", ".join(s.get("name", "") for s in (a.services or []))[:120],
                    "status": "підтверджено" if a.status == "confirmed" else "записаний", "appt": a.id})
    seen = {o["at"].replace(second=0, microsecond=0) for o in out}
    for v in db.scalars(select(Visit).where(Visit.patient_id == patient_id, Visit.starts_at >= now, Visit.status == "booked")
                        .order_by(Visit.starts_at).limit(limit)):
        if v.starts_at.replace(second=0, microsecond=0) in seen:
            continue
        out.append({"at": v.starts_at, "doctor": v.master_name or "", "what": v.service_name or "", "status": "записаний", "appt": None})
    out.sort(key=lambda x: x["at"])
    return out[:limit]


_PATR = re.compile(r"(івна|ївна|овна|евна|ович|йович|івич|ївич)$", re.I)


def first_name(full: str) -> str:
    """«Малюк Аліна Вікторівна» → «Аліна», «Аліна Малюк» → «Аліна»: спершу за по батькові, далі — евристика скриптів."""
    from . import scripts as sc
    w = (full or "").split()
    if len(w) >= 3 and _PATR.search(w[2]):
        return w[1]
    if len(w) >= 2 and _PATR.search(w[1]):
        return w[0]
    return sc.first_name(full or "")


def context(db: Session, patient: Patient | None, name: str | None = None, admin_name: str | None = None) -> dict:
    from . import scripts as sc
    ctx = {k: "" for k in VARS}
    if patient:
        try:
            ctx.update({k: v for k, v in sc.context(db, patient, admin_name=admin_name).items() if k in VARS and v})
        except Exception:  # noqa: BLE001
            pass
        ctx["імʼя"] = first_name(patient.full_name or "")
        nx = upcoming(db, patient.id, 1)
        if nx:
            n = nx[0]
            ctx.update(дата_запису=f"{n['at']:%d.%m}", час_запису=f"{n['at']:%H:%M}",
                       запис=f"{n['at']:%d.%m о %H:%M}" + (f" до лікаря {n['doctor']}" if n["doctor"] else ""))
            if n["doctor"]:
                ctx["лікар"] = n["doctor"]
    elif name:
        nm = re.sub(r"^@\S+\s*·?\s*", "", name).strip() or ""
        w = nm.split()
        ctx["імʼя"] = w[0].capitalize() if w and not w[0].startswith("@") else ""
    ctx.update(клініка="MOLODIST", онлайн_запис=ONLINE, адмін=(admin_name or "").split()[0] if admin_name else "")
    return ctx


KEEP_EMPTY = {"запис", "дата_запису", "час_запису", "процедура", "лікар"}   # немає даних — лишаємо {змінну}, щоб адмін дописав


def render(text: str, ctx: dict) -> str:
    def sub(m):
        k = m.group(1).strip()
        v = ctx.get(k)
        return m.group(0) if v is None or (not v and k in KEEP_EMPTY) else str(v)
    out = re.sub(r"\{([^{}]+)\}", sub, text)
    out = re.sub(r" +([,.!?])", r"\1", out)          # «Вітаємо, !» без імені → «Вітаємо!»
    out = out.replace(", !", "!").replace(",!", "!")
    return re.sub(r"[ \t]{2,}", " ", out).strip()
