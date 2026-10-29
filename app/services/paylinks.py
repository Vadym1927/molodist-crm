"""Платіжні системи: посилання на оплату для пацієнтів (передоплата за запис, послуга, товар).
Провайдери (українські): monobank еквайринг (Plata by mono), LiqPay (ПриватБанк), WayForPay.
Налаштування — SyncState paysys_cfg, ключі зашифровано. Статус оплати — з вебхука провайдера
(для monobank статус ще й перепитуємо в API, тож підробити «оплачено» неможливо)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from ..models import PayLink, SyncState

PROVIDERS = {
    "mono": {"label": "monobank еквайринг", "site": "https://web.monobank.ua", "fields": [("token", "X-Token мерчанта", True)],
             "hint": "Кабінет monobank для бізнесу (web.monobank.ua) → Еквайринг → Інтернет-еквайринг → токен API."},
    "liqpay": {"label": "LiqPay", "site": "https://www.liqpay.ua", "fields": [("public_key", "Public key", False), ("private_key", "Private key", True)],
               "hint": "Кабінет LiqPay → Налаштування → API."},
    "wayforpay": {"label": "WayForPay", "site": "https://wayforpay.com", "fields": [("merchant", "Merchant account", False), ("domain", "Домен мерчанта", False),
                                                                             ("secret", "Merchant secret key", True)],
                  "hint": "Кабінет WayForPay → Налаштування магазину."},
}
SECRET = {f for p in PROVIDERS.values() for f, _, sec in p["fields"] if sec}
STATUS_LABELS = {"created": "очікує оплати", "paid": "оплачено", "failed": "помилка", "expired": "прострочено", "cancelled": "скасовано"}
DEFAULT_TEXT = "Посилання на оплату MOLODIST: {purpose} — {sum} ₴\n{url}"


class PayError(RuntimeError):
    pass


def _cipher():
    from ..integrations.cleverbox import _token_cipher
    return _token_cipher()


def _raw(db: Session) -> dict:
    st = db.get(SyncState, "paysys_cfg")
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    d.setdefault("provider", "")
    d.setdefault("validity_h", 24)
    d.setdefault("text", DEFAULT_TEXT)
    d.setdefault("cashbox", "Онлайн-оплати")
    d.setdefault("p", {})
    if not d.get("hook"):
        d["hook"] = _secrets.token_urlsafe(18)
    return d


def _save(db: Session, d: dict) -> None:
    st = db.get(SyncState, "paysys_cfg") or SyncState(key="paysys_cfg")
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def public(db: Session) -> dict:
    d = _raw(db)
    if not db.get(SyncState, "paysys_cfg"):
        _save(db, d)
    out = {k: v for k, v in d.items() if k != "p"}
    out["p"] = {pr: {k: (bool(v) if k in SECRET else v) for k, v in vals.items()} for pr, vals in d["p"].items()}
    out["ready"] = bool(d["provider"] and all(d["p"].get(d["provider"], {}).get(f) for f, _, _ in PROVIDERS[d["provider"]]["fields"])) \
        if d["provider"] in PROVIDERS else False
    return out


def val(d: dict, prov: str, key: str) -> str:
    v = (d["p"].get(prov) or {}).get(key)
    if not v or key not in SECRET:
        return v or ""
    try:
        return _cipher().decrypt(v.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


def save(db: Session, provider: str, vals: dict, validity_h: int, text: str, cashbox: str) -> None:
    d = _raw(db)
    if provider and provider not in PROVIDERS:
        raise ValueError("Невідома платіжна система")
    d.update(provider=provider, validity_h=max(1, min(int(validity_h or 24), 24 * 30)), text=(text or DEFAULT_TEXT)[:500],
             cashbox=(cashbox or "Онлайн-оплати")[:100])
    if provider:
        cur = d["p"].setdefault(provider, {})
        for f, _, sec in PROVIDERS[provider]["fields"]:
            v = str(vals.get(f) or "").strip()
            if sec:
                if v:
                    cur[f] = _cipher().encrypt(v.encode()).decode()
            else:
                cur[f] = v[:200]
    _save(db, d)


def _hmac_md5(key: str, s: str) -> str:
    return hmac.new(key.encode(), s.encode(), hashlib.md5).hexdigest()


def _liqpay_sig(priv: str, data: str) -> str:
    return base64.b64encode(hashlib.sha1((priv + data + priv).encode()).digest()).decode()


def create(db: Session, amount: float, purpose: str, base_url: str, patient_id: int | None = None, appointment_id: int | None = None,
           user_id: int | None = None, http: httpx.Client | None = None) -> PayLink:
    d = _raw(db)
    prov = d.get("provider")
    if prov not in PROVIDERS:
        raise PayError("Платіжну систему не підключено — «Налаштування → Платіжні системи»")
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        raise PayError("Вкажіть суму")
    purpose = (purpose or "Оплата послуг MOLODIST").strip()[:300]
    pl = PayLink(provider=prov, amount=amount, purpose=purpose, patient_id=patient_id, appointment_id=appointment_id, created_by=user_id)
    db.add(pl)
    db.flush()
    ref = f"MOL-{pl.id}-{_secrets.token_hex(3)}"
    hook = f"{base_url.rstrip('/')}/api/public/pay/{prov}/{d['hook']}"
    http = http or httpx.Client(timeout=25)
    valid = int(d.get("validity_h") or 24) * 3600
    try:
        if prov == "mono":
            r = http.post("https://api.monobank.ua/api/merchant/invoice/create", headers={"X-Token": val(d, prov, "token")},
                          json={"amount": int(round(amount * 100)), "ccy": 980, "validity": valid, "webHookUrl": hook,
                                "merchantPaymInfo": {"reference": ref, "destination": purpose}})
            js = r.json()
            if r.status_code >= 400 or not js.get("pageUrl"):
                raise PayError(f"monobank: {js.get('errText') or js.get('errCode') or r.status_code}")
            pl.ext_id, pl.url = js["invoiceId"], js["pageUrl"]
        elif prov == "liqpay":
            pub, priv = val(d, prov, "public_key"), val(d, prov, "private_key")
            exp = (datetime.utcnow() + timedelta(seconds=valid)).strftime("%Y-%m-%d %H:%M:%S")
            data = base64.b64encode(json.dumps({"version": 3, "public_key": pub, "action": "pay", "amount": f"{amount:.2f}", "currency": "UAH",
                                                "description": purpose, "order_id": ref, "server_url": hook, "expired_date": exp},
                                               ensure_ascii=False).encode()).decode()
            pl.ext_id = ref
            pl.url = "https://www.liqpay.ua/api/3/checkout?" + httpx.QueryParams({"data": data, "signature": _liqpay_sig(priv, data)}).__str__()
        elif prov == "wayforpay":
            m, dom, sec = val(d, prov, "merchant"), val(d, prov, "domain") or "crm.molodist.com.ua", val(d, prov, "secret")
            od = int(time.time())
            amt = f"{amount:.2f}"
            sig = _hmac_md5(sec, ";".join([m, dom, ref, str(od), amt, "UAH", purpose, "1", amt]))
            js = http.post("https://api.wayforpay.com/api", json={
                "transactionType": "CREATE_INVOICE", "merchantAccount": m, "merchantAuthType": "SimpleSignature", "merchantDomainName": dom,
                "merchantSignature": sig, "apiVersion": 1, "language": "UA", "serviceUrl": hook, "orderReference": ref, "orderDate": od,
                "amount": amt, "currency": "UAH", "orderTimeout": valid, "productName": [purpose], "productPrice": [amt], "productCount": [1]}).json()
            if not js.get("invoiceUrl"):
                raise PayError(f"WayForPay: {js.get('reason') or js.get('reasonCode') or 'помилка'}")
            pl.ext_id, pl.url = ref, js["invoiceUrl"]
    except httpx.HTTPError as e:
        db.rollback()
        raise PayError(f"Немає звʼязку з платіжною системою: {str(e)[:120]}") from e
    except PayError:
        db.rollback()
        raise
    pl.info = {"ref": ref, "valid_until": (datetime.now() + timedelta(seconds=valid)).strftime("%d.%m.%Y %H:%M")}
    db.commit()
    return pl


def message(db: Session, pl: PayLink) -> str:
    d = _raw(db)
    return (d.get("text") or DEFAULT_TEXT).replace("{url}", pl.url or "").replace("{sum}", f"{pl.amount:.2f}").replace("{purpose}", pl.purpose)


def _mark(db: Session, pl: PayLink, status: str, paid_amount: float | None = None, extra: dict | None = None) -> None:
    was = pl.status
    pl.status = status
    if extra:
        pl.info = {**(pl.info or {}), **extra}
    if status == "paid" and was != "paid":
        pl.paid_at = datetime.now()
        pl.paid_amount = paid_amount if paid_amount is not None else pl.amount
        _book(db, pl)
    db.commit()


def _book(db: Session, pl: PayLink) -> None:
    """Оплачене посилання → надходження в журналі фіноперацій CRM (каса «Онлайн-оплати») + сповіщення власнику."""
    from ..models import FinOp, Patient
    d = _raw(db)
    who = db.get(Patient, pl.patient_id).full_name if pl.patient_id and db.get(Patient, pl.patient_id) else None
    db.add(FinOp(day=datetime.now().date(), kind="income", category="Онлайн-оплата", amount=pl.paid_amount or pl.amount,
                 cashbox=d.get("cashbox") or "Онлайн-оплати", counterparty=who,
                 comment=f"Посилання на оплату №{pl.id} ({PROVIDERS[pl.provider]['label']}): {pl.purpose}"[:500]))
    try:
        from . import alerts
        alerts.send(f"💳 Онлайн-оплата {pl.paid_amount or pl.amount:.2f} ₴ · {who or 'без пацієнта'} · {pl.purpose}")
    except Exception:  # noqa: BLE001
        pass


def check_status(db: Session, pl: PayLink, http: httpx.Client | None = None) -> str:
    """Перепитати статус у провайдера (monobank — завжди так; для інших — що прийшло у вебхуку)."""
    d = _raw(db)
    if pl.provider == "mono" and pl.ext_id:
        http = http or httpx.Client(timeout=20)
        js = http.get("https://api.monobank.ua/api/merchant/invoice/status", params={"invoiceId": pl.ext_id},
                      headers={"X-Token": val(d, "mono", "token")}).json()
        st = {"success": "paid", "failure": "failed", "expired": "expired", "reversed": "cancelled"}.get(js.get("status"), pl.status)
        _mark(db, pl, st, (js.get("finalAmount") or js.get("amount") or 0) / 100 or None, {"mono_status": js.get("status")})
    return pl.status


def webhook(db: Session, provider: str, raw: bytes, form: dict, http: httpx.Client | None = None) -> dict:
    d = _raw(db)
    if provider == "mono":
        try:
            js = json.loads(raw or b"{}")
        except ValueError:
            js = {}
        pl = db.query(PayLink).filter(PayLink.provider == "mono", PayLink.ext_id == str(js.get("invoiceId") or "")).first()
        if pl:
            check_status(db, pl, http)                        # не віримо тілу — перепитуємо monobank
        return {"ok": True}
    if provider == "liqpay":
        data, sig = form.get("data", ""), form.get("signature", "")
        if not hmac.compare_digest(_liqpay_sig(val(d, "liqpay", "private_key"), data), sig or ""):
            return {"ok": False, "error": "signature"}
        js = json.loads(base64.b64decode(data or b"e30=").decode())
        pl = db.query(PayLink).filter(PayLink.provider == "liqpay", PayLink.ext_id == js.get("order_id")).first()
        if pl:
            st = "paid" if js.get("status") in ("success", "sandbox") else ("failed" if js.get("status") in ("failure", "error") else pl.status)
            _mark(db, pl, st, float(js.get("amount") or 0) or None, {"liqpay_status": js.get("status")})
        return {"ok": True}
    if provider == "wayforpay":
        try:
            js = json.loads(raw or b"{}")
        except ValueError:
            js = {}
        if not js and form:
            try:
                js = json.loads(next(iter(form.keys())))       # WayForPay шле JSON як ключ форми
            except (ValueError, StopIteration):
                js = {}
        sec = val(d, "wayforpay", "secret")
        s = ";".join(str(js.get(k, "")) for k in ("merchantAccount", "orderReference", "amount", "currency", "authCode", "cardPan",
                                                  "transactionStatus", "reasonCode"))
        if not hmac.compare_digest(_hmac_md5(sec, s), str(js.get("merchantSignature") or "")):
            return {"ok": False, "error": "signature"}
        pl = db.query(PayLink).filter(PayLink.provider == "wayforpay", PayLink.ext_id == js.get("orderReference")).first()
        if pl:
            ts = js.get("transactionStatus")
            st = "paid" if ts == "Approved" else ("failed" if ts in ("Declined",) else ("expired" if ts == "Expired" else pl.status))
            _mark(db, pl, st, float(js.get("amount") or 0) or None, {"wfp_status": ts})
        now = int(time.time())
        ref = js.get("orderReference", "")
        return {"orderReference": ref, "status": "accept", "time": now, "signature": _hmac_md5(sec, f"{ref};accept;{now}")}
    return {"ok": False}


def test(db: Session, http: httpx.Client | None = None) -> tuple[bool, str]:
    d = _raw(db)
    prov = d.get("provider")
    if prov not in PROVIDERS:
        return False, "Оберіть платіжну систему"
    http = http or httpx.Client(timeout=20)
    try:
        if prov == "mono":
            r = http.get("https://api.monobank.ua/api/merchant/details", headers={"X-Token": val(d, prov, "token")})
            js = r.json()
            if r.status_code >= 400:
                return False, f"monobank: {js.get('errText') or r.status_code}"
            return True, f"Підключено · {js.get('merchantName') or 'мерчант'}"
        if prov == "liqpay":
            return (True, "Ключі збережено · підпис формується — перевірте першим посиланням на 1 ₴") if \
                val(d, prov, "public_key") and val(d, prov, "private_key") else (False, "Вкажіть обидва ключі")
        if prov == "wayforpay":
            return (True, "Дані збережено · перевірте першим посиланням на 1 ₴") if \
                val(d, prov, "merchant") and val(d, prov, "secret") else (False, "Вкажіть merchant і secret key")
    except Exception as e:  # noqa: BLE001
        return False, f"Немає звʼязку: {str(e)[:150]}"
    return False, "?"
