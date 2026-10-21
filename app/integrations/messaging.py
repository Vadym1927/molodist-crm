"""Канали повідомлень пацієнтам.

• telegram — через ВАШ бот для пацієнтів (OpenClaw): CRM шле повідомлення його токеном (PATIENT_BOT_TOKEN).
  Надсилання не заважає OpenClaw отримувати оновлення. Пацієнт має бути привʼязаний (бот передає chat_id
  у CRM через API /api/v1/bot/link) і не заблокувати бота.
• viber / sms — TurboSMS (https://turbosms.ua/api.html): гібрид — спершу Viber, якщо не доставлено за
  TURBOSMS_VIBER_TTL — SMS. Відповідь з message_id, статуси через message/status.json.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..config import settings

log = logging.getLogger("messaging")


@dataclass
class SendResult:
    ok: bool
    channel: str
    message_id: str | None = None
    error: str | None = None
    blocked: bool = False            # пацієнт заблокував бота → відвʼязати


def normalize_phone(phone: str | None) -> str | None:
    """→ 380XXXXXXXXX або None. Приймає 067…, +38 067…, 38067…, кілька номерів через кому (береться перший)."""
    if not phone:
        return None
    d = re.sub(r"\D", "", str(phone).split(",")[0])
    if len(d) == 10 and d.startswith("0"):
        d = "38" + d
    elif len(d) == 9:
        d = "380" + d
    elif len(d) == 11 and d.startswith("80"):
        d = "3" + d
    return d if len(d) == 12 and d.startswith("380") else None


def is_quiet_time(now: datetime | None = None) -> bool:
    h = (now or datetime.now()).hour
    s, e = settings.quiet_hours_start, settings.quiet_hours_end
    return (h >= s or h < e) if s > e else (s <= h < e)


def sms_parts(text: str) -> int:
    """Кількість SMS-сегментів (кирилиця: 70 / 67 на сегмент у склеєних)."""
    cyr = any(ord(ch) > 127 for ch in text)
    single, multi = (70, 67) if cyr else (160, 153)
    n = len(text)
    return 1 if n <= single else -(-n // multi)


# ---------------- Telegram (бот пацієнтів) ----------------

def telegram_available() -> bool:
    return bool(settings.patient_bot_token)


def send_telegram(chat_id: str, text: str) -> SendResult:
    if not settings.patient_bot_token:
        return SendResult(False, "telegram", error="PATIENT_BOT_TOKEN не задано")
    try:
        r = httpx.post(f"https://api.telegram.org/bot{settings.patient_bot_token}/sendMessage", timeout=30,
                       data={"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": "true"})
        js = r.json()
    except Exception as e:      # noqa: BLE001
        return SendResult(False, "telegram", error=str(e)[:300])
    if js.get("ok"):
        return SendResult(True, "telegram", message_id=str(js["result"]["message_id"]))
    desc = js.get("description", "")
    return SendResult(False, "telegram", error=desc[:300],
                      blocked=r.status_code == 403 or "blocked" in desc or "deactivated" in desc)


# ---------------- TurboSMS ----------------

TS_URL = "https://api.turbosms.ua"


def turbosms_available() -> bool:
    return bool(settings.turbosms_token and (settings.turbosms_sender_sms or settings.turbosms_sender_viber))


def _ts(method: str, body: dict, timeout: float | None = None) -> dict:
    r = httpx.post(f"{TS_URL}/{method}", json=body, timeout=timeout,
                   headers={"Authorization": f"Bearer {settings.turbosms_token}"})
    return r.json()


def send_turbosms(phone: str, text: str, channels: list[str], sequence_id: str | None = None) -> SendResult:
    """channels ⊂ {viber, sms}; обидва → гібрид Viber→SMS."""
    to = normalize_phone(phone)
    if not to:
        return SendResult(False, "sms", error="некоректний номер")
    body: dict = {"recipients": [to], "sequence_id": sequence_id or uuid.uuid4().hex}
    use_viber = "viber" in channels and settings.turbosms_sender_viber
    use_sms = "sms" in channels and settings.turbosms_sender_sms
    if not (use_viber or use_sms):
        return SendResult(False, "sms", error="TurboSMS: не задано відправника для обраних каналів")
    if use_viber:
        body["viber"] = {"sender": settings.turbosms_sender_viber, "text": text[:1000], "ttl": settings.turbosms_viber_ttl}
    if use_sms:
        body["sms"] = {"sender": settings.turbosms_sender_sms, "text": text}
        if use_viber:
            body["sms"]["hybrid_ttl"] = settings.turbosms_viber_ttl + 3600
    channel = "viber" if use_viber else "sms"
    try:
        # без таймауту — так радить TurboSMS (повтор після таймауту = дубль); від дублів захищає sequence_id
        js = _ts("message/send.json", body, timeout=None)
    except Exception as e:      # noqa: BLE001
        return SendResult(False, channel, error=str(e)[:300])
    res = js.get("response_result") or []
    item = res[0] if isinstance(res, list) and res else (res if isinstance(res, dict) else {})
    if item.get("message_id"):
        return SendResult(True, channel, message_id=item["message_id"])
    return SendResult(False, channel, error=f"{item.get('response_status') or js.get('response_status')}"[:300])


def turbosms_status(message_ids: list[str]) -> dict[str, dict]:
    if not message_ids:
        return {}
    js = _ts("message/status.json", {"messages": message_ids[:500]}, timeout=60)
    return {x["message_id"]: x for x in (js.get("response_result") or []) if x.get("message_id")}


def turbosms_balance() -> float | None:
    try:
        js = _ts("user/balance.json", {}, timeout=20)
        return float((js.get("response_result") or {}).get("balance"))
    except Exception:           # noqa: BLE001
        return None


STATUS_MAP = {"delivered": "delivered", "read": "read", "undelivered": "failed", "expired": "failed",
              "rejected": "failed", "failed": "failed", "queued": "sent", "accepted": "sent", "sent": "sent"}


# ---------------- вибір каналу ----------------

def available_channels(patient) -> list[str]:
    out = []
    if patient.telegram_chat_id and telegram_available():
        out.append("telegram")
    if normalize_phone(patient.phone) and turbosms_available():
        if settings.turbosms_sender_viber:
            out.append("viber")
        if settings.turbosms_sender_sms:
            out.append("sms")
    return out


def send_to_patient(patient, text: str, prefer: list[str] | None = None, marketing: bool = False,
                    sequence_id: str | None = None) -> SendResult:
    """Перший доступний канал з prefer (за замовчуванням telegram → viber → sms).
    Viber і SMS, якщо обидва дозволені, ідуть одним гібридним повідомленням."""
    prefer = prefer or ["telegram", "viber", "sms"]
    avail = available_channels(patient)
    for ch in prefer:
        if ch not in avail:
            continue
        if ch == "telegram":
            res = send_telegram(patient.telegram_chat_id, text)
            if res.ok or not (set(prefer) & {"viber", "sms"} & set(avail)):
                return res
            if res.blocked:
                continue                     # бот заблоковано — пробуємо Viber/SMS
            return res
        ts_channels = [c for c in ("viber", "sms") if c in prefer and c in avail]
        footer = settings.marketing_footer if marketing and settings.marketing_footer else ""
        return send_turbosms(patient.phone, f"{text}\n{footer}".strip(), ts_channels, sequence_id)
    return SendResult(False, prefer[0] if prefer else "none", error="немає доступного каналу (Telegram/телефон)")
