"""Канали комунікацій (як у CleverBox): SMS і Viber (TurboSMS), Telegram-бот пацієнтів, Instagram / Facebook (Meta).
Налаштування — SyncState channels_cfg (секрети зашифровані ключем SECRET_KEY, у форму не повертаються).
apply() накладає їх на settings процесу: порожнє поле — лишається значення з .env."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import SyncState

KEY = "channels_cfg"
SECRET_FIELDS = {"token", "webhook_secret", "password", "page_token"}
ENV0 = {k: getattr(settings, k) for k in ("turbosms_token", "turbosms_sender_sms", "turbosms_sender_viber", "turbosms_viber_ttl",
                                          "patient_bot_token", "patient_bot_username", "quiet_hours_start", "quiet_hours_end")}
TYPES = {
    "sms": {"label": "SMS", "icon": "💬", "service": "TurboSMS"},
    "viber": {"label": "Viber", "icon": "📳", "service": "TurboSMS"},
    "telegram": {"label": "Telegram", "icon": "✈️", "service": "Telegram Bot API"},
    "meta": {"label": "Instagram та Facebook", "icon": "∞", "service": "Meta (Facebook Login)"},
}
DEFAULT = [
    {"id": 1, "type": "sms", "name": "Sms", "on": True, "params": {"sender": "", "allowed_from": "08:00", "allowed_to": "23:30", "login": ""}},
    {"id": 2, "type": "viber", "name": "MOLODIST.CLINIC", "on": True, "params": {"sender": "", "ttl": 3600, "fallback_sms": True}},
    {"id": 3, "type": "telegram", "name": "MOLODIST", "on": True, "params": {"username": ""}},
    {"id": 4, "type": "meta", "name": "Instagram та Facebook", "on": False, "params": {"pages": [{"kind": "facebook", "name": "Молодість"},
                                                                                              {"kind": "instagram", "name": "_molodist"}]}},
]


def _cipher():
    from ..integrations.cleverbox import _token_cipher
    return _token_cipher()


def _raw(db: Session) -> list[dict]:
    st = db.get(SyncState, KEY)
    if st and st.value:
        try:
            return json.loads(st.value)
        except ValueError:
            pass
    d = copy.deepcopy(DEFAULT)
    d[0]["params"]["sender"] = ENV0["turbosms_sender_sms"] or ""
    d[1]["params"]["sender"] = ENV0["turbosms_sender_viber"] or ""
    d[1]["params"]["ttl"] = ENV0["turbosms_viber_ttl"] or 3600
    d[2]["params"]["username"] = ENV0["patient_bot_username"] or ""
    return d


def secret(ch: dict, name: str) -> str:
    v = (ch.get("secrets") or {}).get(name)
    if not v:
        return ""
    try:
        return _cipher().decrypt(v.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


def channels(db: Session) -> list[dict]:
    out = []
    for ch in _raw(db):
        c = copy.deepcopy(ch)
        c["has"] = {k: bool((ch.get("secrets") or {}).get(k)) for k in SECRET_FIELDS}
        if c["type"] in ("sms", "viber") and not c["has"].get("token") and ENV0["turbosms_token"]:
            c["env_token"] = True
        if c["type"] == "telegram" and not c["has"].get("token") and ENV0["patient_bot_token"]:
            c["env_token"] = True
        c.pop("secrets", None)
        out.append(c)
    return out


def get(db: Session, cid: int) -> dict | None:
    return next((c for c in _raw(db) if int(c["id"]) == int(cid)), None)


def save(db: Session, items: list[dict]) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(items, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()
    apply(db)


def update(db: Session, cid: int, name: str | None, on: bool, params: dict, secrets: dict) -> dict:
    items = _raw(db)
    ch = next((c for c in items if int(c["id"]) == int(cid)), None)
    if not ch:
        raise ValueError("Канал не знайдено")
    if name:
        ch["name"] = name[:80]
    ch["on"] = on
    ch.setdefault("params", {}).update(params)
    sec = ch.setdefault("secrets", {})
    for k, v in secrets.items():
        if k in SECRET_FIELDS and v:
            sec[k] = _cipher().encrypt(v.encode()).decode()
        elif k.startswith("clear_") and v:
            sec.pop(k[6:], None)
    save(db, items)
    return ch


def add(db: Session, typ: str, name: str) -> dict:
    items = _raw(db)
    nid = max([int(c["id"]) for c in items] + [0]) + 1
    base = next((copy.deepcopy(d) for d in DEFAULT if d["type"] == typ), {"type": typ, "params": {}})
    base.update(id=nid, name=name or TYPES.get(typ, {}).get("label", typ), on=False)
    items.append(base)
    save(db, items)
    return base


def remove(db: Session, cid: int) -> None:
    save(db, [c for c in _raw(db) if int(c["id"]) != int(cid)])


def _hhmm(v: str, default: int) -> int:
    try:
        return int(str(v).split(":")[0])
    except (ValueError, IndexError):
        return default


def apply(db: Session) -> None:
    """Перші ввімкнені канали кожного типу → settings (TurboSMS, бот пацієнтів, дозволений час).
    Поки канали жодного разу не зберігали — працює лише .env (нічого не змінюємо)."""
    st = db.get(SyncState, KEY)
    if not (st and st.value):
        return
    items = _raw(db)
    by = {}
    for c in items:
        by.setdefault(c["type"], c)
    sms, viber, tg = by.get("sms"), by.get("viber"), by.get("telegram")
    token = (secret(sms, "token") if sms else "") or (secret(viber, "token") if viber else "") or ENV0["turbosms_token"]
    settings.turbosms_token = token
    settings.turbosms_sender_sms = ((sms["params"].get("sender") or ENV0["turbosms_sender_sms"]) if sms and sms.get("on") else "")
    settings.turbosms_sender_viber = ((viber["params"].get("sender") or ENV0["turbosms_sender_viber"]) if viber and viber.get("on") else "")
    if viber:
        try:
            settings.turbosms_viber_ttl = int(viber["params"].get("ttl") or ENV0["turbosms_viber_ttl"] or 3600)
        except (TypeError, ValueError):
            settings.turbosms_viber_ttl = 3600
    if sms:
        settings.quiet_hours_end = _hhmm(sms["params"].get("allowed_from"), ENV0["quiet_hours_end"])      # з цієї години можна
        settings.quiet_hours_start = _hhmm(sms["params"].get("allowed_to"), ENV0["quiet_hours_start"])    # після — тиша
    if tg:
        settings.patient_bot_token = (secret(tg, "token") or ENV0["patient_bot_token"]) if tg.get("on") else ""
        settings.patient_bot_username = tg["params"].get("username") or ENV0["patient_bot_username"]


def test(db: Session, cid: int, http: httpx.Client | None = None) -> tuple[bool, str]:
    ch = get(db, cid)
    if not ch:
        return False, "Канал не знайдено"
    http = http or httpx.Client(timeout=20)
    t = ch["type"]
    try:
        if t in ("sms", "viber"):
            tok = secret(ch, "token") or ENV0["turbosms_token"]
            if not tok:
                return False, "Не вказано API-токен TurboSMS"
            r = http.post("https://api.turbosms.ua/user/balance.json", json={}, headers={"Authorization": f"Bearer {tok}"})
            js = r.json()
            bal = (js.get("response_result") or {}).get("balance")
            if bal is None:
                return False, f"TurboSMS: {js.get('response_status') or r.status_code}"
            return True, f"Підключено · баланс {float(bal):.2f} ₴ · відправник «{ch['params'].get('sender') or '—'}»"
        if t == "telegram":
            tok = secret(ch, "token") or ENV0["patient_bot_token"]
            if not tok:
                return False, "Не вказано токен бота"
            js = http.get(f"https://api.telegram.org/bot{tok}/getMe").json()
            if not js.get("ok"):
                return False, f"Telegram: {js.get('description', 'помилка')}"
            return True, f"Підключено · @{js['result'].get('username')}"
        if t == "meta":
            tok = secret(ch, "page_token")
            if not tok:
                return False, "Немає токена сторінки Meta — підключення через Facebook ще не налаштовано в CRM"
            js = http.get("https://graph.facebook.com/v19.0/me", params={"access_token": tok, "fields": "id,name"}).json()
            if js.get("error"):
                return False, f"Meta: {js['error'].get('message', 'помилка')}"
            return True, f"Підключено · {js.get('name')}"
    except Exception as e:  # noqa: BLE001
        return False, f"Немає звʼязку: {str(e)[:150]}"
    return False, "Невідомий тип каналу"


def verify_dlr(db: Session, body: bytes, signature: str | None) -> bool:
    """Підпис вебхука TurboSMS (SHA-1 HMAC секретним ключем), якщо ключ задано."""
    sms = next((c for c in _raw(db) if c["type"] == "sms"), None)
    key = secret(sms, "webhook_secret") if sms else ""
    if not key:
        return True
    calc = hmac.new(key.encode(), body, hashlib.sha1).hexdigest()
    return bool(signature) and hmac.compare_digest(calc, signature.strip().lower())
