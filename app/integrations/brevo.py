"""Email через Brevo (Франція, транзакційні листи; безкоштовно до 300 листів на день).

Ключ API зберігається в SyncState лише зашифрованим (SECRET_KEY), у відповідях і логах не показується.
Відправник — адреса домену molodist.com.ua, підтвердженого в Brevo (DNS-записи DKIM / SPF / DMARC).
"""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from ..models import SyncState

log = logging.getLogger(__name__)
KEY = "email_brevo"
API = "https://api.brevo.com/v3"
DEFAULT = {"on": False, "sender": "info@molodist.com.ua", "sender_name": "MOLODIST", "reply_to": "info@molodist.com.ua",
           "cart_on": False, "cart_subject": "Ваш кошик у MOLODIST чекає на вас", "test_to": "", "last": ""}


class EmailError(RuntimeError):
    pass


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    return {**DEFAULT, **d}


def save(db: Session, c: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(c, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def set_key(db: Session, key: str) -> None:
    from .cleverbox import _token_cipher
    c = cfg(db)
    c["key_enc"] = _token_cipher().encrypt(key.strip().encode()).decode() if key.strip() else ""
    save(db, c)


def _key(c: dict) -> str:
    from .cleverbox import _token_cipher
    try:
        return _token_cipher().decrypt(c["key_enc"].encode()).decode() if c.get("key_enc") else ""
    except Exception:  # noqa: BLE001
        return ""


def has_key(c: dict) -> bool:
    return bool(c.get("key_enc"))


def ready(db: Session) -> bool:
    c = cfg(db)
    return bool(c.get("on") and has_key(c) and c.get("sender"))


def send(db: Session, to: str, subject: str, text: str, html_body: str | None = None, name: str = "", force: bool = False) -> str:
    """Надіслати лист. force — для тестового листа, коли надсилання ще вимкнене. Повертає messageId."""
    c = cfg(db)
    k = _key(c)
    if not k:
        raise EmailError("Не вказано ключ API Brevo.")
    if not (c.get("on") or force):
        raise EmailError("Надсилання email вимкнене в налаштуваннях.")
    to = (to or "").strip()
    if "@" not in to:
        raise EmailError("Немає адреси отримувача.")
    body = {"sender": {"name": c.get("sender_name") or "MOLODIST", "email": c["sender"]}, "to": [{"email": to, **({"name": name} if name else {})}],
            "subject": subject[:200], "textContent": text, "htmlContent": html_body or wrap(text)}
    if c.get("reply_to"):
        body["replyTo"] = {"email": c["reply_to"]}
    try:
        r = httpx.post(f"{API}/smtp/email", json=body, headers={"api-key": k, "accept": "application/json"}, timeout=20)
    except httpx.HTTPError as e:
        raise EmailError(f"Brevo недоступний: {e.__class__.__name__}") from e
    if r.status_code >= 300:
        try:
            msg = r.json().get("message") or r.text
        except ValueError:
            msg = r.text
        raise EmailError(f"Brevo відповів {r.status_code}: {str(msg)[:200]}")
    mid = (r.json() or {}).get("messageId", "")
    c = cfg(db)
    c["last"] = f"{datetime.now():%d.%m %H:%M} → {to}: OK"
    save(db, c)
    return mid


def account(db: Session) -> dict:
    """Перевірка ключа: назва акаунта й план (без секретів)."""
    k = _key(cfg(db))
    if not k:
        raise EmailError("Не вказано ключ API Brevo.")
    try:
        r = httpx.get(f"{API}/account", headers={"api-key": k, "accept": "application/json"}, timeout=15)
    except httpx.HTTPError as e:
        raise EmailError(f"Brevo недоступний: {e.__class__.__name__}") from e
    if r.status_code >= 300:
        raise EmailError(f"Ключ не прийнято ({r.status_code}).")
    j = r.json() or {}
    plan = ", ".join(f"{p.get('type')}: {p.get('credits')}" for p in j.get("plan") or [])
    return {"company": j.get("companyName") or "", "email": j.get("email") or "", "plan": plan}


def wrap(text: str, button: tuple[str, str] | None = None) -> str:
    """Простий фірмовий лист: MOLODIST (Belleza-стиль), текст, кнопка."""
    paras = "".join(f'<p style="margin:0 0 14px">{html.escape(p).replace(chr(10), "<br>")}</p>' for p in text.split("\n\n") if p.strip())
    btn = ""
    if button:
        btn = (f'<p style="margin:22px 0"><a href="{html.escape(button[1])}" style="background:#5C4B82;color:#fff;text-decoration:none;'
               f'padding:12px 22px;border-radius:10px;font-weight:600;display:inline-block">{html.escape(button[0])}</a></p>')
    return ('<div style="background:#F7F4F0;padding:28px 12px;font-family:Open Sans,Arial,sans-serif;color:#2A2430">'
            '<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:16px;padding:28px 26px">'
            '<div style="font:400 22px Georgia,serif;letter-spacing:.16em;text-align:center;margin-bottom:4px">MOLODIST</div>'
            '<div style="font-size:11px;letter-spacing:.2em;text-align:center;color:#8a8494;margin-bottom:22px">МЕДИЧНИЙ ПРОСТІР</div>'
            f'{paras}{btn}'
            '<p style="margin:22px 0 0;font-size:12px;color:#8a8494">MOLODIST · Київ, вул. Князя Романа Мстиславича, 28А · molodist.store</p>'
            '</div></div>')
