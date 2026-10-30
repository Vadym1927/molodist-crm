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
    d.setdefault("validity_h", 24)
    d.setdefault("text", DEFAULT_TEXT)
    d.setdefault("cashbox", "Онлайн-оплати")
    if "accounts" not in d:                                    # старий формат: один провайдер
        d["accounts"] = []
        if d.get("provider") in PROVIDERS:
            d["accounts"].append({"id": 1, "provider": d["provider"], "name": PROVIDERS[d["provider"]]["label"], "active": True,
                                  "cashbox": d.get("cashbox") or "Онлайн-оплати", "creds": (d.get("p") or {}).get(d["provider"], {})})
        d["default"] = 1 if d["accounts"] else None
    d.pop("provider", None)
    d.pop("p", None)
    if not d.get("hook"):
        d["hook"] = _secrets.token_urlsafe(18)
    return d


def _save(db: Session, d: dict) -> None:
    st = db.get(SyncState, "paysys_cfg") or SyncState(key="paysys_cfg")
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def _ready(a: dict) -> bool:
    return bool(a.get("active") and a.get("provider") in PROVIDERS and all((a.get("creds") or {}).get(f) for f, _, _ in PROVIDERS[a["provider"]]["fields"]))


def public(db: Session) -> dict:
    d = _raw(db)
    if not db.get(SyncState, "paysys_cfg"):
        _save(db, d)
    out = {k: v for k, v in d.items() if k != "accounts"}
    out["accounts"] = [{**{k: v for k, v in a.items() if k != "creds"}, "ready": _ready(a),
                        "creds": {k: (bool(v) if k in SECRET else v) for k, v in (a.get("creds") or {}).items()}} for a in d["accounts"]]
    out["ready"] = any(a["ready"] for a in out["accounts"])
    return out


def account_by_name(d: dict, name: str) -> dict | None:
    return next((a for a in d["accounts"] if a.get("name") == name), None)


def account(d: dict, aid: int | None) -> dict | None:
    acc = [a for a in d["accounts"] if a.get("active")]
    if aid:
        return next((a for a in d["accounts"] if int(a["id"]) == int(aid)), None)
    return next((a for a in acc if int(a["id"]) == int(d.get("default") or 0) and _ready(a)), None) or next((a for a in acc if _ready(a)), None)


def val(a: dict, key: str) -> str:
    v = (a.get("creds") or {}).get(key)
    if not v or key not in SECRET:
        return v or ""
    try:
        return _cipher().decrypt(v.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


def save_common(db: Session, validity_h: int, text: str, default: int | None) -> None:
    d = _raw(db)
    d.update(validity_h=max(1, min(int(validity_h or 24), 24 * 30)), text=(text or DEFAULT_TEXT)[:500])
    if default:
        d["default"] = int(default)
    _save(db, d)


def save_account(db: Session, aid: int | None, provider: str, name: str, vals: dict, cashbox: str, active: bool) -> dict:
    d = _raw(db)
    if provider not in PROVIDERS:
        raise ValueError("Оберіть платіжну систему")
    if not (name or "").strip():
        raise ValueError("Вкажіть назву підключення, напр. «monobank ФОП Колган»")
    a = next((x for x in d["accounts"] if aid and int(x["id"]) == int(aid)), None)
    if not a:
        a = {"id": max([int(x["id"]) for x in d["accounts"]] + [0]) + 1, "creds": {}}
        d["accounts"].append(a)
    if a.get("provider") and a["provider"] != provider:
        a["creds"] = {}
    a.update(provider=provider, name=name.strip()[:80], cashbox=(cashbox or "Онлайн-оплати").strip()[:100], active=active)
    cur = a.setdefault("creds", {})
    for f, _, sec in PROVIDERS[provider]["fields"]:
        v = str(vals.get(f) or "").strip()
        if sec:
            if v:
                cur[f] = _cipher().encrypt(v.encode()).decode()
        else:
            cur[f] = v[:200]
    if not d.get("default"):
        d["default"] = a["id"]
    _save(db, d)
    return a


def delete_account(db: Session, aid: int) -> None:
    d = _raw(db)
    d["accounts"] = [a for a in d["accounts"] if int(a["id"]) != int(aid)]
    if int(d.get("default") or 0) == int(aid):
        d["default"] = d["accounts"][0]["id"] if d["accounts"] else None
    _save(db, d)


def save(db: Session, provider: str, vals: dict, validity_h: int, text: str, cashbox: str) -> None:
    """Сумісність: одне підключення (перше з таким провайдером)."""
    d = _raw(db)
    a = next((x for x in d["accounts"] if x.get("provider") == provider), None)
    save_account(db, a["id"] if a else None, provider, a["name"] if a else PROVIDERS[provider]["label"], vals,
                 cashbox or "Онлайн-оплати", True)
    save_common(db, validity_h, text, None)


def _hmac_md5(key: str, s: str) -> str:
    return hmac.new(key.encode(), s.encode(), hashlib.md5).hexdigest()


def _liqpay_sig(priv: str, data: str) -> str:
    return base64.b64encode(hashlib.sha1((priv + data + priv).encode()).digest()).decode()


def create(db: Session, amount: float, purpose: str, base_url: str, patient_id: int | None = None, appointment_id: int | None = None,
           user_id: int | None = None, http: httpx.Client | None = None, account_id: int | None = None,
           basket: list[dict] | None = None) -> PayLink:
    d = _raw(db)
    a = account(d, account_id)
    if not a or not _ready(a):
        raise PayError("Платіжну систему не підключено — «Налаштування → Платіжні системи»")
    prov = a["provider"]
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        raise PayError("Вкажіть суму")
    purpose = (purpose or "Оплата послуг MOLODIST").strip()[:300]
    pl = PayLink(provider=prov, amount=amount, purpose=purpose, patient_id=patient_id, appointment_id=appointment_id, created_by=user_id,
                 account_id=a["id"])
    db.add(pl)
    db.flush()
    ref = f"MOL-{pl.id}-{_secrets.token_hex(3)}"
    hook = f"{base_url.rstrip('/')}/api/public/pay/{prov}/{d['hook']}"
    http = http or httpx.Client(timeout=25)
    valid = int(d.get("validity_h") or 24) * 3600
    try:
        if prov == "mono":
            r = http.post("https://api.monobank.ua/api/merchant/invoice/create", headers={"X-Token": val(a, "token")},
                          json={"amount": int(round(amount * 100)), "ccy": 980, "validity": valid, "webHookUrl": hook,
                                "merchantPaymInfo": {"reference": ref, "destination": purpose,
                                                     "basketOrder": _basket(basket, amount, purpose)}})
            js = r.json()
            if r.status_code >= 400 or not js.get("pageUrl"):
                raise PayError(f"monobank: {js.get('errText') or js.get('errCode') or r.status_code}")
            pl.ext_id, pl.url = js["invoiceId"], js["pageUrl"]
        elif prov == "liqpay":
            pub, priv = val(a, "public_key"), val(a, "private_key")
            exp = (datetime.utcnow() + timedelta(seconds=valid)).strftime("%Y-%m-%d %H:%M:%S")
            data = base64.b64encode(json.dumps({"version": 3, "public_key": pub, "action": "pay", "amount": f"{amount:.2f}", "currency": "UAH",
                                                "description": purpose, "order_id": ref, "server_url": hook, "expired_date": exp},
                                               ensure_ascii=False).encode()).decode()
            pl.ext_id = ref
            pl.url = "https://www.liqpay.ua/api/3/checkout?" + httpx.QueryParams({"data": data, "signature": _liqpay_sig(priv, data)}).__str__()
        elif prov == "wayforpay":
            m, dom, sec = val(a, "merchant"), val(a, "domain") or "crm.molodist.com.ua", val(a, "secret")
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
    pl.info = {"ref": ref, "account": a["name"], "valid_until": (datetime.now() + timedelta(seconds=valid)).strftime("%d.%m.%Y %H:%M")}
    db.commit()
    return pl


def _basket(basket: list[dict] | None, amount: float, purpose: str) -> list[dict]:
    """Кошик для monobank (обовʼязковий при фіскалізації в monobank): позиції в копійках, сума = сумі посилання.
    Якщо позиції не сходяться з сумою (часткова оплата) — один рядок «за що» на всю суму."""
    total = int(round(amount * 100))
    rows = []
    for b in basket or []:
        line = int(round(float(b.get("total") or 0) * 100))
        if line <= 0:
            continue
        qty = float(b.get("qty") or 1)
        q = int(round(qty)) if abs(qty - round(qty)) < 1e-9 and round(qty) >= 1 else 1
        if line % q:
            q = 1
        rows.append({"name": str(b.get("name") or purpose)[:128], "qty": q, "sum": line // q, "total": line,
                     "code": str(b.get("code") or "1")[:64], "unit": "шт."})
    if not rows or sum(r["total"] for r in rows) != total:
        rows = [{"name": purpose[:128], "qty": 1, "sum": total, "total": total, "code": "pay", "unit": "посл."}]
    return rows


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
    """Оплачене посилання → надходження в журналі фіноперацій CRM (каса «Онлайн-оплати») + сповіщення власнику.
    Посилання з чека каси CRM — чек стає «оплачено» (гроші вже враховані оплатою чека, фіноперацію не дублюємо)."""
    from ..models import FinOp, Patient, Sale
    if pl.sale_id:
        s = db.get(Sale, pl.sale_id)
        if s and s.status == "awaiting" and not (s.fiscal or {}).get("term_wait"):
            others = db.query(PayLink).filter(PayLink.sale_id == s.id, PayLink.id != pl.id, PayLink.status != "paid").count()
            if not others:
                s.status = "paid"
        try:
            from . import alerts
            alerts.send(f"💳 Онлайн-оплата {pl.paid_amount or pl.amount:.2f} ₴ · чек Ч-{pl.sale_id} · {pl.purpose}")
        except Exception:  # noqa: BLE001
            pass
        return
    d = _raw(db)
    a = _acc_of(d, pl)
    who = db.get(Patient, pl.patient_id).full_name if pl.patient_id and db.get(Patient, pl.patient_id) else None
    db.add(FinOp(day=datetime.now().date(), kind="income", category="Онлайн-оплата", amount=pl.paid_amount or pl.amount,
                 cashbox=(a or {}).get("cashbox") or "Онлайн-оплати", counterparty=who,
                 comment=f"Посилання на оплату №{pl.id} ({(a or {}).get('name') or PROVIDERS[pl.provider]['label']}): {pl.purpose}"[:500]))
    try:
        from . import alerts
        alerts.send(f"💳 Онлайн-оплата {pl.paid_amount or pl.amount:.2f} ₴ · {who or 'без пацієнта'} · {pl.purpose}")
    except Exception:  # noqa: BLE001
        pass


def _acc_of(d: dict, pl: PayLink) -> dict | None:
    if pl.account_id:
        a = next((x for x in d["accounts"] if int(x["id"]) == int(pl.account_id)), None)
        if a:
            return a
    return next((x for x in d["accounts"] if x.get("provider") == pl.provider), None)


def check_status(db: Session, pl: PayLink, http: httpx.Client | None = None) -> str:
    """Перепитати статус у провайдера (monobank — завжди так; для інших — що прийшло у вебхуку)."""
    d = _raw(db)
    if pl.provider == "mono" and pl.ext_id:
        http = http or httpx.Client(timeout=20)
        js = http.get("https://api.monobank.ua/api/merchant/invoice/status", params={"invoiceId": pl.ext_id},
                      headers={"X-Token": val(_acc_of(d, pl) or {}, "token")}).json()
        st = {"success": "paid", "failure": "failed", "expired": "expired", "reversed": "cancelled"}.get(js.get("status"), pl.status)
        _mark(db, pl, st, (js.get("finalAmount") or js.get("amount") or 0) / 100 or None, {"mono_status": js.get("status")})
    return pl.status


def remove(db: Session, pl: PayLink, http: httpx.Client | None = None) -> None:
    """Скасувати неоплачене посилання: у monobank — інвалідувати рахунок (POST /invoice/remove), у CRM — статус cancelled."""
    if pl.status != "created":
        return
    if pl.provider == "mono" and pl.ext_id:
        try:
            http = http or httpx.Client(timeout=20)
            http.post("https://api.monobank.ua/api/merchant/invoice/remove", json={"invoiceId": pl.ext_id},
                      headers={"X-Token": val(_acc_of(_raw(db), pl) or {}, "token")})
            pl.info = {**(pl.info or {}), "removed": True}
        except Exception as e:  # noqa: BLE001
            pl.info = {**(pl.info or {}), "remove_error": str(e)[:200]}
    pl.status = "cancelled"
    db.commit()


def mono_fiscal(db: Session, pl: PayLink, http: httpx.Client | None = None, with_file: bool = False) -> list[dict]:
    """Фіскальні чеки, які monobank створив для оплати (власний ПРРО або Checkbox), — GET /invoice/fiscal-checks.
    Готовий чек записуємо в чек CRM (sale.fiscal), тож повторно фіскалізувати не потрібно."""
    if pl.provider != "mono" or not pl.ext_id:
        return []
    d = _raw(db)
    http = http or httpx.Client(timeout=20)
    js = http.get("https://api.monobank.ua/api/merchant/invoice/fiscal-checks", params={"invoiceId": pl.ext_id},
                  headers={"X-Token": val(_acc_of(d, pl) or {}, "token")}).json()
    checks = [c for c in (js.get("checks") or []) if isinstance(c, dict)]
    if with_file:
        return checks
    out = [{"id": c.get("id"), "type": c.get("type"), "status": c.get("status"), "url": c.get("taxUrl"),
            "source": c.get("fiscalizationSource"), "note": c.get("statusDescription")} for c in checks]
    pl.info = {**(pl.info or {}), "fiscal": out}
    done = [c for c in out if (c.get("status") or "").lower() == "done" and (c.get("type") or "sale").lower() != "return"]
    if done and pl.sale_id:
        from ..models import Sale
        s = db.get(Sale, pl.sale_id)
        if s and not (s.fiscal or {}).get("fiscal_code"):
            c = done[0]
            src = {"checkbox": "Checkbox", "monopay": "monobank"}.get((c.get("source") or "").lower(), c.get("source") or "monobank")
            s.fiscal = {**(s.fiscal or {}), "fiscal_code": c.get("id"), "number": c.get("id"), "url": c.get("url"),
                        "rro_name": f"monobank · {src}", "source": "monobank", "paylink": pl.id,
                        "at": datetime.now().strftime("%d.%m.%Y %H:%M")}
    db.commit()
    return out


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
        try:
            js = json.loads(base64.b64decode(data or "e30=").decode())
        except ValueError:
            return {"ok": False, "error": "data"}
        pl = db.query(PayLink).filter(PayLink.provider == "liqpay", PayLink.ext_id == js.get("order_id")).first()
        if not pl or not hmac.compare_digest(_liqpay_sig(val(_acc_of(d, pl) or {}, "private_key"), data), sig or ""):
            return {"ok": False, "error": "signature"}
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
        pl = db.query(PayLink).filter(PayLink.provider == "wayforpay", PayLink.ext_id == js.get("orderReference")).first()
        sec = val(_acc_of(d, pl) or {}, "secret") if pl else ""
        if not pl:
            return {"ok": False, "error": "unknown order"}
        s = ";".join(str(js.get(k, "")) for k in ("merchantAccount", "orderReference", "amount", "currency", "authCode", "cardPan",
                                                  "transactionStatus", "reasonCode"))
        if not hmac.compare_digest(_hmac_md5(sec, s), str(js.get("merchantSignature") or "")):
            return {"ok": False, "error": "signature"}
        if pl:
            ts = js.get("transactionStatus")
            st = "paid" if ts == "Approved" else ("failed" if ts in ("Declined",) else ("expired" if ts == "Expired" else pl.status))
            _mark(db, pl, st, float(js.get("amount") or 0) or None, {"wfp_status": ts})
        now = int(time.time())
        ref = js.get("orderReference", "")
        return {"orderReference": ref, "status": "accept", "time": now, "signature": _hmac_md5(sec, f"{ref};accept;{now}")}
    return {"ok": False}


def test(db: Session, aid: int | None = None, http: httpx.Client | None = None) -> tuple[bool, str]:
    d = _raw(db)
    a = account(d, aid)
    if not a:
        return False, "Додайте підключення"
    prov = a["provider"]
    http = http or httpx.Client(timeout=20)
    try:
        if prov == "mono":
            r = http.get("https://api.monobank.ua/api/merchant/details", headers={"X-Token": val(a, "token")})
            js = r.json()
            if r.status_code >= 400:
                return False, f"monobank: {js.get('errText') or r.status_code}"
            return True, f"{a['name']}: підключено · {js.get('merchantName') or 'мерчант'}"
        if prov == "liqpay":
            return (True, f"{a['name']}: ключі збережено — перевірте першим посиланням на 1 ₴") if \
                val(a, "public_key") and val(a, "private_key") else (False, "Вкажіть обидва ключі")
        if prov == "wayforpay":
            return (True, f"{a['name']}: дані збережено — перевірте першим посиланням на 1 ₴") if \
                val(a, "merchant") and val(a, "secret") else (False, "Вкажіть merchant і secret key")
    except Exception as e:  # noqa: BLE001
        return False, f"Немає звʼязку: {str(e)[:150]}"
    return False, "?"
