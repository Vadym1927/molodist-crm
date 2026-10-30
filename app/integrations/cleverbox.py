"""CleverBox REST API v3 (https://cbox.mobi/doc/rest/).

Усі запити — POST JSON на https://cbox.mobi/api/v3/rest/<method>, токен у заголовку `token`.
Ліміт: не більше 1 запиту на секунду. У налаштуваннях модуля REST API в CleverBox
має бути дозволена IP-адреса VPS, інакше прийде помилка 2.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import base64
import hashlib
from datetime import datetime, date, timedelta
from typing import Iterator

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, delete, func
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


_RATE_LOCK = threading.Lock()
_RATE = {"last": 0.0}


def _is_rate_limited(err) -> bool:
    e = str(err or "").lower()
    return err == 429 or "per second" in e or "too many" in e or "limit" in e and "request" in e


class CleverBox:
    def __init__(self, token: str | None = None, base_url: str | None = None):
        self.token = token or settings.cleverbox_token
        self.base = (base_url or settings.cleverbox_base_url).rstrip("/")
        if not self.token:
            raise CleverBoxError("CLEVERBOX_TOKEN не задано в .env")
        self._last = 0.0
        self.http = httpx.Client(timeout=30, headers={"token": self.token, "Content-Type": "application/json"})

    def call(self, method: str, body: dict | None = None) -> dict:
        """Один запит до CleverBox. Ліміт 1 запит/сек спільний для ВСІХ клієнтів у процесі
        (плановий синк, вебхуки, кнопки, запис), а при «no more than 1 request per second»
        запит повторюється з паузою — помилка не доходить до синку."""
        url = f"{self.base}/{method.lstrip('/')}"
        for attempt in range(6):
            with _RATE_LOCK:
                wait = settings.cleverbox_min_interval_sec - (time.monotonic() - _RATE["last"])
                if wait > 0:
                    time.sleep(wait)
                try:
                    r = self.http.post(url, json=body or {})
                finally:
                    _RATE["last"] = time.monotonic()
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in ERRORS:
                raise CleverBoxError(f"{method}: {ERRORS[r.status_code]}")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("ok") is False:
                err = data.get("error")
                if _is_rate_limited(err) and attempt < 5:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise CleverBoxError(f"{method}: {ERRORS.get(err, err) if isinstance(err, int) else err}")
            return data
        raise CleverBoxError(f"{method}: CleverBox перевантажений (ліміт запитів), спробуйте пізніше")

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


def _num_val(c: dict, keys) -> float | None:
    v = _first_val(c, keys)
    try:
        return float(str(v).replace(",", ".").replace(" ", "")) if v is not None else None
    except ValueError:
        return None


def _flat(v):
    """Значення картки для показу: список/словник → рядок."""
    if isinstance(v, dict):
        if "name" in v or "value" in v:
            return v.get("value") if v.get("value") not in (None, "") else v.get("name")
        return ", ".join(f"{k}: {x}" for k, x in v.items() if x not in (None, "", [], {}))
    if isinstance(v, list):
        return ", ".join(str(_flat(x)) for x in v if x not in (None, "", [], {}))
    return v


GENDER_KEYS = ("sex", "gender")
ADDRESS_KEYS = ("address", "city", "addr")
SOURCE_KEYS = ("source", "source_name", "ad_source", "advert", "advertising", "where_from", "referral", "how_found")
GROUP_KEYS = ("groups", "group", "categories", "category", "category_name", "tags", "client_groups")
BALANCE_KEYS = ("balance", "deposit")
BONUS_KEYS = ("bonus", "bonuses", "bonus_balance")
TOTAL_KEYS = ("ltv", "total_paid", "total_payd", "total_sum")
VISITS_KEYS = ("visits", "visits_count", "count_visits", "visit_count")
AVERAGE_KEYS = ("average_check", "avg_check")
LAST_VISIT_KEYS = ("last_visit", "date_last_visit", "last_visit_date", "last_date")
# що вже лягає в окремі поля CRM — у «інші поля» не дублюємо
_KNOWN = {"id", "name", "lname", "fname", "sname", "full_name", "phone", "phones", "email",
          *REGISTERED_KEYS, *BIRTHDAY_KEYS}


def apply_client_fields(p: Patient, c: dict) -> None:
    """Поля картки CleverBox → картка CRM. Числа CleverBox (баланс, бонуси, LTV) оновлюються завжди;
    текстові поля (стать, адреса, джерело, групи) — лише якщо в CRM порожньо, щоб не затерти ручні правки.
    Усе інше, що віддає API, зберігається як є в cb_extra і видно в картці пацієнта."""
    reg = parse_dt(_first_val(c, REGISTERED_KEYS))
    if reg and reg.year > 2000:
        p.cb_registered_at = reg
    if not p.birthday:
        b = parse_dt(_first_val(c, BIRTHDAY_KEYS))
        if b and 1920 < b.year <= date.today().year:
            p.birthday = b.date()
    if not p.email and isinstance(c.get("email"), str) and "@" in c["email"]:
        p.email = c["email"].strip()[:200]
    ph = c.get("phone") or _flat(c.get("phones"))
    if not p.phone and ph and len(re.sub(r"\D", "", str(ph))) >= 9:
        p.phone = str(ph).strip()[:64]
    g = str(_first_val(c, GENDER_KEYS) or "").strip().lower()
    if not p.gender and g:
        p.gender = "жіноча" if g in ("f", "w", "female", "woman", "ж", "жін", "жіноча", "жiноча") else \
            "чоловіча" if g in ("m", "male", "man", "ч", "чол", "чоловіча") else None
    if not p.address and _first_val(c, ADDRESS_KEYS):
        p.address = str(_flat(_first_val(c, ADDRESS_KEYS)))[:300]
    if not p.source and _first_val(c, SOURCE_KEYS):
        p.source = str(_flat(_first_val(c, SOURCE_KEYS)))[:100]
    grp = _first_val(c, GROUP_KEYS)
    if not p.client_groups and grp:
        items = grp if isinstance(grp, list) else str(_flat(grp)).split(",")
        p.client_groups = [str(_flat(x)).strip() for x in items if str(_flat(x)).strip()][:20] or None
    for attr, keys in (("cb_balance", BALANCE_KEYS), ("cb_bonus", BONUS_KEYS), ("cb_total", TOTAL_KEYS),
                       ("cb_average", AVERAGE_KEYS)):
        v = _num_val(c, keys)
        if v is not None:
            setattr(p, attr, v)
    v = _num_val(c, VISITS_KEYS)
    if v is not None:
        p.cb_visits = int(v)
    lv = parse_dt(_first_val(c, LAST_VISIT_KEYS))
    if lv and lv.year > 2000:
        p.cb_last_visit = lv
    if str(c.get("vip") or c.get("is_vip") or "").lower() in ("1", "true", "yes"):
        p.cb_mark = "vip"
    extra = {}
    for k, v in c.items():
        if k in _KNOWN or v in (None, "", [], {}, "0000-00-00", "00.00.0000"):
            continue
        fv = _flat(v)
        if fv not in (None, ""):
            extra[k] = fv if isinstance(fv, (int, float, bool)) else str(fv)[:500]
    p.cb_extra = extra or None
    p.cb_synced_at = datetime.now()


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
    from ..services.patient_match import score, digits, AUTO
    by_cb = {p.cleverbox_id: p for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    n, keys = 0, set()
    unlinked = []
    for c in cb.clients():
        keys.update(c.keys())
        p = by_cb.get(int(c.get("id") or 0))
        if p:
            apply_client_fields(p, c)
            n += 1
        else:
            unlinked.append(c)
    # картки CRM без CleverBox (напр. створені з PDF плану): привʼязуємо за телефоном або однозначним ПІБ,
    # якщо цього клієнта CleverBox ще немає в CRM (інакше це дубль — об'єднання кнопкою в картці)
    orphans = db.scalars(select(Patient).where(Patient.cleverbox_id.is_(None))).all()
    if orphans and unlinked:
        by_phone = {}
        for c in unlinked:
            d = digits(str(c.get("phone") or ""))
            if len(d) == 9:
                by_phone.setdefault(d, []).append(c)
        for p in orphans:
            cands = by_phone.get(digits(p.phone), []) if p.phone and len(digits(p.phone)) == 9 else []
            if not cands:
                full = lambda c: " ".join(str(c.get(k) or "") for k in ("name", "lname", "fname", "sname")).strip() or str(c.get("full_name") or "")  # noqa: E731
                hits = [(score(p.full_name, full(c)), c) for c in unlinked]
                best = max((h for h, _ in hits), default=0)
                cands = [c for h, c in hits if h >= AUTO and h == best]
            if len(cands) == 1:
                cid = int(cands[0].get("id") or 0)
                if cid and cid not in by_cb:
                    p.cleverbox_id = cid
                    by_cb[cid] = p
                    apply_client_fields(p, cands[0])
                    n += 1
    _state(db, "cleverbox_client_fields", ",".join(sorted(keys))[:1000])   # лише назви полів, без значень
    db.commit()
    return n


def visit_end(v: dict, start: datetime) -> datetime | None:
    """Кінець запису: date_end / тривалість у хвилинах; неправдоподібне відкидаємо."""
    end = parse_dt(v.get("date_end") or v.get("date_finish") or v.get("date_stop") or v.get("end"))
    if not end:
        try:
            mins = int(v.get("duration") or v.get("time") or 0)
        except (TypeError, ValueError):
            mins = 0
        end = start + timedelta(minutes=mins) if 0 < mins < 600 else None
    if end and not (start < end <= start + timedelta(hours=10)):
        end = None
    return end


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


STAFF_FIRED_KEYS = ("fired", "is_fired", "dismissed", "is_dismissed", "deleted", "is_deleted", "archive",
                    "archived", "is_archive", "blocked", "is_blocked")
STAFF_ACTIVE_KEYS = ("active", "is_active", "enabled", "works", "is_work", "visible")
STAFF_POSITION_KEYS = ("position", "post", "speciality", "specialty", "specialization", "job", "role_name", "role")


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "так")


def _falsy(v) -> bool:
    return str(v).strip().lower() in ("0", "false", "no", "n", "ні")


def staff_is_fired(s: dict) -> bool:
    if any(_truthy(s.get(k)) for k in STAFF_FIRED_KEYS if s.get(k) not in (None, "")):
        return True
    if any(_falsy(s.get(k)) for k in STAFF_ACTIVE_KEYS if s.get(k) not in (None, "")):
        return True
    st = str(s.get("status") or s.get("_status") or "").lower()
    return bool(re.search(r"звільн|уволен|fired|dismiss|архів|архив", st))


def staff_name(s: dict) -> str:
    return " ".join(filter(None, [str(s.get("name") or "").strip(), str(s.get("lname") or "").strip()])).strip()


def same_person(a: str | None, b: str | None) -> bool:
    """«Дарʼя Колган» ≈ «Колган Дарʼя» ≈ «Дарʼя Колган (косметолог)»."""
    ta, tb = set(norm(a or "").split()), set(norm(b or "").split())
    if not ta or not tb:
        return False
    return ta == tb or (len(ta) >= 2 and ta <= tb) or (len(tb) >= 2 and tb <= ta)


def match_doctor(name: str | None, doctors) -> "Doctor | None":
    if not name:
        return None
    for d in doctors:
        if same_person(d.name, name):
            return d
    return None


def sync_staff(db: Session, cb: CleverBox) -> int:
    """Спеціалісти з CleverBox: привʼязка до наявних карток, нові картки, статус «працює / звільнений».
    Хто є у /staff і не позначений звільненим — працює. Майстри з історії візитів, яких уже немає
    у /staff, — звільнені. Ручно змінений статус (status_locked) синк не перезаписує."""
    try:
        staff = cb.staff()
    except CleverBoxError as e:              # модуль співробітників може бути недоступний — працюємо з історії візитів
        log.warning("CleverBox /staff: %s", e)
        staff = []
    keys = set()
    for s in staff:
        keys.update(s.keys())
    _state(db, "cleverbox_staff_fields", ",".join(sorted(keys))[:1000])      # лише назви полів
    doctors = db.scalars(select(Doctor)).all()
    by_cb = {d.cleverbox_id: d for d in doctors if d.cleverbox_id}
    now = datetime.utcnow()
    n = 0
    seen = set()
    for s in staff:
        try:
            sid = int(s["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = staff_name(s)
        if not name:
            continue
        doc = by_cb.get(sid) or next((d for d in doctors if not d.cleverbox_id and same_person(d.name, name)), None)
        if not doc:
            if db.scalar(select(Doctor).where(Doctor.name == name)):
                name = f"{name} #{sid}"
            doc = Doctor(name=name, source="cleverbox", active=True, in_calendar=True)
            db.add(doc)
            doctors.append(doc)
            n += 1
        elif not doc.cleverbox_id:
            n += 1
        doc.cleverbox_id = sid
        by_cb[sid] = doc
        seen.add(sid)
        doc.source = doc.source or "cleverbox"
        doc.cb_seen_at = now
        if s.get("email") and not doc.email:
            doc.email = str(s["email"]).strip()[:200]
        phone = s.get("phone") or s.get("mobile")
        if phone and not doc.phone:
            doc.phone = str(phone).strip()[:64]
        img = s.get("img") or s.get("photo") or s.get("avatar")
        if isinstance(img, str) and img.startswith("http") and not (doc.extra or {}).get("avatar_path"):   # завантажений у CRM аватар не перезаписуємо
            doc.photo_url = img[:500]
        pos = next((s.get(k) for k in STAFF_POSITION_KEYS if isinstance(s.get(k), str) and s.get(k).strip()), None)
        if pos and not doc.position:
            doc.position = pos.strip()[:200]
        if not doc.status_locked:
            fired = staff_is_fired(s)
            if fired and doc.active:
                doc.fired_at = date.today()
            doc.active = not fired
            if not fired:
                doc.fired_at = None
    # Майстри з історії візитів. Якщо CleverBox віддав список співробітників — кого в ньому немає,
    # той звільнений. Якщо списку немає — працює той, хто мав візити за 60 днів або має записи наперед.
    if staff:
        for d in doctors:
            if d.cleverbox_id and d.cleverbox_id not in seen and d.active and not d.status_locked:
                d.active, d.fired_at = False, date.today()
    recent_from = datetime.now() - timedelta(days=60)
    last_seen: dict[str, datetime] = {}
    for m, mx in db.execute(select(Visit.master_name, func.max(Visit.starts_at))
                            .where(Visit.master_name.is_not(None)).group_by(Visit.master_name)):
        m = (m or "").strip()
        if m and "," not in m:
            last_seen[m] = max(filter(None, [last_seen.get(m), mx]), default=None)
    for m, mx in sorted(last_seen.items()):
        recent = bool(mx and mx >= recent_from)
        d = match_doctor(m, doctors)
        if d:
            if not staff and not d.cleverbox_id and not d.status_locked and recent and not d.active:
                d.active, d.fired_at = True, None
            continue
        if db.scalar(select(Doctor).where(Doctor.name == m[:200])):
            continue
        active = recent and not staff
        d = Doctor(name=m[:200], source="cleverbox", active=active, in_calendar=True,
                   fired_at=None if active else date.today())
        db.add(d)
        doctors.append(d)
        n += 1
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


def visit_services(v: dict) -> list[dict]:
    """Послуги запису: CleverBox віддає їх у різних полях залежно від версії/методу."""
    for k in ("services", "service", "services_list", "items"):
        x = v.get(k)
        if isinstance(x, list) and x:
            return [i if isinstance(i, dict) else {"id": i} for i in x]
        if isinstance(x, dict) and x:
            return [x]
    if v.get("service_id"):
        return [{"id": v.get("service_id"), "name": v.get("service_name")}]
    return [{}]


def svc_id(s: dict) -> int | None:
    for k in ("id", "service_id", "item_id"):
        try:
            if s.get(k):
                return int(s[k])
        except (TypeError, ValueError):
            continue
    return None


def svc_name(s: dict, names: dict, sid: int | None) -> str | None:
    for k in ("name", "title", "service_name", "item_name"):
        if s.get(k):
            return str(s[k])[:300]
    return names.get(sid) if sid else None


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
            end = visit_end(v, start)
            for i, s in enumerate(visit_services(v)):
                sid = svc_id(s)
                ext = f"{v['id']}-{sid or i}"
                seen.add(ext)
                obj = db.scalar(select(Visit).where(Visit.source == "cb_visit", Visit.external_id == ext))
                if not obj:
                    obj = Visit(source="cb_visit", external_id=ext, patient_id=p.id)
                    db.add(obj)
                obj.service_id, obj.service_name = sid, svc_name(s, names, sid)
                obj.master_name = s.get("master") or s.get("master_name") or v.get("master")
                obj.starts_at, obj.status, obj.status_label = start, "booked", label
                obj.ends_at = end
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
    n, keys = 0, set()
    from ..services.catalog import warehouse_of
    for it in cb.products_list():
        pid = int(it["item_id"])
        prod = db.scalar(select(Product).where(Product.cb_product_id == pid))
        if not prod:
            prod = Product(cb_product_id=pid, name=it.get("name") or "", name_norm="")
            db.add(prod)
        prod.name = it.get("name") or prod.name
        prod.name_norm = norm(prod.name)
        prod.category = it.get("category_name")
        wh = warehouse_of(prod.category) or warehouse_of(_first(it, "direction_name", "direction"))
        if wh and not prod.warehouse:
            prod.warehouse = wh
        if it.get("for_sale") is not None:
            prod.for_sale = bool(it.get("for_sale")) or bool(it.get("for_shop"))
        if not prod.warehouse and prod.for_sale is False:
            prod.warehouse = "single"                   # матеріал «для роботи» без категорії — до одноразки
        unit = _first(it, "unit", "unit_name", "unit_write_off")
        if isinstance(unit, str) and unit.strip() and not prod.unit:
            prod.unit = unit.strip().rstrip(".")[:20]
        prod.price = float(_first(it, "sale_price", "base_price", default=0) or 0) or prod.price
        prod.cost = float(it["cost"]) if it.get("cost") not in (None, "") else prod.cost
        # бренд / артикул / постачальник — якщо CleverBox їх віддає (ручні значення не затираємо)
        brand = _first(it, "brand", "brand_name", "manufacturer", "manufacturer_name", "producer", "vendor")
        sku = _first(it, "article", "sku", "vendor_code", "code", "articul", "barcode")
        supplier = _first(it, "supplier", "supplier_name", "provider", "provider_name")
        if isinstance(brand, str) and brand.strip() and not prod.brand:
            prod.brand = brand.strip()[:120]
        if sku not in (None, "") and not prod.sku:
            prod.sku = str(sku).strip()[:100]
        if isinstance(supplier, str) and supplier.strip() and not prod.supplier:
            prod.supplier = supplier.strip()[:200]
        keys.update(it.keys())
        n += 1
    _state(db, "cleverbox_product_fields", ",".join(sorted(keys))[:1000])   # лише назви полів
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
    sample_saved = False
    for i in range(days):
        day = today + timedelta(days=i)
        for v in cb.visits_on(day):
            if not sample_saved:                                   # назви полів запису — для діагностики (без значень)
                sv = v.get("services")
                _state(db, "cleverbox_visit_fields", ("visit: " + ",".join(sorted(v.keys())) + " | service: " +
                       (",".join(sorted(sv[0].keys())) if isinstance(sv, list) and sv and isinstance(sv[0], dict) else type(sv).__name__))[:1000])
                sample_saved = True
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
            end = visit_end(v, start)
            status = "attended" if DONE_RE.search(label) else "booked"   # «attended» не рахується в реалізацію —
            for j, s in enumerate(visit_services(v)):                   # факт послуги береться з /services/sales
                sid = svc_id(s)
                ext = f"{v['id']}-{sid or j}"
                seen.add(ext)
                obj = db.scalar(select(Visit).where(Visit.source == "cb_visit", Visit.external_id == ext))
                if not obj:
                    obj = Visit(source="cb_visit", external_id=ext, patient_id=p.id)
                    db.add(obj)
                obj.service_id, obj.service_name = sid, svc_name(s, names, sid) or obj.service_name
                obj.master_name = s.get("master") or s.get("master_name") or v.get("master") or obj.master_name
                obj.starts_at, obj.status, obj.status_label = start, status, label
                obj.ends_at = end
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
    last_staff = _state(db, "last_staff_sync")
    if initial or not db.scalar(select(CbService).limit(1)) or not last_staff \
            or datetime.fromisoformat(last_staff) < datetime.now() - timedelta(hours=20):
        report["services"] = sync_services(db, cb)
        try:
            report["staff"] = sync_staff(db, cb)
        except Exception as e:              # noqa: BLE001 — спеціалісти не мають зупиняти синк візитів
            log.exception("sync_staff")
            report["staff"] = f"помилка: {e}"
        _state(db, "last_staff_sync", datetime.now().isoformat(timespec="seconds"))
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
    try:                                      # рух коштів (cashflow): метод шукається сам, раз на добу, поки не знайдено
        from .cb_finance import sync_cash, probe
        meth, last_probe = _state(db, "cleverbox_finance_method"), _state(db, "cleverbox_finance_probe_at")
        if not meth and (not last_probe or datetime.fromisoformat(last_probe) < datetime.now() - timedelta(hours=20)):
            probe(db, cb)
            _state(db, "cleverbox_finance_probe_at", datetime.now().isoformat(timespec="seconds"))
            db.commit()
        report["cashflow"] = sync_cash(db, end - timedelta(days=7), end, cb)
    except Exception as e:                    # noqa: BLE001 — фінанси не мають зупиняти синк
        log.warning("cashflow sync: %s", e)
        report["cashflow"] = f"помилка: {e}"
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
