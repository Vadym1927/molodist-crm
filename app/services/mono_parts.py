"""«Покупка Частинами monobank» для інтернет-магазину (API: https://monobank.ua/api-docs/chast).

Підпис кожного запиту: заголовки store-id і signature = Base64(HMAC-SHA256(тіло, signKey)).
Хід: замовлення з оплатою частинами → /api/order/create → покупець підтверджує в застосунку monobank →
банк шле callback (WAITING_FOR_STORE_CONFIRM) → замовлення «Оплачено частинами» → після відправки товару магазин
викликає /api/order/confirm (без цього розстрочка не активується). Резервно стан перепитується раз на 5 хв.
storeID і signKey зберігаються в базі лише зашифрованими."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SiteOrder, SyncState

KEY = "mono_parts_enc"
HOSTS = {"test": "https://u2-demo-ext.mono.st4g3.com", "stage": "https://u2-ext.mono.st4g3.com", "prod": "https://u2.monobank.com.ua"}
MODES = {"test": "Тест (пісочниця)", "stage": "Stage (перед запуском)", "prod": "Бойовий"}


class PartsError(RuntimeError):
    pass


def _cipher():
    from ..integrations.cleverbox import _token_cipher
    return _token_cipher()


def creds(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    if not st or not st.value:
        return {}
    try:
        return json.loads(_cipher().decrypt(st.value.encode()).decode())
    except Exception:  # noqa: BLE001
        return {}


def public(db: Session) -> dict:
    c = creds(db)
    return {"ready": bool(c.get("store_id") and c.get("sign_key") and c.get("on")), "has_keys": bool(c.get("store_id") and c.get("sign_key")),
            "on": bool(c.get("on")), "mode": c.get("mode") or "test", "parts": c.get("parts") or [3, 4], "min_sum": c.get("min_sum") or 500}


def save(db: Session, store_id: str, sign_key: str, mode: str, parts: str, on: bool, min_sum: float = 500, clear: bool = False) -> None:
    c = {} if clear else creds(db)
    if store_id.strip():
        c["store_id"] = store_id.strip()[:120]
    if sign_key.strip():
        c["sign_key"] = sign_key.strip()[:200]
    c["mode"] = mode if mode in HOSTS else "test"
    nums = sorted({int(x) for x in str(parts).replace(",", " ").split() if x.isdigit() and 2 <= int(x) <= 25})
    c["parts"] = nums or [3, 4]
    c["on"], c["min_sum"] = bool(on), float(min_sum or 0)
    db.merge(SyncState(key=KEY, value=_cipher().encrypt(json.dumps(c).encode()).decode(), updated_at=datetime.utcnow()))
    db.commit()


def sign(body: bytes, key: str) -> str:
    return base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


def _call(db: Session, path: str, payload: dict, http: httpx.Client | None = None) -> dict:
    c = creds(db)
    if not (c.get("store_id") and c.get("sign_key")):
        raise PartsError("Покупку Частинами не підключено")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    http = http or httpx.Client(timeout=25)
    try:
        r = http.post(HOSTS[c.get("mode") or "test"] + path, content=body,
                      headers={"store-id": c["store_id"], "signature": sign(body, c["sign_key"]), "Content-Type": "application/json", "Accept": "application/json"})
    except httpx.HTTPError as e:
        raise PartsError(f"немає звʼязку з monobank: {str(e)[:120]}") from e
    try:
        j = r.json() if r.content else {}
    except ValueError:
        j = {}
    if r.status_code >= 400:
        det = j.get("errors") or j.get("details") or j.get("fields") or ""
        msg = str(j.get("message") or j.get("error") or r.status_code)
        raise PartsError((msg + (f" ({json.dumps(det, ensure_ascii=False)[:150]})" if det else ""))[:300])
    return j


def phone(v: str | None) -> str:
    """Телефон для monobank — міжнародний формат +380XXXXXXXXX (у замовленнях він зберігається як 380…)."""
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    if len(d) == 10 and d.startswith("0"):
        d = "38" + d
    elif len(d) == 9:
        d = "380" + d
    return "+" + d if d else ""


def products(o: SiteOrder) -> list[dict]:
    """Позиції заявки; сума позицій має дорівнювати total_sum (знижки Coins/промокоду розкладаються на позиції)."""
    rows = [{"name": str(i["name"])[:500], "count": int(i.get("qty") or 1), "sum": round(float(i.get("price") or 0), 2)} for i in o.items or []]
    total = round(float(o.total or 0), 2)
    if not rows or abs(sum(r["count"] * r["sum"] for r in rows) - total) < 0.005:
        return rows
    lines = [{"name": r["name"], "count": 1, "sum": round(r["count"] * r["sum"], 2)} for r in rows]
    base = sum(x["sum"] for x in lines) or 1
    for x in lines:
        x["sum"] = round(x["sum"] * total / base, 2)
    big = max(lines, key=lambda x: x["sum"])
    big["sum"] = round(big["sum"] + total - sum(x["sum"] for x in lines), 2)
    return [x for x in lines if x["sum"] > 0]


def test(db: Session, http: httpx.Client | None = None) -> tuple[bool, str]:
    """Перевірка ключів: запит перевірки клієнта (нічого не створює)."""
    try:
        _call(db, "/api/client/validate", {"phone": "+380000000000"}, http)
        return True, "Ключі прийнято"
    except PartsError as e:
        msg = str(e)
        return ("401" not in msg and "403" not in msg and "sign" not in msg.lower() and "підключено" not in msg), msg


def create(db: Session, o: SiteOrder, parts: int, callback_url: str, http: httpx.Client | None = None) -> str:
    p = public(db)
    if not p["ready"]:
        raise PartsError("Покупка Частинами зараз недоступна")
    if parts not in p["parts"]:
        parts = p["parts"][0]
    j = _call(db, "/api/order/create", {
        "store_order_id": o.number, "client_phone": phone(o.phone), "total_sum": round(float(o.total or 0), 2),
        "invoice": {"date": (o.created_at or datetime.now()).strftime("%Y-%m-%d"), "number": o.number, "source": "INTERNET"},
        "available_programs": [{"available_parts_count": [parts], "type": "payment_installments"}],
        "products": products(o),
        "result_callback": callback_url}, http)
    oid = str(j.get("order_id") or "")
    if not oid:
        raise PartsError("monobank не повернув номер заявки")
    o.raw = {**(o.raw or {}), "parts": {"order_id": oid, "count": parts, "state": "IN_PROCESS", "sub": "WAITING_FOR_CLIENT", "at": datetime.now().isoformat(timespec="minutes")}}
    db.commit()
    return oid


def apply_state(db: Session, o: SiteOrder, state: str, sub: str = "") -> None:
    """Стан заявки monobank → замовлення CRM."""
    from . import shop_front as sf
    ps = dict((o.raw or {}).get("parts") or {})
    was = (ps.get("state"), ps.get("sub"))
    ps.update(state=state, sub=sub or "", checked=datetime.now().isoformat(timespec="minutes"))
    o.raw = {**(o.raw or {}), "parts": ps}
    if (state == "SUCCESS" or sub in ("WAITING_FOR_STORE_CONFIRM", "ACTIVE")) and not o.paid:
        o.paid, o.status = True, "Оплачено"
        db.commit()
        sf.notify(db, o, paid_now=True)
    elif state == "FAIL" and was[0] != "FAIL":
        if o.status in ("Очікує оплати", "Нове"):
            o.status = "Нове"
        o.comment = ((o.comment or "") + f"\nПокупка Частинами: відмова ({sub or 'FAIL'}) — запропонувати іншу оплату").strip()
    db.commit()


def refresh(db: Session, o: SiteOrder, http: httpx.Client | None = None) -> dict:
    ps = (o.raw or {}).get("parts") or {}
    if not ps.get("order_id"):
        return {}
    j = _call(db, "/api/order/state", {"order_id": ps["order_id"]}, http)
    apply_state(db, o, str(j.get("state") or ""), str(j.get("order_sub_state") or ""))
    return j


def confirm(db: Session, o: SiteOrder, http: httpx.Client | None = None) -> bool:
    """Товар відправлено / видано → підтвердити в monobank (активує розстрочку)."""
    ps = dict((o.raw or {}).get("parts") or {})
    if not ps.get("order_id") or ps.get("confirmed"):
        return False
    _call(db, "/api/order/confirm", {"order_id": ps["order_id"]}, http)
    ps["confirmed"] = datetime.now().isoformat(timespec="minutes")
    o.raw = {**(o.raw or {}), "parts": ps}
    db.commit()
    return True


def reject(db: Session, o: SiteOrder, http: httpx.Client | None = None) -> bool:
    ps = dict((o.raw or {}).get("parts") or {})
    if not ps.get("order_id") or ps.get("confirmed") or ps.get("rejected"):
        return False
    _call(db, "/api/order/reject", {"order_id": ps["order_id"]}, http)
    ps["rejected"] = datetime.now().isoformat(timespec="minutes")
    o.raw = {**(o.raw or {}), "parts": ps}
    db.commit()
    return True


def callback(db: Session, raw: bytes, signature: str) -> bool:
    c = creds(db)
    if not c.get("sign_key") or not hmac.compare_digest(sign(raw, c["sign_key"]), signature or ""):
        return False
    try:
        j = json.loads(raw or b"{}")
    except ValueError:
        return False
    oid = str(j.get("order_id") or "")
    for o in db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.created_at >= datetime.now() - timedelta(days=30))):
        if ((o.raw or {}).get("parts") or {}).get("order_id") == oid:
            apply_state(db, o, str(j.get("state") or ""), str(j.get("order_sub_state") or ""))
            return True
    return False


def poll(db: Session, http: httpx.Client | None = None) -> int:
    """Резервне опитування заявок, що чекають покупця (за 2 доби)."""
    if not public(db)["has_keys"]:
        return 0
    n = 0
    for o in db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.created_at >= datetime.now() - timedelta(days=2))):
        ps = (o.raw or {}).get("parts") or {}
        if ps.get("order_id") and ps.get("state") == "IN_PROCESS" and not o.paid:
            try:
                refresh(db, o, http)
                n += 1
            except PartsError:
                db.rollback()
    return n
