"""Рахунки постачальників на оплату → чернетка платежу в monobank на підпис.

Потік: рахунок (PDF / фото) у Telegram-групі «Рахунки» (або завантаження в CRM) → розпізнавання (Claude API:
продавець, IBAN, ЄДРПОУ, платник, №, дата, сума, ПДВ, призначення) → ФОП-платник за назвою платника в рахунку →
картка з кнопками «Погодити / Інший ФОП / Відхилити» (погоджує лише власник) → monobank Corporate API
POST /ext/v1/payment/prepare: платіж-чернетка, яку власник підписує в застосунку monobank →
стан GET /ext/v1/payment/state (DRAFT → IN_STATEMENT / DECLINED) щогодини; оплачений — далі звичайне рознесення виписки."""
from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import date, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PayRequest, SyncState, User

CHAT_KEY = "pay_chat_id"
CFG_KEY = "pay_cfg"
STATUS = {"new": "очікує погодження", "approved": "погоджено", "draft": "чернетка в monobank — підпишіть",
          "paid": "оплачено", "declined": "відхилено в monobank", "rejected": "відхилено", "error": "помилка"}

PROMPT = """Це рахунок на оплату (рахунок-фактура) від постачальника клініці в Україні.
Поверни ЛИШЕ JSON без пояснень:
{"seller": {"name": "", "edrpou": "", "iban": ""}, "payer": {"name": "", "edrpou": ""}, "number": "", "date": "РРРР-ММ-ДД",
 "total": 0, "vat": 0, "purpose": ""}
Правила: seller — ПОСТАЧАЛЬНИК / одержувач коштів (його IBAN UA… з 29 символів, ЄДРПОУ 8 цифр або ІПН 10 цифр);
payer — ПОКУПЕЦЬ / платник (зазвичай ФОП …); total — сума до сплати; vat — сума ПДВ (0, якщо без ПДВ);
purpose — призначення платежу, якщо воно прямо вказане в рахунку, інакше порожньо.
Числа — без пробілів, крапка як роздільник. Не вигадуй: невідоме — порожній рядок або 0."""


def _get(db: Session, key: str, default=None):
    st = db.get(SyncState, key)
    if st and st.value:
        try:
            return json.loads(st.value)
        except ValueError:
            return st.value
    return default


def _put(db: Session, key: str, val) -> None:
    st = db.get(SyncState, key) or SyncState(key=key)
    st.value, st.updated_at = json.dumps(val, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def chat_id(db: Session) -> str | None:
    v = _get(db, CHAT_KEY)
    return str(v) if v else None


def set_chat(db: Session, cid: str | None) -> None:
    _put(db, CHAT_KEY, str(cid) if cid else "")


def cfg(db: Session) -> dict:
    return {"sender": {}, **(_get(db, CFG_KEY, {}) or {})}


def save_cfg(db: Session, c: dict) -> None:
    _put(db, CFG_KEY, c)


def owner_tg_ids(db: Session) -> set[str]:
    """Хто може погоджувати: власник (роль owner) з указаним Telegram chat ID у «Мій кабінет»."""
    return {str(u.telegram_chat_id) for u in db.scalars(select(User).where(User.role == "owner", User.active.is_(True),
                                                                              User.telegram_chat_id.is_not(None)))}


# ---------------- розпізнавання ----------------
def _num(v) -> float:
    try:
        return round(float(str(v).replace(" ", "").replace(" ", "").replace(",", ".")), 2)
    except (TypeError, ValueError):
        return 0.0


def _date(v) -> date | None:
    s = str(v or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def clean_iban(v) -> str:
    return re.sub(r"\s", "", str(v or "")).upper()


def recognize(db: Session, data: bytes, filename: str, mime: str | None = None, claude=None) -> dict:
    """PDF / фото рахунку → поля. claude(key, data, mime, prompt) — для тестів."""
    from . import invoice_ai
    name = (filename or "").lower()
    mime = (mime or "").lower()
    if name.endswith(".pdf"):
        mime = "application/pdf"
    elif name.endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    elif name.endswith(".png"):
        mime = "image/png"
    elif name.endswith(".webp"):
        mime = "image/webp"
    if mime not in invoice_ai.IMAGE_TYPES | {"application/pdf"}:
        raise ValueError("Підтримуються PDF і фото (JPG, PNG, WebP)")
    key = invoice_ai.api_key(db)
    if not key and not claude:
        raise ValueError("Для розпізнавання рахунків потрібен ключ Claude API («Налаштування → Розпізнавання накладних»)")
    j = (claude or _claude)(key, data, mime, PROMPT)
    return {"receiver_name": ((j.get("seller") or {}).get("name") or "").strip()[:300],
            "receiver_iban": clean_iban((j.get("seller") or {}).get("iban"))[:40],
            "receiver_edrpou": re.sub(r"\D", "", str((j.get("seller") or {}).get("edrpou") or ""))[:20],
            "payer_name": ((j.get("payer") or {}).get("name") or "").strip()[:300],
            "invoice_no": str(j.get("number") or "").strip()[:80], "invoice_date": _date(j.get("date")),
            "amount": _num(j.get("total")), "vat": _num(j.get("vat")), "purpose": str(j.get("purpose") or "").strip()[:420], "raw": j}


def _claude(key: str, data: bytes, mime: str, prompt: str) -> dict:
    from . import invoice_ai
    if mime == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}
    r = httpx.post(invoice_ai.API, timeout=120,
                   headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                   json={"model": invoice_ai.model(), "max_tokens": 1500,
                         "messages": [{"role": "user", "content": [block, {"type": "text", "text": prompt}]}]})
    if r.status_code >= 400:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:200]}")
    out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError("Claude API: відповідь без JSON")
    return json.loads(m.group(0))


# ---------------- ФОП-платник і рахунок списання ----------------
def fops(db: Session) -> list[str]:
    from ..integrations import monobank
    return [x["fop"] for x in monobank.saved(db)]


def fop_for(db: Session, payer_name: str | None) -> str | None:
    """ФОП CRM за назвою платника в рахунку («ФОП Бондар Вадим …» → «Бондар»)."""
    p = (payer_name or "").lower()
    for f in fops(db):
        if f and f.lower().split()[0] in p:
            return f
    return None


def sender_accounts(db: Session, fop: str | None) -> list[dict]:
    """Гривневі рахунки ФОП у monobank (для вибору рахунку списання)."""
    from ..integrations import monobank
    rec = next((x for x in monobank.saved(db) if x["fop"] == fop), None)
    return [a for a in (rec or {}).get("accounts", []) if a.get("currency") == 980 and a.get("type") == "fop" and a.get("iban")]


def sender_iban(db: Session, fop: str | None) -> str | None:
    c = cfg(db)
    if fop and (c.get("sender") or {}).get(fop):
        return c["sender"][fop]
    accs = sender_accounts(db, fop)
    return accs[0]["iban"] if accs else None


def default_purpose(pr: PayRequest) -> str:
    no = f" № {pr.invoice_no}" if pr.invoice_no else ""
    dt = f" від {pr.invoice_date:%d.%m.%Y}" if pr.invoice_date else ""
    vat = f", у т.ч. ПДВ {pr.vat:.2f} грн" if pr.vat else ", без ПДВ"
    return f"Оплата за товар згідно рахунку{no}{dt}{vat}"[:420]


def create(db: Session, data: bytes, filename: str, mime: str | None, source: str = "crm", tg_chat: str | None = None,
           tg_msg: int | None = None, sender: str | None = None, claude=None) -> PayRequest:
    """Новий рахунок на оплату з файлу: зберегти, розпізнати, підставити постачальника, ФОП і призначення."""
    from . import ingest
    from .suppliers import find
    pr = PayRequest(file_path=ingest.store(data, filename or "rahunok"), file_name=(filename or "рахунок")[:200], mime=(mime or "")[:100] or None,
                    source=source, tg_chat=tg_chat, tg_msg=tg_msg, sender_name=(sender or "")[:200] or None)
    try:
        f = recognize(db, data, filename, mime, claude=claude)
        for k, v in f.items():
            setattr(pr, k, v)
    except Exception as e:  # noqa: BLE001
        pr.status, pr.error = "error", f"Не розпізнано: {str(e)[:300]}"
    hit = find(db, pr.receiver_iban, pr.receiver_edrpou)
    if hit:
        pr.supplier_id = hit[0].id
    pr.fop = fop_for(db, pr.payer_name) or (fops(db)[0] if len(fops(db)) == 1 else None)
    pr.sender_iban = sender_iban(db, pr.fop)
    if not pr.purpose:
        pr.purpose = default_purpose(pr)
    db.add(pr)
    db.commit()
    return pr


def problems(pr: PayRequest) -> list[str]:
    out = []
    if not pr.receiver_iban or not re.fullmatch(r"UA\d{27}", pr.receiver_iban or ""):
        out.append("IBAN отримувача")
    if not pr.receiver_edrpou or len(pr.receiver_edrpou) not in (8, 10):
        out.append("ЄДРПОУ / ІПН отримувача")
    if not pr.receiver_name:
        out.append("назва отримувача")
    if not pr.amount or pr.amount <= 0:
        out.append("сума")
    if not pr.fop:
        out.append("ФОП-платник")
    if not pr.sender_iban:
        out.append("рахунок списання ФОП")
    if not pr.purpose or len(pr.purpose) < 6:
        out.append("призначення платежу")
    return out


# ---------------- monobank Corporate API ----------------
def _corp_token(db: Session, fop: str | None) -> str | None:
    from ..integrations import monobank
    rec = next((x for x in monobank.saved(db) if x["fop"] == fop), None)
    return (rec or {}).get("corp_token") or (rec or {}).get("token")


def approve(db: Session, pr: PayRequest, user_id: int | None, http: httpx.Client | None = None) -> PayRequest:
    """Погодити → чернетка платежу в monobank (на підпис у застосунку). Помилка — в pr.error, статус error."""
    if pr.status in ("draft", "paid"):
        return pr
    bad = problems(pr)
    if bad:
        pr.status, pr.error = "error", "Не вистачає: " + ", ".join(bad)
        db.commit()
        return pr
    from ..integrations import monobank
    tok = _corp_token(db, pr.fop)
    if not tok:
        pr.status, pr.error = "error", f"ФОП {pr.fop}: немає токена monobank"
        db.commit()
        return pr
    ref = f"crm-pay-{pr.id}"
    body = {"senderIban": pr.sender_iban, "receiver": {"iban": pr.receiver_iban, "edrpou": pr.receiver_edrpou, "name": pr.receiver_name[:140]},
            "destination": pr.purpose[:420], "amount": int(round(pr.amount * 100)), "currency": 980, "externalReference": ref}
    c = http or httpx.Client(timeout=30)
    pr.decided_at, pr.decided_by = datetime.utcnow(), user_id
    try:
        r = c.post(f"{monobank.CORP}/ext/v1/payment/prepare", json=body,
                   headers={"x-token": tok, "accept": "application/json", "content-type": "application/json",
                            "idempotency-key": str(uuid.uuid5(uuid.NAMESPACE_URL, ref))})
        js = {}
        try:
            js = r.json()
        except ValueError:
            pass
        if r.status_code >= 400:
            msg = js.get("errorDescription") or js.get("message") or js.get("error") or r.text[:200]
            pr.status, pr.error = "error", (f"monobank {r.status_code}: {msg}" +
                                            (" — токен не має доступу до Corporate API (перевірте в Налаштування → monobank)" if r.status_code in (401, 403) else ""))[:500]
        else:
            pr.status, pr.error = "draft", None
            pr.corp_id = str(js.get("id") or js.get("paymentId") or "")[:80] or None
            pr.corp_state = str(js.get("state") or js.get("status") or "DRAFT")[:30]
    except httpx.HTTPError as e:
        pr.status, pr.error = "error", f"monobank недоступний: {str(e)[:200]}"
    db.commit()
    return pr


def reject(db: Session, pr: PayRequest, user_id: int | None) -> None:
    pr.status, pr.decided_at, pr.decided_by = "rejected", datetime.utcnow(), user_id
    db.commit()


def refresh(db: Session, pr: PayRequest, http: httpx.Client | None = None) -> str | None:
    """Стан чернетки в monobank: DRAFT → IN_STATEMENT (оплачено) / DECLINED. Повертає новий статус, якщо змінився."""
    if pr.status != "draft":
        return None
    from ..integrations import monobank
    tok = _corp_token(db, pr.fop)
    if not tok:
        return None
    c = http or httpx.Client(timeout=20)
    try:
        r = c.get(f"{monobank.CORP}/ext/v1/payment/state", params={"externalReference": f"crm-pay-{pr.id}"},
                  headers={"x-token": tok, "accept": "application/json"})
        js = r.json() if r.status_code < 400 else {}
    except (httpx.HTTPError, ValueError):
        return None
    st = str(js.get("state") or js.get("status") or "").upper()
    if not st or st == (pr.corp_state or ""):
        return None
    pr.corp_state = st[:30]
    new = None
    if st == "IN_STATEMENT":
        pr.status, pr.paid_at, new = "paid", datetime.utcnow(), "paid"
    elif st == "DECLINED":
        pr.status, new = "declined", "declined"
    db.commit()
    return new


def refresh_all(db: Session) -> list[PayRequest]:
    changed = []
    for pr in db.scalars(select(PayRequest).where(PayRequest.status == "draft")).all():
        if refresh(db, pr):
            changed.append(pr)
    return changed


# ---------------- текст картки ----------------
def card_text(db: Session, pr: PayRequest) -> str:
    from html import escape
    from ..models import Supplier
    sup = db.get(Supplier, pr.supplier_id) if pr.supplier_id else None
    lines = [f"🧾 <b>Рахунок на оплату #{pr.id}</b> · {STATUS.get(pr.status, pr.status)}"]
    lines.append(f"Отримувач: <b>{escape(pr.receiver_name or '—')}</b>" + (f" ({escape(sup.name)})" if sup else " · <i>новий постачальник</i>"))
    lines.append(f"IBAN: <code>{escape(pr.receiver_iban or '—')}</code> · ЄДРПОУ {escape(pr.receiver_edrpou or '—')}")
    lines.append(f"Сума: <b>{(pr.amount or 0):,.2f} ₴</b>".replace(",", " ") + (f" (ПДВ {pr.vat:,.2f})".replace(",", " ") if pr.vat else ""))
    if pr.invoice_no or pr.invoice_date:
        lines.append(f"Рахунок № {escape(pr.invoice_no or '—')}" + (f" від {pr.invoice_date:%d.%m.%Y}" if pr.invoice_date else ""))
    lines.append(f"Платник: <b>ФОП {escape(pr.fop)}</b>" if pr.fop else f"Платник: ⚠️ не визначено ({escape(pr.payer_name or '—')})")
    lines.append(f"Призначення: {escape(pr.purpose or '—')}")
    bad = problems(pr)
    if bad and pr.status in ("new", "error"):
        lines.append("⚠️ Перевірте: " + ", ".join(bad))
    if pr.error:
        lines.append(f"❗ {escape(pr.error)}")
    if pr.status == "draft":
        lines.append("✍️ Чернетка створена в monobank — підпишіть у застосунку.")
    if pr.status == "paid":
        lines.append("✅ Оплачено (у виписці).")
    return "\n".join(lines)
