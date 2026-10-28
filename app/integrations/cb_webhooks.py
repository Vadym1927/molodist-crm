"""Вебхуки CleverBox → CRM: записи і нові клієнти зʼявляються за хвилину, а не раз на 2 години.

CleverBox шле події visit_create (новий/перенесений запис), visit_status (зміна статусу), client_create.
Адреса прийому: {PUBLIC_URL}/hooks/cleverbox/{секрет}. Секрет генерується один раз і зберігається в БД —
без нього запит відхиляється. Формат тіла події CleverBox не задокументований публічно, тому розбір
терпимий: шукаємо client_id / visit_id / event будь-де в JSON, а дані беремо повторним запитом до API.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from datetime import datetime, date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import SessionLocal, SyncState, Patient

log = logging.getLogger("cb_webhooks")

EVENTS = ("visit_create", "visit_status", "client_create")
SYNC_LOCK = threading.Lock()          # спільний з повним синком: не ганяємо два синки CleverBox разом
_pending = {"due": 0.0, "clients": set(), "timer": None}
_plock = threading.Lock()
DEBOUNCE_SEC = 20                     # кілька подій поспіль → один швидкий синк


def _get(db: Session, key: str) -> str | None:
    st = db.get(SyncState, key)
    return st.value if st else None


def _set(db: Session, key: str, value: str) -> None:
    st = db.get(SyncState, key) or SyncState(key=key)
    st.value, st.updated_at = value, datetime.utcnow()
    db.merge(st)


def secret(db: Session) -> str:
    s = _get(db, "cb_webhook_secret")
    if not s:
        s = secrets.token_urlsafe(24)
        _set(db, "cb_webhook_secret", s)
        db.commit()
    return s


def hook_url(db: Session) -> str:
    return f"{settings.public_url.rstrip('/')}/hooks/cleverbox/{secret(db)}"


def check_secret(db: Session, given: str) -> bool:
    s = _get(db, "cb_webhook_secret")
    return bool(s) and hmac.compare_digest(s, given or "")


# ---------------- розбір події ----------------

def _walk(obj, depth=0):
    if depth > 4:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:20]:
            yield from _walk(v, depth + 1)


def _int(v) -> int | None:
    try:
        i = int(str(v).strip())
        return i if i > 0 else None
    except (TypeError, ValueError):
        return None


def parse(payload) -> dict:
    """→ {event, client_id, visit_id, client: {name, phone}}"""
    out = {"event": None, "client_id": None, "visit_id": None, "client": {}}
    if not isinstance(payload, (dict, list)):
        return out
    for k, v in _walk(payload):
        kl = str(k).lower()
        if kl in ("event", "event_type", "type", "action", "hook") and isinstance(v, str) and not out["event"]:
            if any(e in v.lower() for e in EVENTS) or "visit" in v.lower() or "client" in v.lower():
                out["event"] = v.lower()
        elif kl == "client_id" and not out["client_id"]:
            out["client_id"] = _int(v)
        elif kl in ("visit_id", "record_id") and not out["visit_id"]:
            out["visit_id"] = _int(v)
        elif kl == "client" and isinstance(v, dict):
            out["client_id"] = out["client_id"] or _int(v.get("id"))
            out["client"] = {"name": v.get("name"), "phone": v.get("phone")}
    if isinstance(payload, dict):
        ev = out["event"] or ""
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if "client" in ev and not out["client_id"]:
            out["client_id"] = _int(data.get("id"))
            if not out["client"]:
                out["client"] = {"name": data.get("name"), "phone": data.get("phone")}
        if "visit" in ev and not out["visit_id"]:
            out["visit_id"] = _int(data.get("id"))
    return out


def _shape(obj, depth=0):
    """Структура JSON без значень (для діагностики — персональні дані не зберігаємо)."""
    if depth > 3:
        return "…"
    if isinstance(obj, dict):
        return {k: _shape(v, depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, list):
        return [_shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


# ---------------- обробка ----------------

def handle(payload) -> dict:
    ev = parse(payload)
    with SessionLocal() as db:
        _set(db, "cb_webhook_last", json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                                "event": ev["event"], "shape": _shape(payload)},
                                               ensure_ascii=False)[:4000])
        n = int(_get(db, "cb_webhook_count") or 0) + 1
        _set(db, "cb_webhook_count", str(n))
        cid = ev["client_id"]
        if cid and "client" in (ev["event"] or "") and ev["client"].get("name"):
            if not db.scalar(select(Patient).where(Patient.cleverbox_id == cid)):
                phone = (ev["client"].get("phone") or "").split(",")[0].strip() or None
                db.add(Patient(full_name=str(ev["client"]["name"]).strip()[:200], cleverbox_id=cid, phone=phone))
        db.commit()
    schedule(cid)
    return ev


def schedule(client_id: int | None = None) -> None:
    with _plock:
        if client_id:
            _pending["clients"].add(client_id)
        if _pending["timer"] is None:
            t = threading.Timer(DEBOUNCE_SEC, _run_pending)
            t.daemon = True
            _pending["timer"] = t
            t.start()


def _run_pending() -> None:
    with _plock:
        clients = set(_pending["clients"])
        _pending["clients"].clear()
        _pending["timer"] = None
    try:
        quick_sync(clients)
    except Exception:                      # noqa: BLE001
        log.exception("quick sync after webhook failed")


def quick_sync(client_ids: set[int]) -> dict:
    """Календар на 14 днів + надані послуги за 2 дні по клієнтах із подій."""
    if not settings.cleverbox_token:
        return {}
    from .cleverbox import CleverBox, sync_calendar, sync_done_services
    if not SYNC_LOCK.acquire(timeout=600):
        return {}
    try:
        cb = CleverBox()
        with SessionLocal() as db:
            rep = {"calendar": sync_calendar(db, cb, 14)}
            end = date.today()
            rep["done"] = sum(sync_done_services(db, cb, end - timedelta(days=2), end, client_id=c)
                              for c in list(client_ids)[:20])
            _set(db, "cb_webhook_last_sync", datetime.now().isoformat(timespec="seconds"))
            db.commit()
            from ..services.stock import auto_moves
            rep["stock"] = auto_moves(db)
        return rep
    finally:
        SYNC_LOCK.release()


# ---------------- підписка ----------------

def subscribe(db: Session) -> list[dict]:
    """Створює/оновлює підписки CleverBox на всі події. Повертає відповіді CleverBox (для діагностики)."""
    from .cleverbox import CleverBox, CleverBoxError
    cb = CleverBox()
    url = hook_url(db)
    out = []
    for ev in EVENTS:
        try:
            res = cb.call("webhooks/save", {"event": ev, "url": url, "active": 1})
            out.append({"event": ev, "ok": True, "response": _shape(res)})
        except (CleverBoxError, Exception) as e:     # noqa: BLE001
            out.append({"event": ev, "ok": False, "error": str(e)[:300]})
    _set(db, "cb_webhook_subscribe", json.dumps(out, ensure_ascii=False)[:4000])
    db.commit()
    return out


def subscriptions(db: Session) -> list | str:
    from .cleverbox import CleverBox
    try:
        res = CleverBox().call("webhooks/list")
    except Exception as e:                 # noqa: BLE001
        return f"помилка: {str(e)[:200]}"
    data = res.get("data") if isinstance(res, dict) else res
    url = hook_url(db)
    rows = []
    for w in (data or []) if isinstance(data, list) else []:
        if not isinstance(w, dict):
            continue
        u = str(w.get("url") or "")
        rows.append({"event": w.get("event") or w.get("type"), "ours": u == url,
                     "url": (u[:40] + "…") if u and u != url else ("ця CRM" if u == url else u),
                     "active": w.get("active", w.get("status"))})
    return rows


def status(db: Session) -> dict:
    def j(key):
        try:
            return json.loads(_get(db, key) or "null")
        except ValueError:
            return None
    return {"count": int(_get(db, "cb_webhook_count") or 0), "last": j("cb_webhook_last"),
            "subscribe": j("cb_webhook_subscribe"), "last_sync": _get(db, "cb_webhook_last_sync")}
