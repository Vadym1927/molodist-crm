"""Viber-бот клініки (Viber Bot API) → центр спілкування CRM, як Instagram.

• Канал «Viber-бот» у Налаштування → Канали: токен бота (Viber Admin Panel → бот → Token).
• «Зберегти й перевірити» → get_account_info + set_webhook на /api/public/viber/webhook/<hook>.
• Вхідне від людини → ChatMessage (канал viber, діалог vb:<id>) + звернення «Viber»; контакт із номером → телефон звернення й картка пацієнта.
• Відповідь з CRM — send_message від імені бота. Підпис кожного виклику — X-Viber-Content-Signature (HMAC-SHA256 тіла токеном бота).
Особистий Viber на номері клініки сюди не потрапляє — у нього немає API; на сайті кнопка Viber має вести на бота (viber://pa?chatURI=…).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets as _sec
from datetime import date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ChatMessage
from . import channels as ch

log = logging.getLogger("viber_bot")
API = "https://chatapi.viber.com/pa"
PREFIX = "vb:"


def channel(db: Session) -> dict | None:
    return next((c for c in ch._raw(db) if c["type"] == "viber_bot"), None)


def token(db: Session) -> str:
    c = channel(db)
    return (ch.secret(c, "token") or "").strip() if c else ""


def hook_id(db: Session) -> str:
    c = channel(db)
    if not c:
        return ""
    h = (c.get("params") or {}).get("hook")
    if not h:
        h = _sec.token_urlsafe(18)
        ch.update(db, c["id"], None, bool(c.get("on")), {"hook": h}, {})
    return h


def _post(db: Session, path: str, body: dict, http: httpx.Client | None = None) -> dict:
    try:
        return (http or httpx.Client(timeout=20)).post(f"{API}/{path}", json=body, headers={"X-Viber-Auth-Token": token(db)}).json()
    except Exception as e:      # noqa: BLE001
        return {"status": -1, "status_message": str(e)[:200]}


def connect(db: Session, base_url: str, http: httpx.Client | None = None) -> tuple[bool, str]:
    """Перевірити бота й встановити вебхук CRM."""
    if not token(db):
        return False, "Не вказано токен Viber-бота"
    info = _post(db, "get_account_info", {}, http)
    if info.get("status") != 0:
        return False, f"Viber: {info.get('status_message') or 'помилка'} — бот неактивний або токен невірний"
    url = f"{base_url}/api/public/viber/webhook/{hook_id(db)}"
    r = _post(db, "set_webhook", {"url": url, "event_types": ["delivered", "seen", "failed", "subscribed", "unsubscribed", "conversation_started"],
                                  "send_name": True, "send_photo": True}, http)
    c = channel(db)
    ch.update(db, c["id"], None, True, {"bot_name": info.get("name"), "uri": info.get("uri"), "subscribers": info.get("subscribers_count"),
                                         "webhook_ok": r.get("status") == 0, "webhook_at": datetime.now().isoformat(timespec="minutes")}, {})
    if r.get("status") != 0:
        return False, f"Бот «{info.get('name')}» знайдено, але вебхук не встановлено: {r.get('status_message')}"
    return True, f"Підключено · «{info.get('name')}» · {info.get('subscribers_count') or 0} підписників · вебхук CRM встановлено"


def check_signature(db: Session, body: bytes, sig: str | None) -> bool:
    tok = token(db)
    if not tok or not sig:
        return False
    return hmac.compare_digest(hmac.new(tok.encode(), body, hashlib.sha256).hexdigest(), sig.strip())


def ingest(db: Session, ev: dict) -> ChatMessage | None:
    """Подія вебхука Viber → ChatMessage; нова розмова → звернення «Viber»."""
    from . import meta_inbox as mi, appeals as ap
    if ev.get("event") != "message":
        return None
    snd = ev.get("sender") or {}
    msg = ev.get("message") or {}
    sid = str(snd.get("id") or "")
    if not sid:
        return None
    thread = f"{PREFIX}{sid}"
    mid = f"vb:{ev.get('message_token')}"
    if db.scalar(select(ChatMessage.id).where(ChatMessage.mid == mid)):
        return None
    typ = msg.get("type") or "text"
    text = (msg.get("text") or "").strip()
    contact = msg.get("contact") or {}
    phone = contact.get("phone_number")
    if typ == "contact" and phone:
        text = f"Контакт: {contact.get('name') or ''} {phone}".strip()
    atts = [{"type": typ, "url": msg.get("media")}] if msg.get("media") else None
    name = (snd.get("name") or "").strip() or f"Viber …{sid[-4:]}"
    prev = db.scalars(select(ChatMessage).where(ChatMessage.thread == thread).order_by(ChatMessage.id.desc())).first()
    pat_id = prev.patient_id if prev and prev.patient_id else None
    if not pat_id and phone:
        p = ap.find_patient(db, phone)
        pat_id = p.id if p else None
    appeal_id = mi._appeal_for(db, "viber", thread, pat_id, name, text or f"[{typ}]")
    if phone and appeal_id:
        from ..models import Appeal
        a = db.get(Appeal, appeal_id)
        if a and not a.phone:
            a.phone = phone[:64]
        if a and pat_id and not a.patient_id:
            a.patient_id = pat_id
    ts = ev.get("timestamp")
    m = ChatMessage(at=datetime.utcfromtimestamp(ts / 1000) if isinstance(ts, (int, float)) else datetime.utcnow(), channel="viber",
                    thread=thread, direction="in", text=text[:4000] or (None if atts else f"[{typ}]"), attachments=atts, mid=mid,
                    name=name, patient_id=pat_id, appeal_id=appeal_id)
    db.add(m)
    if pat_id and prev:
        for x in db.scalars(select(ChatMessage).where(ChatMessage.thread == thread, ChatMessage.patient_id.is_(None))):
            x.patient_id = pat_id
    db.commit()
    try:                                                # клік з реклами по Viber на сайті → кампанія для звернення
        from . import site_leads as sl
        sl.match_clicks(db, date.today(), date.today())
    except Exception as e:      # noqa: BLE001
        log.warning("match_clicks: %s", e)
    return m


def send(db: Session, thread: str, text: str, http: httpx.Client | None = None) -> tuple[bool, str | None, str | None]:
    c = channel(db)
    if not token(db):
        return False, None, "Viber-бот не підключено (Налаштування → Канали)"
    js = _post(db, "send_message", {"receiver": thread.removeprefix(PREFIX), "type": "text", "text": text[:7000],
                                    "sender": {"name": ((c or {}).get("params") or {}).get("bot_name") or "MOLODIST"}}, http)
    if js.get("status") != 0:
        return False, None, f"Viber: {js.get('status_message') or 'помилка'}"[:300]
    return True, f"vb:{js.get('message_token')}", None


def link(db: Session) -> str:
    c = channel(db)
    uri = ((c or {}).get("params") or {}).get("uri")
    return f"viber://pa?chatURI={uri}" if uri else ""


def log_event(db: Session, kind: str, note: str = "") -> None:
    c = channel(db)
    if not c:
        return
    lg = ([{"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, "note": note[:160]}] + ((c.get("params") or {}).get("log") or []))[:15]
    ch.update(db, c["id"], None, bool(c.get("on")), {"log": lg}, {})


def parse(body: bytes) -> dict:
    try:
        d = json.loads(body or b"{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}
