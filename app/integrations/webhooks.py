"""Вихідні вебхуки: CRM сама надсилає події в n8n / інші системи.

POST <url> з JSON {"event": "...", "sent_at": "...", "data": {...}}
Підпис: заголовок X-CRM-Signature = hex(HMAC-SHA256(secret, тіло запиту)) — перевіряйте на своєму боці.
Події: plan.created, plan.updated, reminders.daily, sync.done
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime

import httpx
from sqlalchemy import select

from ..models import SessionLocal, Webhook

log = logging.getLogger("webhooks")
EVENTS = {"plan.created": "Новий бюті-план", "plan.updated": "План змінено",
          "reminders.daily": "Щоденні нагадування", "sync.done": "Синхронізацію з CleverBox завершено"}


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _deliver(hook_id: int, url: str, secret: str, body: bytes):
    try:
        r = httpx.post(url, content=body, timeout=10, headers={
            "Content-Type": "application/json", "X-CRM-Signature": sign(secret, body)})
        status = f"{datetime.now():%d.%m %H:%M} → {r.status_code}"
    except Exception as e:      # noqa: BLE001
        status = f"{datetime.now():%d.%m %H:%M} помилка: {e}"[:200]
    with SessionLocal() as db:
        h = db.get(Webhook, hook_id)
        if h:
            h.last_status = status
            db.commit()


def fire(event: str, data: dict, sync: bool = False):
    """Не блокує запит: доставка у фоновому потоці."""
    with SessionLocal() as db:
        hooks = [(h.id, h.url, h.secret) for h in db.scalars(select(Webhook).where(Webhook.active.is_(True)))
                 if event in (h.events or [])]
    if not hooks:
        return
    body = json.dumps({"event": event, "sent_at": datetime.now().isoformat(timespec="seconds"), "data": data},
                      ensure_ascii=False, default=str).encode()
    for hid, url, secret in hooks:
        if sync:
            _deliver(hid, url, secret, body)
        else:
            threading.Thread(target=_deliver, args=(hid, url, secret, body), daemon=True).start()
