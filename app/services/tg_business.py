"""Telegram Business: особистий акаунт клініки (@molodist_ua) → центр спілкування CRM.

Власник акаунта з Telegram Premium підключає нашого бота: Налаштування → Telegram для бізнесу → Чат-боти.
Бот отримує нові повідомлення в особистих чатах акаунта (історію до підключення — ні; групи й канали — ні):
• вхідне від людини → ChatMessage (канал telegram) + звернення з джерелом «Telegram» (як для Instagram);
• повідомлення, яке адміністратор написав у самому Telegram, → вихідне в тій самій переписці;
• відповідь з CRM іде від імені акаунта клініки (якщо в правах бота ввімкнено «відповідати на повідомлення»).
Звернення з Telegram за часом привʼязується до кліку з реклами на сайті (site_leads.match_clicks) — так рахується в кампанію.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ChatMessage, Patient, SyncState

log = logging.getLogger("tg_business")
KEY = "tg_business"
PREFIX = "tg:"


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else {}
    except ValueError:
        return {}


def save_cfg(db: Session, d: dict) -> None:
    db.merge(SyncState(key=KEY, value=json.dumps(d, ensure_ascii=False, default=str), updated_at=datetime.utcnow()))
    db.commit()


def _log(c: dict, note: str) -> None:
    c["log"] = ([{"at": datetime.now().isoformat(timespec="seconds"), "note": note[:200]}] + (c.get("log") or []))[:20]


def on_connection(db: Session, conn: dict) -> dict:
    """business_connection: підключили / відключили / змінили права бота."""
    c = cfg(db)
    u = conn.get("user") or {}
    rights = conn.get("rights") or {}
    can_reply = bool(rights.get("can_reply", conn.get("can_reply")))
    c.update({"connection_id": conn.get("id"), "user_id": u.get("id"), "enabled": bool(conn.get("is_enabled")),
              "account": " ".join(filter(None, (u.get("first_name"), u.get("last_name")))) + (f" @{u['username']}" if u.get("username") else ""),
              "can_reply": can_reply, "at": datetime.now().isoformat(timespec="minutes")})
    _log(c, ("підключено" if c["enabled"] else "відключено") + (", може відповідати" if can_reply else ", лише читання"))
    save_cfg(db, c)
    return c


def _who(u: dict) -> str:
    nm = " ".join(filter(None, (u.get("first_name"), u.get("last_name")))).strip()
    return (f"@{u['username']}" + (f" · {nm}" if nm else "")) if u.get("username") else (nm or f"Telegram {u.get('id')}")


def _patient(db: Session, u: dict, phone: str | None) -> Patient | None:
    from . import appeals as ap
    uid, un = str(u.get("id") or ""), (u.get("username") or "").lower()
    if uid:
        p = db.scalars(select(Patient).where(Patient.telegram_chat_id == uid)).first()
        if p:
            return p
    if un:
        for p in db.scalars(select(Patient).where(Patient.telegram_username.is_not(None))):
            if (p.telegram_username or "").lower().lstrip("@") == un:
                return p
    if phone:
        return ap.find_patient(db, phone)
    return None


def _when(v) -> datetime:
    """Час повідомлення в UTC: unix-час або ISO-рядок (aiogram у режимі json)."""
    try:
        if isinstance(v, (int, float)):
            return datetime.utcfromtimestamp(v)
        if isinstance(v, str) and v:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d.replace(tzinfo=None) if d.tzinfo is None else datetime.utcfromtimestamp(d.timestamp())
    except (ValueError, OSError):
        pass
    return datetime.utcnow()


def ingest(db: Session, msg: dict) -> ChatMessage | None:
    """business_message → ChatMessage; нова розмова → звернення «Telegram»."""
    from . import meta_inbox as mi
    c = cfg(db)
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return None
    frm = msg.get("from") or {}
    thread = f"{PREFIX}{chat.get('id')}"
    mid = f"{chat.get('id')}:{msg.get('message_id')}"
    if db.scalar(select(ChatMessage.id).where(ChatMessage.mid == mid)):
        return None
    out = bool(c.get("user_id")) and frm.get("id") == c.get("user_id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    contact = msg.get("contact") or {}
    att = [k for k in ("photo", "video", "voice", "document", "audio", "sticker", "video_note", "contact", "location") if msg.get(k)]
    if not text and att:
        text = {"contact": f"Контакт: {contact.get('phone_number', '')}"}.get(att[0], "")
    person = chat if out else frm
    name = _who({"id": chat.get("id"), "username": chat.get("username"), "first_name": chat.get("first_name"), "last_name": chat.get("last_name")}) if out else _who(frm)
    prev = db.scalars(select(ChatMessage).where(ChatMessage.thread == thread).order_by(ChatMessage.id.desc())).first()
    pat_id = prev.patient_id if prev and prev.patient_id else None
    if not pat_id and not out:
        p = _patient(db, person, contact.get("phone_number"))
        pat_id = p.id if p else None
    appeal_id = prev.appeal_id if prev else None
    if not out:
        appeal_id = mi._appeal_for(db, "telegram", thread, pat_id, name, text) or appeal_id
        if contact.get("phone_number") and appeal_id:
            from ..models import Appeal
            a = db.get(Appeal, appeal_id)
            if a and not a.phone:
                a.phone = contact["phone_number"][:64]
    m = ChatMessage(channel="telegram", thread=thread, direction="out" if out else "in", text=text[:4000] or None,
                    attachments=att or None, mid=mid, name=name, patient_id=pat_id, appeal_id=appeal_id,
                    status="sent" if out else None, at=_when(msg.get("date")))
    db.add(m)
    db.commit()
    if not out:
        try:                                            # клік з реклами по Telegram → кампанія для цього звернення
            from . import site_leads as sl
            sl.match_clicks(db, date.today(), date.today())
        except Exception as e:      # noqa: BLE001
            log.warning("match_clicks: %s", e)
    return m


def send(db: Session, thread: str, text: str, http: httpx.Client | None = None) -> tuple[bool, str | None, str | None]:
    """Відповідь з CRM від імені акаунта клініки. → (ok, message_id, помилка)."""
    from ..config import settings
    c = cfg(db)
    if not (c.get("connection_id") and c.get("enabled")):
        return False, None, "Telegram Business не підключено (Налаштування → Канали)"
    if not c.get("can_reply"):
        return False, None, "У правах бота в Telegram вимкнено «Відповідати на повідомлення»"
    try:
        js = (http or httpx.Client(timeout=20)).post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                                                     data={"chat_id": thread.removeprefix(PREFIX), "text": text[:4000],
                                                           "business_connection_id": c["connection_id"]}).json()
    except Exception as e:      # noqa: BLE001
        return False, None, str(e)[:200]
    if not js.get("ok"):
        return False, None, (js.get("description") or "помилка Telegram")[:300]
    r = js.get("result") or {}
    return True, f"{r.get('chat', {}).get('id')}:{r.get('message_id')}", None
