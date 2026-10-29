"""Фіскалізація продажів CRM через Checkbox (ПРРО) — як «Інтеграція Checkbox» у CleverBox.

Блоки налаштувань (SyncState fiscal_cfg; ключі РРО — fiscal_rro_enc, зашифровано):
  • Фіскальний чек — авто/вручну, РРО за замовчуванням, які способи оплати фіскалізуються (готівка / картка / ні);
  • РРО — каси: назва, ФОП, ключ ліцензії каси, логін і пароль (або PIN) касира, тестовий режим;
  • Відправлення чеків — канали (Telegram / Viber / SMS) і текст повідомлення з посиланням на чек;
  • Термінали (Checkbox PayLink) і Віджет чека — прапорці на майбутнє.
API Checkbox: POST /cashier/signin → Bearer; GET /cashier/shift; POST /shifts (X-License-Key) → OPENED;
POST /receipts/sell {goods[{good{code,name,price коп.}, quantity×1000, discounts}], payments[{type CASH|CASHLESS, value}]};
GET /receipts/{id} → fiscal_code. Повернення — той самий /receipts/sell з is_return і related_receipt_id."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from ..models import Sale, SyncState

PROD = "https://api.checkbox.in.ua/api/v1"
DEV = "https://dev-api.checkbox.in.ua/api/v1"
HEADERS = {"X-Client-Name": "MOLODIST CRM", "X-Client-Version": "1.0"}
CHECK_URL = "https://check.checkbox.ua/{id}"
NON_FISCAL_DISCOUNT = ("Бонуси", "Сертифікат")     # не гроші — у чеку як знижка
DEFAULT = {
    "auto": False, "default_rro": None, "method_modes": {"Готівка": "cash", "Картка (термінал)": "card"},
    "send": {"on": False, "channels": ["telegram", "viber", "sms"],
             "text": "Дякуємо, що обрали MOLODIST! Ваш фіскальний чек: {url}"},
    "paylink": False, "widget": False,
}


class FiscalError(RuntimeError):
    pass


def _cipher():
    from ..integrations.cleverbox import _token_cipher
    return _token_cipher()


def cfg(db: Session) -> dict:
    st = db.get(SyncState, "fiscal_cfg")
    d = json.loads(json.dumps(DEFAULT))
    if st and st.value:
        try:
            v = json.loads(st.value)
            d.update({k: v[k] for k in v if k != "send"})
            d["send"].update(v.get("send") or {})
        except ValueError:
            pass
    return d


def save_cfg(db: Session, d: dict) -> None:
    st = db.get(SyncState, "fiscal_cfg") or SyncState(key="fiscal_cfg")
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def _rro_raw(db: Session) -> list[dict]:
    st = db.get(SyncState, "fiscal_rro_enc")
    if not st or not st.value:
        return []
    try:
        return json.loads(_cipher().decrypt(st.value.encode()).decode())
    except Exception:  # noqa: BLE001
        return []


def _rro_save(db: Session, items: list[dict]) -> None:
    st = db.get(SyncState, "fiscal_rro_enc") or SyncState(key="fiscal_rro_enc")
    st.value, st.updated_at = _cipher().encrypt(json.dumps(items, ensure_ascii=False).encode()).decode(), datetime.utcnow()
    db.merge(st)
    db.commit()


def rros(db: Session) -> list[dict]:
    """Для інтерфейсу — без секретів."""
    return [{"id": r["id"], "name": r["name"], "fop": r.get("fop", ""), "test": bool(r.get("test")), "on": r.get("on", True),
             "login": r.get("login", ""), "has_key": bool(r.get("license_key")), "has_pw": bool(r.get("password") or r.get("pin")),
             "status": r.get("status")} for r in _rro_raw(db)]


def save_rro(db: Session, rid: int | None, data: dict) -> int:
    items = _rro_raw(db)
    r = next((x for x in items if rid and int(x["id"]) == int(rid)), None)
    if not r:
        r = {"id": max([int(x["id"]) for x in items] + [0]) + 1}
        items.append(r)
    for k in ("name", "fop", "login"):
        if k in data:
            r[k] = str(data[k] or "").strip()[:120]
    r["test"], r["on"] = bool(data.get("test")), bool(data.get("on", True))
    for k in ("license_key", "password", "pin"):           # порожнє — лишаємо збережене
        if data.get(k):
            r[k] = str(data[k]).strip()
    _rro_save(db, items)
    c = cfg(db)
    if not c.get("default_rro"):
        c["default_rro"] = r["id"]
        save_cfg(db, c)
    return int(r["id"])


def delete_rro(db: Session, rid: int) -> None:
    _rro_save(db, [x for x in _rro_raw(db) if int(x["id"]) != int(rid)])


def _set_status(db: Session, rid: int, ok: bool, msg: str) -> None:
    items = _rro_raw(db)
    for x in items:
        if int(x["id"]) == int(rid):
            x["status"] = {"ok": ok, "msg": msg[:200], "at": datetime.now().strftime("%d.%m.%Y %H:%M")}
    _rro_save(db, items)


class Kasa:
    def __init__(self, r: dict, http: httpx.Client | None = None, wait: float = 1.0):
        if not r.get("license_key"):
            raise FiscalError("Не вказано ключ ліцензії каси")
        self.base, self.key, self.wait = (DEV if r.get("test") else PROD), r["license_key"], wait
        self.http = http or httpx.Client(timeout=30, headers=HEADERS)
        if r.get("pin") and not r.get("password"):
            resp = self.http.post(f"{self.base}/cashier/signinPinCode", json={"pin_code": r["pin"]}, headers=self._h(False))
        else:
            resp = self.http.post(f"{self.base}/cashier/signin", json={"login": r.get("login", ""), "password": r.get("password", "")},
                                  headers=self._h(False))
        if resp.status_code >= 400:
            raise FiscalError(f"вхід касира: {_msg(resp)}")
        self.token = resp.json().get("access_token") or ""
        if not self.token:
            raise FiscalError("вхід касира: немає access_token")

    def _h(self, auth: bool = True) -> dict:
        h = {**HEADERS, "X-License-Key": self.key}
        if auth:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        r = self.http.request(method, f"{self.base}/{path}", json=body, headers=self._h())
        if r.status_code >= 400:
            raise FiscalError(f"{path}: {_msg(r)}")
        try:
            return r.json() or {}
        except ValueError:
            return {}

    def shift(self) -> dict | None:
        try:
            s = self._req("GET", "cashier/shift")
        except FiscalError:
            return None
        return s if s and s.get("status") in ("OPENED", "CREATED", "OPENING") else None

    def ensure_shift(self) -> dict:
        s = self.shift() or self._req("POST", "shifts", {})
        for _ in range(15):
            if s.get("status") == "OPENED":
                return s
            time.sleep(self.wait)
            s = self._req("GET", f"shifts/{s['id']}")
        raise FiscalError("зміна не відкрилась — перевірте підпис (Checkbox Підпис / хмарний КЕП) у кабінеті Checkbox")

    def close_shift(self) -> dict:
        return self._req("POST", "shifts/close", {})

    def sell(self, body: dict) -> dict:
        rc = self._req("POST", "receipts/sell", body)
        for _ in range(15):
            if rc.get("status") in ("DONE", "ERROR"):
                break
            time.sleep(self.wait)
            rc = self._req("GET", f"receipts/{rc['id']}")
        if rc.get("status") == "ERROR":
            raise FiscalError("Checkbox відхилив чек: " + str(rc.get("transaction", {}).get("response_error_message") or "")[:150])
        return rc


def _msg(r) -> str:
    try:
        js = r.json()
        return f"{r.status_code} {js.get('message') or js.get('detail') or ''}".strip()
    except ValueError:
        return str(r.status_code)


def _rro(db: Session, rid: int | None) -> dict:
    c = cfg(db)
    rid = rid or c.get("default_rro")
    r = next((x for x in _rro_raw(db) if rid and int(x["id"]) == int(rid)), None)
    if not r:
        raise FiscalError("Не вибрано касу (РРО) — додайте її в «Налаштування → Checkbox → РРО»")
    return r


def mode_of(c: dict, method: str) -> str:
    m = (c.get("method_modes") or {}).get(method)
    if m:
        return m
    if method in NON_FISCAL_DISCOUNT:
        return "discount"
    return "skip"


def build(db: Session, sale: Sale, is_return: bool = False) -> dict:
    """Тіло чека Checkbox. Бонуси/сертифікат — знижкою на чек; способи «не фіскалізувати» — помилка (чек вручну)."""
    c = cfg(db)
    pays, disc, skipped = [], 0, []
    for p in sale.payments:
        m = mode_of(c, p.method)
        kop = int(round(p.amount * 100))
        if m == "cash":
            pays.append({"type": "CASH", "value": kop, "label": "Готівка"})
        elif m == "card":
            pays.append({"type": "CASHLESS", "value": kop, "label": p.method[:40]})
        elif m == "discount":
            disc += kop
        else:
            skipped.append(p.method)
    if skipped:
        raise FiscalError("У чеку є оплата, яка не фіскалізується: " + ", ".join(skipped))
    if not pays:
        raise FiscalError("Немає оплат готівкою чи карткою — фіскалізувати нічого")
    goods = []
    for i in sale.items:
        g = {"good": {"code": f"{i.kind[:1]}{i.ref_id or i.id}", "name": i.name[:250], "price": int(round(i.price * 100))},
             "quantity": int(round(i.qty * 1000))}
        if i.discount:
            g["discounts"] = [{"type": "DISCOUNT", "mode": "VALUE", "value": int(round(i.discount * 100)), "name": "Знижка"}]
        if is_return:
            g["is_return"] = True
        goods.append(g)
    body = {"id": str(uuid.uuid4()), "goods": goods, "payments": pays}
    if disc:
        body["discounts"] = [{"type": "DISCOUNT", "mode": "VALUE", "value": disc, "name": "Бонуси / сертифікат"}]
    if is_return and sale.fiscal and sale.fiscal.get("id"):
        body["related_receipt_id"] = sale.fiscal["id"]
    return body


def fiscalize(db: Session, sale: Sale, rid: int | None = None, http: httpx.Client | None = None, wait: float = 1.0,
              is_return: bool = False) -> dict:
    if not is_return and sale.fiscal and sale.fiscal.get("fiscal_code"):
        raise FiscalError("Чек уже фіскалізовано")
    r = _rro(db, rid or ((sale.fiscal or {}).get("rro") if is_return else None))
    body = build(db, sale, is_return)
    k = Kasa(r, http, wait)
    k.ensure_shift()
    rc = k.sell(body)
    info = {"id": rc.get("id"), "fiscal_code": rc.get("fiscal_code"), "url": CHECK_URL.format(id=rc.get("id")),
            "rro": r["id"], "rro_name": r["name"], "test": bool(r.get("test")), "at": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "number": rc.get("fiscal_code")}
    f = dict(sale.fiscal or {})
    if is_return:
        f["return"] = info
    else:
        f.update(info)
    sale.fiscal = f
    db.commit()
    _set_status(db, r["id"], True, "чек " + str(rc.get("fiscal_code") or rc.get("id")))
    return info


def send_receipt(db: Session, sale: Sale) -> str | None:
    """Посилання на чек пацієнту обраними каналами. Повертає помилку або None."""
    c = cfg(db)
    if not (c["send"].get("on") and sale.patient and sale.fiscal and sale.fiscal.get("url")):
        return "вимкнено або немає пацієнта"
    from ..integrations.messaging import send_to_patient
    text = (c["send"].get("text") or DEFAULT["send"]["text"]).replace("{url}", sale.fiscal["url"]) \
        .replace("{sum}", f"{sale.total:.2f}").replace("{name}", (sale.patient.full_name or "").split(" ")[0])
    res = send_to_patient(sale.patient, text, prefer=c["send"].get("channels") or None)
    f = dict(sale.fiscal)
    f["sent"] = {"ok": res.ok, "channel": res.channel, "at": datetime.now().strftime("%d.%m.%Y %H:%M")}
    sale.fiscal = f
    db.commit()
    return None if res.ok else (res.error or "не надіслано")


def auto(sale_id: int) -> None:
    """Після продажу (у фоні): якщо ввімкнено автофіскалізацію — чек у Checkbox і відправлення пацієнту."""
    from ..models import SessionLocal
    with SessionLocal() as db:
        c = cfg(db)
        s = db.get(Sale, sale_id)
        if not (c.get("auto") and s and s.status == "paid"):
            return
        try:
            fiscalize(db, s)
        except FiscalError as e:
            f = dict(s.fiscal or {})
            f["error"] = str(e)[:200]
            s.fiscal = f
            db.commit()
            return
        send_receipt(db, s)


def auto_return(sale_id: int) -> None:
    """Повернення фіскалізованого чека — чек повернення в Checkbox (у фоні)."""
    from ..models import SessionLocal
    with SessionLocal() as db:
        s = db.get(Sale, sale_id)
        if not (s and s.fiscal and s.fiscal.get("fiscal_code")) or s.fiscal.get("return"):
            return
        try:
            fiscalize(db, s, is_return=True)
        except FiscalError as e:
            f = dict(s.fiscal)
            f["return_error"] = str(e)[:200]
            s.fiscal = f
            db.commit()


def test(db: Session, rid: int, http: httpx.Client | None = None) -> tuple[bool, str]:
    try:
        r = _rro(db, rid)
        k = Kasa(r, http)
        sh = k.shift()
        msg = "Підключено" + (" (тестове середовище)" if r.get("test") else "") + (" · зміна відкрита" if sh else " · зміна закрита")
        _set_status(db, rid, True, msg)
        return True, msg
    except (FiscalError, httpx.HTTPError) as e:
        _set_status(db, rid, False, str(e))
        return False, str(e)[:200]
