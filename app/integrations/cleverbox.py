"""CleverBox REST API v3 (https://cbox.mobi/doc/rest/).

Усі запити — POST JSON на https://cbox.mobi/api/v3/rest/<method>, токен у заголовку `token`.
Ліміт: не більше 1 запиту на секунду. У налаштуваннях модуля REST API в CleverBox
має бути дозволена IP-адреса VPS, інакше прийде помилка 2.
"""
from __future__ import annotations

import logging
import re
import time
import base64
import hashlib
from datetime import datetime, date, timedelta
from typing import Iterator

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (Patient, Visit, ProductSale, CbService, Doctor, BeautyPlan, SyncState, Product, StockLevel,
                      Check)
from ..parsers.observ import norm

log = logging.getLogger("cleverbox")

ERRORS = {1: "адреса сайту не збігається з налаштуваннями API",
          2: "IP сервера не дозволена в налаштуваннях API CleverBox",
          400: "токен не валідний", 403: "акаунт вимкнено або порожній токен"}


class CleverBoxError(RuntimeError):
    pass


def _token_cipher() -> Fernet:
    """Key material stays in SECRET_KEY; the database only gets ciphertext."""
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def load_saved_token(db: Session) -> bool:
    """Restore a token configured through the owner UI after an app restart."""
    if settings.cleverbox_token:
        return True
    item = db.get(SyncState, "cleverbox_token_enc")
    if not item or not item.value:
        return False
    try:
        settings.cleverbox_token = _token_cipher().decrypt(item.value.encode()).decode()
        return bool(settings.cleverbox_token)
    except (InvalidToken, ValueError, UnicodeDecodeError):
        log.warning("Saved CleverBox token cannot be decrypted")
        return False


def save_token(db: Session, token: str) -> None:
    item = db.get(SyncState, "cleverbox_token_enc") or SyncState(key="cleverbox_token_enc")
    item.value = _token_cipher().encrypt(token.encode()).decode()
    db.merge(item)
    db.commit()
    settings.cleverbox_token = token


def d(dt: date) -> str:
    return dt.strftime("%d.%m.%Y")


def parse_dt(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def phone_key(p: str | None) -> str:
    return re.sub(r"\D", "", p or "")[-9:]


class CleverBox:
    def __init__(self, token: str | None = None, base_url: str | None = None):
        self.token = token or settings.cleverbox_token
        self.base = (base_url or settings.cleverbox_base_url).rstrip("/")
        if not self.token:
            raise CleverBoxError("CLEVERBOX_TOKEN не задано в .env")
        self._last = 0.0
        self.http = httpx.Client(timeout=30, headers={"token": self.token, "Content-Type": "application/json"})

    def call(self, method: str, body: dict | None = None) -> dict:
        wait = settings.cleverbox_min_interval_sec - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            self._last = time.monotonic()
            r = self.http.post(f"{self.base}/{method.lstrip('/')}", json=body or {})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            break
        if r.status_code in ERRORS:
            raise CleverBoxError(f"{method}: {ERRORS[r.status_code]}")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("ok") is False:
            err = data.get("error")
            raise CleverBoxError(f"{method}: {ERRORS.get(err, err) if isinstance(err, int) else err}")
        return data

    def paged(self, method: str, body: dict | None = None, page: int = 100) -> Iterator[dict]:
        offset = 0
        while True:
            res = self.call(method, {**(body or {}), "offset": offset})
            rows = res.get("data") or []
            yield from rows
            limit = int(res.get("limit") or page)
            total = res.get("total") or res.get("records")
            offset += len(rows)
            if not rows or len(rows) < limit or (total and offset >= int(total)):
                break

    # --- методи
    def filials(self):
        return self.call("filials").get("data", [])

    def staff(self):
        return list(self.paged("staff"))

    def services(self):
        return list(self.paged("services"))

    def clients(self, **flt):
        return self.paged("clients", flt)

    def search_client(self, phone: str):
        return self.call("clients/search", {"phone": re.sub(r"\D", "", phone)}).get("data", [])

    def client_future_visits(self, client_id: int):
        return self.call("clients/visits", {"client_id": str(client_id)}).get("data", [])

    def visits_on(self, day: date):
        body = {"date": d(day)}
        if settings.cleverbox_filial_id:
            body["filial_id"] = settings.cleverbox_filial_id
        return self.paged("visits", body)

    def services_sales(self, start: date, end: date, client_id: int | None = None):
        body = {"date": d(start), "date_end": d(end), "limit": 100}
        if client_id:
            body["client_id"] = client_id
        if settings.cleverbox_filial_id:
            body["filial_id"] = settings.cleverbox_filial_id
        return self.paged("services/sales", body)

    def products_list(self):
        return self.paged("products/list", {"limit": 100})

    def products_stock(self, day: date | None = None):
        body = {"limit": 100, "deleted": 0}
        if day:
            body["date"] = d(day)
        return self.paged("products/stock", body)

    def checks(self, start: date, end: date):
        body = {"date": d(start), "date_end": d(end), "limit": 100}
        if settings.cleverbox_filial_id:
            body["filial_id"] = settings.cleverbox_filial_id
        return self.paged("sales/checks", body)

    def products_sales(self, start: date, end: date):
        body = {"date": d(start), "date_end": d(end), "limit": 100}
        if settings.cleverbox_filial_id:
            body["filial_id"] = settings.cleverbox_filial_id
        return self.paged("products/sales", body)


# ---------------- Синхронізація ----------------

# ---------------- Атрибути клієнта з /clients ----------------
# Назви полів у різних версіях API CleverBox відрізняються — беремо перше наявне.
REGISTERED_KEYS = ("date_add", "date_added", "add_date", "created", "created_at", "date_create",
                   "date_created", "date_reg", "registration_date", "reg_date")
BIRTHDAY_KEYS = ("birthday", "birth_date", "date_birth", "birthdate", "bday")


def _first_val(c: dict, keys) -> str | None:
    for k in keys:
        v = c.get(k)
        if v not in (None, "", "0000-00-00", "00.00.0000"):
            return v
    return None


def apply_client_fields(p: Patient, c: dict) -> None:
    """Дата додавання в CleverBox і день народження — лише якщо API їх віддає; ручні правки не затираємо."""
    reg = parse_dt(_first_val(c, REGISTERED_KEYS))
    if reg and reg.year > 2000:
        p.cb_registered_at = reg
    if not p.birthday:
        b = parse_dt(_first_val(c, BIRTHDAY_KEYS))
        if b and 1920 < b.year <= date.today().year:
            p.birthday = b.date()
    if not p.email and isinstance(c.get("email"), str) and "@" in c["email"]:
        p.email = c["email"].strip()[:200]


def backfill_visit_amounts(db: Session) -> int:
    """Надані послуги без суми (перенесені до появи цін) — ціна з довідника послуг."""
    prices = {i: pr for i, pr in db.execute(select(CbService.id, CbService.price)) if pr}
    n = 0
    for v in db.scalars(select(Visit).where(Visit.status == "done", Visit.amount.is_(None),
                                            Visit.service_id.is_not(None))):
        if prices.get(v.service_id):
            v.amount = prices[v.service_id]
            n += 1
    db.commit()
    return n


def sync_clients(db: Session, cb: CleverBox) -> int:
    """Оновлює атрибути вже привʼязаних пацієнтів (дата додавання, ДН). Нових пацієнтів не створює."""
    by_cb = {p.cleverbox_id: p for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    n, keys = 0, set()
    for c in cb.clients():
        keys.update(c.keys())
        p = by_cb.get(int(c.get("id") or 0))
        if p:
            apply_client_fields(p, c)
            n += 1
    _state(db, "cleverbox_client_fields", ",".join(sorted(keys))[:1000])   # лише назви полів, без значень
    db.commit()
    return n


CANCEL_RE = re.compile(r"скас|відмін|отмен|не\s*прийш|не\s*з.явив|перенес|cancel", re.I)
DONE_RE = re.compile(r"викон|заверш|оплач|прийш|done", re.I)


def _state(db: Session, key: str, value: str | None = None) -> str | None:
    st = db.get(SyncState, key)
    if value is None:
        return st.value if st else None
    if not st:
        st = SyncState(key=key)
        db.add(st)
    st.value, st.updated_at = value, datetime.utcnow()
    return value


def sync_services(db: Session, cb: CleverBox) -> int:
    n = 0
    for s in cb.services():
        obj = db.get(CbService, int(s["id"])) or CbService(id=int(s["id"]))
        obj.name = s.get("name") or s.get("check_name") or ""
        obj.category = s.get("category_name")
        obj.price = float(s["price"]) if s.get("price") not in (None, "") else None
        obj.time_min = int(s["time"]) if s.get("time") else None
        db.merge(obj)
        n += 1
    db.commit()
    return n


def sync_staff(db: Session, cb: CleverBox) -> int:
    staff = cb.staff()
    n = 0
    for doc in db.scalars(select(Doctor).where(Doctor.cleverbox_id.is_(None))):
        dn = set(norm(doc.name).split())
        for s in staff:
            sn = set(norm(f"{s.get('name', '')} {s.get('lname', '')}").split())
            if dn and dn == sn:
                doc.cleverbox_id = int(s["id"]); n += 1
                break
    db.commit()
    return n


def link_patients(db: Session, cb: CleverBox) -> dict:
    """Привʼязка пацієнтів CRM до клієнтів CleverBox: спершу за телефоном, потім за ПІБ."""
    unlinked = db.scalars(select(Patient).where(Patient.cleverbox_id.is_(None))).all()
    res = {"linked": 0, "new_ids": [], "ambiguous": [], "not_found": []}
    if not unlinked:
        return res
    by_name: dict[frozenset, list[dict]] | None = None
    for p in unlinked:
        cand = []
        if p.phone and len(phone_key(p.phone)) >= 4:
            cand = cb.search_client(p.phone)
        if not cand:
            if by_name is None:                    # повний список клієнтів — один раз за синк
                by_name = {}
                for c in cb.clients():
                    by_name.setdefault(frozenset(norm(c.get("name", "")).split()), []).append(c)
            cand = by_name.get(frozenset(norm(p.full_name).split()), [])
        if len(cand) == 1:
            c = cand[0]
            if not db.scalar(select(Patient).where(Patient.cleverbox_id == int(c["id"]))):
                p.cleverbox_id = int(c["id"])
                if not p.phone and c.get("phone"):
                    p.phone = c["phone"].split(",")[0].strip()
                res["linked"] += 1
                res["new_ids"].append(p.id)
        elif len(cand) > 1:
            res["ambiguous"].append((p.id, p.full_name, [(c["id"], c.get("name"), c.get("phone")) for c in cand[:5]]))
        else:
            res["not_found"].append((p.id, p.full_name))
    db.commit()
    return res


def _service_names(db: Session) -> dict[int, str]:
    return {s.id: s.name for s in db.scalars(select(CbService))}


def sync_future_visits(db: Session, cb: CleverBox) -> int:
    """Майбутні записи — по кожному привʼязаному пацієнту з активним планом."""
    names = _service_names(db)
    # Не використовуємо SQL DISTINCT: PostgreSQL не має оператора рівності
    # для JSON-поля груп клієнта. ORM прибирає повтори без порівняння JSON.
    patients = db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None))
                          .join(BeautyPlan).where(BeautyPlan.status == "active")).unique().all()
    n = 0
    for p in patients:
        rows = cb.client_future_visits(p.cleverbox_id)
        seen = set()
        for v in rows:
            label = v.get("_status") or ""
            if CANCEL_RE.search(label):
                continue
            start = parse_dt(v.get("date_start"))
            if not start:
                continue
            for i, s in enumerate(v.get("services") or [{}]):
                sid = int(s["id"]) if s.get("id") else None
                ext = f"{v['id']}-{sid or i}"
                seen.add(ext)
                obj = db.scalar(select(Visit).where(Visit.source == "cb_visit", Visit.external_id == ext))
                if not obj:
                    obj = Visit(source="cb_visit", external_id=ext, patient_id=p.id)
                    db.add(obj)
                obj.service_id, obj.service_name = sid, names.get(sid) if sid else None
                obj.master_name = s.get("master")
                obj.starts_at, obj.status, obj.status_label = start, "booked", label
                obj.amount = float(v["total"]) if v.get("total") and len(v.get("services") or []) <= 1 else None
                obj.synced_at = datetime.utcnow()
                n += 1
        # записи, яких більше немає серед майбутніх (скасовані/перенесені) — прибираємо
        db.execute(delete(Visit).where(Visit.patient_id == p.id, Visit.source == "cb_visit",
                                       Visit.starts_at >= datetime.now(),
                                       Visit.external_id.not_in(seen or {"-"})))
    db.commit()
    return n


def sync_done_services(db: Session, cb: CleverBox, start: date, end: date,
                       client_id: int | None = None) -> int:
    pmap = {p.cleverbox_id: p.id for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    prices = {i: pr for i, pr in db.execute(select(CbService.id, CbService.price)) if pr}
    n = 0
    for s in cb.services_sales(start, end, client_id=client_id):
        pid = pmap.get(int(s.get("client_id") or 0))
        if not pid:
            continue
        dt = parse_dt(s.get("date"))
        ext = f"{s.get('sale_id')}-{s.get('service_id')}-{s.get('date')}"
        obj = db.scalar(select(Visit).where(Visit.source == "cb_service_sale", Visit.external_id == ext))
        if not obj:
            obj = Visit(source="cb_service_sale", external_id=ext, patient_id=pid)
            db.add(obj)
        obj.service_id = int(s["service_id"]) if s.get("service_id") else None
        obj.service_name = s.get("service_name")
        obj.master_name = s.get("user_name")
        obj.starts_at, obj.status, obj.status_label = dt, "done", "Надано"
        # CleverBox returns the unit price; visits are kept one service per row.
        # Якщо ціни в рядку немає — ціна послуги з довідника (оцінка).
        qty = max(1, int(s.get("quantity") or 1))
        try:
            unit = float(s.get("price") or 0) or float(prices.get(obj.service_id) or 0)
            total = max(0.0, unit * qty - float(s.get("discount") or 0))
            obj.amount = round(total / qty, 2) if unit else None
        except (TypeError, ValueError):
            obj.amount = prices.get(obj.service_id)
        obj.synced_at = datetime.utcnow()
        n += 1
        # кількість >1 в одному рядку — розгортаємо в окремі «візити»
        for k in range(2, qty + 1):
            ext_k = f"{ext}#{k}"
            vk = db.scalar(select(Visit).where(Visit.source == "cb_service_sale", Visit.external_id == ext_k))
            if vk:
                vk.amount = obj.amount
            else:
                db.add(Visit(source="cb_service_sale", external_id=ext_k, patient_id=pid,
                             service_id=obj.service_id, service_name=obj.service_name,
                             master_name=obj.master_name, starts_at=dt, status="done", status_label="Надано",
                             amount=obj.amount))
    db.commit()
    return n


def sync_product_sales(db: Session, cb: CleverBox, start: date, end: date) -> int:
    pmap = {p.cleverbox_id: p.id for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    n = 0
    for s in cb.products_sales(start, end):
        pid = pmap.get(int(s.get("client_id") or 0))
        if not pid:
            continue
        ext = f"{s.get('check_id')}-{s.get('product_id')}-{s.get('date')}"
        obj = db.scalar(select(ProductSale).where(ProductSale.external_id == ext))
        if not obj:
            obj = ProductSale(external_id=ext, patient_id=pid)
            db.add(obj)
        obj.product_id = int(s["product_id"]) if s.get("product_id") else None
        obj.product_name = s.get("product_name") or ""
        obj.quantity = int(s.get("quantity") or 1)
        obj.amount = float(s.get("total_amount") or 0) - float(s.get("discount") or 0)
        obj.sold_at = parse_dt(s.get("date")) or datetime.now()
        n += 1
    db.commit()
    return n


def _first(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def sync_products(db: Session, cb: CleverBox) -> int:
    """Каталог товарів CleverBox -> Product (ціна, собівартість, категорія)."""
    from ..services.catalog import link_all_items
    n = 0
    for it in cb.products_list():
        if it.get("for_sale") is False and it.get("for_shop") is False:
            continue                                   # витратні матеріали «для роботи» — не догляд
        pid = int(it["item_id"])
        prod = db.scalar(select(Product).where(Product.cb_product_id == pid))
        if not prod:
            prod = Product(cb_product_id=pid, name=it.get("name") or "", name_norm="")
            db.add(prod)
        prod.name = it.get("name") or prod.name
        prod.name_norm = norm(prod.name)
        prod.category = it.get("category_name")
        prod.price = float(_first(it, "sale_price", "base_price", default=0) or 0) or prod.price
        prod.cost = float(it["cost"]) if it.get("cost") not in (None, "") else prod.cost
        n += 1
    db.commit()
    link_all_items(db)
    return n


def sync_stock(db: Session, cb: CleverBox) -> int:
    rows = list(cb.products_stock())
    db.execute(delete(StockLevel))
    for r in rows:
        pid = _first(r, "product_id", "item_id", "id")
        db.add(StockLevel(cb_product_id=int(pid) if pid not in (None, "") else None,
                          name=str(_first(r, "product_name", "name", "title", default="")),
                          storage=_first(r, "storage_name", "storage", "store_name"),
                          qty=float(_first(r, "quantity", "qty", "stock", "balance", "count", "amount", default=0) or 0),
                          unit=_first(r, "unit", "unit_name")))
    db.commit()
    return len(rows)


def sync_checks(db: Session, cb: CleverBox, start: date, end: date) -> int:
    pmap = {p.cleverbox_id: p.id for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    n = 0
    for c in cb.checks(start, end):
        cid = int(c["check_id"])
        obj = db.get(Check, cid) or Check(id=cid)
        obj.date = parse_dt(c.get("date")) or datetime.now()
        obj.amount = float(c.get("amount") or 0)
        obj.discount = float(c.get("discount") or 0)
        obj.cb_client_id = int(c["client_id"]) if c.get("client_id") else None
        obj.patient_id = pmap.get(obj.cb_client_id)
        obj.author = c.get("author_name")
        obj.items = c.get("items") or []
        obj.payments = c.get("payments") or []
        obj.deleted = bool(int(c.get("deleted") or 0)) or c.get("status") == "deleted"
        db.merge(obj)
        n += 1
    db.commit()
    return n


def sync_calendar(db: Session, cb: CleverBox, days: int = 14) -> int:
    """Записи на найближчі N днів для всіх клієнтів (розділ «Календар»). Нових клієнтів створює в CRM."""
    names = _service_names(db)
    by_cb = {p.cleverbox_id: p for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    n = 0
    today = date.today()
    seen: set[str] = set()
    for i in range(days):
        day = today + timedelta(days=i)
        for v in cb.visits_on(day):
            label = v.get("_status") or ""
            if CANCEL_RE.search(label):
                continue
            cid = int(v.get("client_id") or 0)
            p = by_cb.get(cid)
            if not p and cid:
                cl = v.get("client") or {}
                p = Patient(full_name=(cl.get("name") or f"Клієнт CleverBox #{cid}").strip(), cleverbox_id=cid,
                            phone=(cl.get("phone") or "").split(",")[0].strip() or None)
                db.add(p)
                db.flush()
                by_cb[cid] = p
            if not p:
                continue
            start = parse_dt(v.get("date_start"))
            if not start:
                continue
            status = "attended" if DONE_RE.search(label) else "booked"   # «attended» не рахується в реалізацію —
            for j, s in enumerate(v.get("services") or [{}]):           # факт послуги береться з /services/sales
                sid = int(s["id"]) if s.get("id") else None
                ext = f"{v['id']}-{sid or j}"
                seen.add(ext)
                obj = db.scalar(select(Visit).where(Visit.source == "cb_visit", Visit.external_id == ext))
                if not obj:
                    obj = Visit(source="cb_visit", external_id=ext, patient_id=p.id)
                    db.add(obj)
                obj.service_id, obj.service_name = sid, names.get(sid) if sid else obj.service_name
                obj.master_name = s.get("master") or obj.master_name
                obj.starts_at, obj.status, obj.status_label = start, status, label
                obj.amount = float(v["total"]) if v.get("total") and len(v.get("services") or []) <= 1 else obj.amount
                obj.synced_at = datetime.utcnow()
                n += 1
    # скасовані/перенесені записи у вікні — прибираємо
    window_end = datetime.combine(today + timedelta(days=days), datetime.min.time())
    db.execute(delete(Visit).where(Visit.source == "cb_visit", Visit.status == "booked",
                                   Visit.starts_at >= datetime.now(), Visit.starts_at < window_end,
                                   Visit.external_id.not_in(seen or {"-"})))
    db.commit()
    return n


def full_sync(db: Session, initial: bool | None = None) -> dict:
    cb = CleverBox()
    last = _state(db, "last_sales_sync")
    initial = initial if initial is not None else last is None
    end = date.today()
    start = end - timedelta(days=settings.sync_lookback_days if initial else 3)
    report = {}
    if initial or not db.scalar(select(CbService).limit(1)):
        report["services"] = sync_services(db, cb)
        report["staff"] = sync_staff(db, cb)
    report["patients"] = link_patients(db, cb)
    report["done_services"] = sync_done_services(db, cb, start, end)
    report["product_sales"] = sync_product_sales(db, cb, start, end)
    # нові привʼязані пацієнти — довантажуємо історію за lookback-період
    new_ids = report["patients"]["new_ids"]
    if new_ids and not initial:
        hist_start = end - timedelta(days=settings.sync_lookback_days)
        for pid in new_ids:
            p = db.get(Patient, pid)
            report["done_services"] += sync_done_services(db, cb, hist_start, end, client_id=p.cleverbox_id)
        report["product_sales"] += sync_product_sales(db, cb, hist_start, end)
    report["future_visits"] = sync_future_visits(db, cb)
    for key, fn in (("checks", lambda: sync_checks(db, cb, start, end)),
                    ("stock", lambda: sync_stock(db, cb)),
                    ("calendar", lambda: sync_calendar(db, cb, 14))):
        try:
            report[key] = fn()
        except CleverBoxError as e:                # окремий модуль може бути вимкнений у тарифі CleverBox
            report[key] = f"помилка: {e}"
    last_prod = _state(db, "last_products_sync")
    if initial or not last_prod or datetime.fromisoformat(last_prod) < datetime.now() - timedelta(hours=20):
        try:
            report["products"] = sync_products(db, cb)
            _state(db, "last_products_sync", datetime.now().isoformat(timespec="seconds"))
        except CleverBoxError as e:
            report["products"] = f"помилка: {e}"
    report["amounts_filled"] = backfill_visit_amounts(db)
    last_cl = _state(db, "last_clients_sync")
    if initial or not last_cl or datetime.fromisoformat(last_cl) < datetime.now() - timedelta(hours=20):
        try:
            report["clients"] = sync_clients(db, cb)
            _state(db, "last_clients_sync", datetime.now().isoformat(timespec="seconds"))
        except CleverBoxError as e:
            report["clients"] = f"помилка: {e}"
    _state(db, "last_sales_sync", datetime.now().isoformat(timespec="seconds"))
    db.commit()
    return report


# ---------------- Повний перенос даних з CleverBox ----------------

def migrate(job, months: int = 24, all_clients: bool = True, start_date: date | None = None) -> dict:
    """Перенос з CleverBox: довідники → клієнти → надані послуги і продажі товарів за N місяців → майбутні записи.
    Запускається у фоні (services.jobs). Ліміт API 1 запит/сек, тому велика база переноситься довго (хвилини–година)."""
    from ..models import SessionLocal
    cb = CleverBox()
    rep: dict = {}
    with SessionLocal() as db:
        job.log("Довідник послуг…")
        rep["services"] = sync_services(db, cb)
        job.log(f"Послуг: {rep['services']}. Співробітники…")
        rep["staff_linked"] = sync_staff(db, cb)

        try:
            rep["products"] = sync_products(db, cb)
            job.log(f"Товарів у каталозі: {rep['products']}")
        except CleverBoxError as e:
            job.log(f"Товари: {e}")
        job.log("Клієнти…")
        created = linked = 0
        by_cb = {p.cleverbox_id: p for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
        unlinked = {frozenset(norm(p.full_name).split()): p
                    for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_(None)))}
        by_phone = {phone_key(p.phone): p for p in db.scalars(select(Patient).where(Patient.phone.is_not(None)))}
        n = 0
        for c in cb.clients():
            n += 1
            cid = int(c["id"])
            if n == 1:
                job.log("Поля клієнта в CleverBox: " + ", ".join(sorted(c.keys())))
            if cid in by_cb:
                apply_client_fields(by_cb[cid], c)
                continue
            phone = (c.get("phone") or "").split(",")[0].strip() or None
            p = (by_phone.get(phone_key(phone)) if phone and len(phone_key(phone)) >= 9 else None) \
                or unlinked.get(frozenset(norm(c.get("name", "")).split()))
            if p and p.cleverbox_id is None:
                p.cleverbox_id = cid
                p.phone = p.phone or phone
                linked += 1
            elif all_clients and c.get("name"):
                p = Patient(full_name=c["name"].strip(), phone=phone, cleverbox_id=cid)
                db.add(p)
                created += 1
            if p:
                apply_client_fields(p, c)
                by_cb[cid] = p
            if n % 500 == 0:
                db.commit()
                job.log(f"…оброблено клієнтів: {n}")
        db.commit()
        rep.update(clients_seen=n, patients_created=created, patients_linked=linked)
        job.log(f"Клієнтів: {n}, створено пацієнтів: {created}, привʼязано: {linked}")

        end = date.today()
        # Для повного переносу беремо всю доступну в CleverBox історію,
        # а не обмежуємо її довільною кількістю місяців.
        start = start_date or (end - timedelta(days=30 * months))
        rep["done_services"] = rep["product_sales"] = rep["checks"] = 0
        cur = start
        while cur <= end:                         # помісячно, щоб не впиратись у великі вибірки
            nxt = min(cur + timedelta(days=30), end)
            rep["done_services"] += sync_done_services(db, cb, cur, nxt)
            rep["product_sales"] += sync_product_sales(db, cb, cur, nxt)
            try:
                rep["checks"] += sync_checks(db, cb, cur, nxt)
            except CleverBoxError:
                pass
            job.log(f"Історія {cur:%d.%m.%Y}–{nxt:%d.%m.%Y}: послуг {rep['done_services']}, товарів "
                    f"{rep['product_sales']}, чеків {rep['checks']}")
            cur = nxt + timedelta(days=1)

        job.log("Майбутні записи пацієнтів з активними планами…")
        rep["future_visits"] = sync_future_visits(db, cb)
        for key, fn in (("stock", lambda: sync_stock(db, cb)), ("calendar", lambda: sync_calendar(db, cb, 14))):
            try:
                rep[key] = fn()
            except CleverBoxError as e:
                rep[key] = f"помилка: {e}"
        _state(db, "last_products_sync", datetime.now().isoformat(timespec="seconds"))
        _state(db, "last_sales_sync", datetime.now().isoformat(timespec="seconds"))
        _state(db, "last_migration", datetime.now().isoformat(timespec="seconds"))
        db.commit()
    return rep
