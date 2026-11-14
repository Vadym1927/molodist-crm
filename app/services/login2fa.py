"""Двоетапний вхід у CRM: після пароля — 6-значний код у SMS (TurboSMS) на телефон співробітника.
Якщо SMS не вдалося — той самий код у Telegram (якщо в «Мій кабінет» указано Telegram).

Ролі, для яких вхід із кодом обовʼязковий, і «запамʼятати пристрій» — у «Налаштування → Вхід із кодом» (SyncState login_2fa).
Співробітник без телефону й Telegram входить лише за паролем (щоб не заблокувати) — сторінка налаштувань показує таких.
Код зберігається в сесії лише як HMAC (з секретом сервера), діє 5 хв, до 5 спроб і до 5 надсилань."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..models import SyncState, User

KEY = "login_2fa"
TTL = 300
MAX_TRIES = 5
MAX_SENDS = 5
RESEND_AFTER = 60
COOKIE = "mtd"


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        c = json.loads(st.value) if st and st.value else {}
    except ValueError:
        c = {}
    c.setdefault("roles", [])
    c.setdefault("remember_days", 30)
    c.setdefault("via", "telegram")
    return c


VIA = {"telegram": "Лише Telegram (ID у картці співробітника)", "sms": "Лише SMS (TurboSMS)", "sms_tg": "SMS, якщо не вдалося — Telegram"}


def save(db: Session, roles: list[str], remember_days: int, via: str = "telegram") -> None:
    db.merge(SyncState(key=KEY, value=json.dumps({"roles": roles, "remember_days": max(0, min(int(remember_days), 90)),
                                                  "via": via if via in VIA else "telegram"}), updated_at=datetime.utcnow()))
    db.commit()


def channels(u: User, via: str | None = None) -> list[str]:
    from ..integrations import messaging as m
    if via is None:
        from ..models import SessionLocal
        with SessionLocal() as db:
            via = cfg(db)["via"]
    out = []
    if via in ("sms", "sms_tg") and u.phone and m.normalize_phone(u.phone) and m.turbosms_available() and settings.turbosms_sender_sms:
        out.append("sms")
    if via in ("telegram", "sms_tg") and u.telegram_chat_id and settings.telegram_bot_token:
        out.append("telegram")
    return out


def required(db: Session, u: User) -> bool:
    c = cfg(db)
    return u.role in c["roles"] and bool(channels(u, c["via"]))


def _sig(*parts) -> str:
    return hmac.new(settings.secret_key.encode(), ":".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()


def code_hash(uid: int, code: str, exp: int) -> str:
    return _sig("code", uid, code, exp)


def mask(phone: str | None) -> str:
    d = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"+{d[:5]}•••{d[-2:]}" if len(d) >= 9 else "ваш телефон"


def send(u: User) -> tuple[str | None, str]:
    """→ (hash, канал | помилка). Код генерується тут і нікуди, крім SMS/Telegram, не потрапляє."""
    code = f"{secrets.randbelow(10 ** 6):06d}"
    exp = int(time.time()) + TTL
    text = f"MOLODIST CRM: код входу {code}. Діє 5 хв. Нікому не повідомляйте."
    errs = []
    for ch in channels(u):
        if ch == "sms":
            from ..integrations import messaging as m
            r = m.send_turbosms(u.phone, text, ["sms"], internal=True)
            if r.ok:
                return f"{code_hash(u.id, code, exp)}|{exp}", f"SMS на {mask(u.phone)}"
            errs.append(f"SMS: {r.error}")
        if ch == "telegram":
            from . import alerts
            if alerts._api("sendMessage", data={"chat_id": u.telegram_chat_id, "text": f"🔐 {text}"}):
                return f"{code_hash(u.id, code, exp)}|{exp}", "Telegram"
            errs.append("Telegram: не надіслано")
    return None, "; ".join(errs) or "немає телефону чи Telegram"


def check(uid: int, stored: str, code: str) -> bool:
    try:
        h, exp = stored.rsplit("|", 1)
        exp_i = int(exp)
    except (ValueError, AttributeError):
        return False
    if time.time() > exp_i:
        return False
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    return len(code) == 6 and hmac.compare_digest(h, code_hash(uid, code, exp_i))


def device_token(u: User, days: int) -> str:
    exp = int(time.time()) + days * 86400
    return f"{u.id}.{exp}.{_sig('dev', u.id, exp, (u.password_hash or '')[-16:])}"


def device_ok(u: User, token: str | None) -> bool:
    """Запамʼятований пристрій: токен діє до терміну й лише з тим самим паролем (зміна пароля — знову код)."""
    try:
        uid, exp, sig = (token or "").split(".")
        if int(uid) != u.id or time.time() > int(exp):
            return False
        return hmac.compare_digest(sig, _sig("dev", u.id, int(exp), (u.password_hash or "")[-16:]))
    except ValueError:
        return False
