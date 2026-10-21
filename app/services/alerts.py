"""Технічні сповіщення власнику в Telegram: збої синку, бекапу, фонових задач — і повідомлення про відновлення.

Кому: TELEGRAM_ALERT_CHAT_ID, або всі власники, що вказали свій Telegram chat ID у «Мій кабінет».
Без спаму: однакова помилка того самого типу повторюється не частіше ніж раз на ALERT_REPEAT_HOURS;
коли все знову працює — одне повідомлення «✅ відновлено».
Відправка напряму через Bot API (httpx) — працює з будь-якого потоку, без event loop.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import httpx
from sqlalchemy import select

from ..config import settings
from ..models import SessionLocal, SyncState, User

log = logging.getLogger("alerts")
TITLES = {"sync": "Синхронізація з CleverBox", "backup": "Резервна копія", "backup_tg": "Копія в Telegram",
          "job": "Фонова задача", "digest": "Нагадування", "sync_part": "Синк: окремі модулі CleverBox",
          "security": "Безпека"}


def recipients() -> list[str]:
    if settings.telegram_alert_chat_id:
        return [c.strip() for c in settings.telegram_alert_chat_id.split(",") if c.strip()]
    with SessionLocal() as db:
        return [u.telegram_chat_id for u in db.scalars(select(User).where(
            User.role == "owner", User.active.is_(True), User.telegram_chat_id.is_not(None)))]


def _api(method: str, **kw) -> bool:
    if not settings.telegram_bot_token:
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}", timeout=120, **kw)
        if r.status_code != 200:
            log.warning("telegram %s → %s %s", method, r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:      # noqa: BLE001
        log.warning("telegram %s failed: %s", method, e)
        return False


def send(text: str) -> int:
    sent = 0
    for chat in recipients():
        sent += _api("sendMessage", data={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                          "disable_web_page_preview": "true"})
    if not sent:
        log.warning("Сповіщення не надіслано (немає бота або chat ID власника): %s", text[:200])
    return sent


def send_document(path: str | Path, caption: str, chat: str | None = None) -> bool:
    chats = [chat] if chat else recipients()
    ok = False
    for c in chats:
        with open(path, "rb") as f:
            ok |= _api("sendDocument", data={"chat_id": c, "caption": caption[:1000], "parse_mode": "HTML"},
                       files={"document": (Path(path).name, f)})
    return ok


def _get(key: str) -> dict:
    with SessionLocal() as db:
        st = db.get(SyncState, f"alert:{key}")
        return json.loads(st.value) if st and st.value else {}


def _set(key: str, val: dict):
    with SessionLocal() as db:
        db.merge(SyncState(key=f"alert:{key}", value=json.dumps(val, ensure_ascii=False), updated_at=datetime.utcnow()))
        db.commit()


def failure(kind: str, error: str, hint: str | None = None):
    """Повідомити про збій (з дедуплікацією)."""
    prev = _get(kind)
    now = datetime.now()
    msg = str(error)[:500]
    last = datetime.fromisoformat(prev["at"]) if prev.get("at") else None
    if prev.get("failing") and prev.get("msg") == msg and last and now - last < timedelta(hours=settings.alert_repeat_hours):
        return False
    text = (f"🔴 <b>{TITLES.get(kind, kind)}: помилка</b>\n{escape(msg)}"
            + (f"\n\n{escape(hint)}" if hint else "")
            + f"\n\n<a href=\"{settings.public_url.rstrip('/')}/\">Відкрити CRM</a>")
    send(text)
    _set(kind, {"failing": True, "msg": msg, "at": now.isoformat(timespec="seconds"),
                "since": prev.get("since") if prev.get("failing") else now.isoformat(timespec="seconds")})
    return True


def success(kind: str, note: str | None = None):
    """Скинути стан помилки; якщо раніше був збій — повідомити про відновлення."""
    prev = _get(kind)
    if prev.get("failing"):
        since = prev.get("since", "")[:16].replace("T", " ")
        send(f"✅ <b>{TITLES.get(kind, kind)}: відновлено</b>" + (f" (збій з {since})" if since else "")
             + (f"\n{escape(note)}" if note else ""))
        _set(kind, {"failing": False, "at": datetime.now().isoformat(timespec="seconds")})


HINTS = {
    "403": "Акаунт CleverBox вимкнено або порожній токен — перевірте CLEVERBOX_TOKEN.",
    "токен не валідний": "Токен CleverBox недійсний — згенеруйте новий у налаштуваннях REST API і оновіть .env.",
    "IP сервера": "IP VPS не дозволена в налаштуваннях REST API CleverBox — додайте її там.",
    "timed out": "CleverBox не відповідає — найімовірніше, тимчасово. Наступна спроба за розкладом.",
    "Name or service not known": "Немає зʼєднання з cbox.mobi — перевірте мережу/DNS сервера.",
}


def hint_for(error: str) -> str | None:
    for k, v in HINTS.items():
        if k.lower() in str(error).lower():
            return v
    return None
