"""Подарункові сертифікати для продажу (як «Шаблони Gift Card» у CleverBox).

Шаблон: назва, номінал, термін дії (днів), ціна, опис, лише покупець, послуги, активний. Зберігається в SyncState gift_types.
Продаж у касі CRM (позиція kind=certificate, ref_id = id шаблону) → GiftCertificate у картці покупця: код, номінал, залишок,
діє до (дата продажу + днів), продавець. Оплата сертифікатом у касі: повна — чек без РРО, часткова — сума сертифіката
йде знижкою у фіскальний чек, решта фіскалізується. Нагадування адміністраторам (задача + Telegram) за N днів до закінчення."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import SyncState, GiftCertificate, Patient, Doctor, User

KEY = "gift_types"
CFG_KEY = "gift_cfg"
CRM_ID_FROM = 900000            # id сертифікатів, проданих у CRM (CleverBox-id менші — без колізій при імпорті)
DEFAULT_CFG = {"remind_days": 14, "notify_tg": True, "code_prefix": "MC", "next_no": 1, "desc": "Послуги краси"}
# шаблони CleverBox на 04.11.2026 (ID / код, назва, номінал, днів, ціна)
FISCAL_DEFAULT = "Послуги краси"       # назва позиції у фіскальному чеку Checkbox
USAGE = {"multi": "Багаторазовий", "single": "Одноразовий"}
SEED = [{"cb_id": 1030, "name": "Сертифікат 2000", "nominal": 2000, "days": 60, "price": 2000},
        {"cb_id": 1031, "name": "Сертифікат 3000", "nominal": 3000, "days": 60, "price": 3000},
        {"cb_id": 1033, "name": "Сертифікат 5000", "nominal": 5000, "days": 60, "price": 5000},
        {"cb_id": 1034, "name": "Сертифікат 10 000", "nominal": 10000, "days": 60, "price": 10000}]


def _get(db: Session, key: str):
    st = db.get(SyncState, key)
    if st and st.value:
        try:
            return json.loads(st.value)
        except ValueError:
            return None
    return None


def _put(db: Session, key: str, val) -> None:
    st = db.get(SyncState, key) or SyncState(key=key)
    st.value, st.updated_at = json.dumps(val, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def cfg(db: Session) -> dict:
    out = dict(DEFAULT_CFG)
    out.update(_get(db, CFG_KEY) or {})
    return out


def save_cfg(db: Session, data: dict) -> None:
    c = cfg(db)
    for k in ("remind_days", "next_no"):
        if data.get(k) is not None:
            c[k] = max(1, int(data[k]))
    if "notify_tg" in data:
        c["notify_tg"] = bool(data["notify_tg"])
    if data.get("code_prefix") is not None:
        c["code_prefix"] = str(data["code_prefix"]).strip()[:8] or "MC"
    if data.get("desc") is not None:
        c["desc"] = str(data["desc"]).strip()[:100]
    _put(db, CFG_KEY, c)


def items(db: Session) -> list[dict]:
    xs = _get(db, KEY)
    if xs is None:                                   # перший раз — шаблони з CleverBox
        xs = [{"id": i + 1, "active": True, "only_buyer": False, "services": "", "desc": DEFAULT_CFG["desc"], "usage": "multi", **s} for i, s in enumerate(SEED)]
        _put(db, KEY, xs)
    return xs


def active(db: Session) -> list[dict]:
    return [x for x in items(db) if x.get("active", True)]


def get(db: Session, tid: int | None) -> dict | None:
    return next((x for x in items(db) if tid and int(x["id"]) == int(tid)), None)


def save(db: Session, tid: int | None, data: dict) -> dict:
    xs = items(db)
    name = " ".join(str(data.get("name") or "").split())[:100]
    if not name:
        raise ValueError("Вкажіть назву шаблону")
    nominal = float(data.get("nominal") or 0)
    if nominal <= 0:
        raise ValueError("Номінал має бути більше 0")
    x = next((x for x in xs if tid and int(x["id"]) == int(tid)), None)
    if not x:
        x = {"id": max([int(y["id"]) for y in xs] + [0]) + 1}
        xs.append(x)
    x.update(name=name, nominal=round(nominal, 2), price=round(float(data.get("price") or nominal), 2),
             days=max(1, int(data.get("days") or 60)), desc=str(data.get("desc") or "").strip()[:200],
             only_buyer=bool(data.get("only_buyer")), services=str(data.get("services") or "").strip()[:500],
             usage="single" if data.get("usage") == "single" else "multi",
             active=bool(data.get("active", True)))
    if data.get("cb_id") is not None:
        x["cb_id"] = data["cb_id"]
    _put(db, KEY, xs)
    return x


def toggle(db: Session, tid: int) -> None:
    xs = items(db)
    for x in xs:
        if int(x["id"]) == int(tid):
            x["active"] = not x.get("active", True)
    _put(db, KEY, xs)


def delete(db: Session, tid: int) -> None:
    _put(db, KEY, [x for x in items(db) if int(x["id"]) != int(tid)])


# ---------------- продаж ----------------
def next_code(db: Session) -> str:
    c = cfg(db)
    n = int(c.get("next_no") or 1)
    code = f"{c.get('code_prefix') or 'MC'}-{n:06d}"
    while db.scalar(select(GiftCertificate.id).where(GiftCertificate.code == code)):
        n += 1
        code = f"{c.get('code_prefix') or 'MC'}-{n:06d}"
    c["next_no"] = n + 1
    _put(db, CFG_KEY, c)
    return code


def issue(db: Session, tpl: dict, patient: Patient | None, seller: Doctor | None, sale_id: int, paid: float,
          sold_at: datetime | None = None, recipient: str | None = None) -> GiftCertificate:
    """Виписати сертифікат за шаблоном при продажу в касі CRM."""
    sold_at = sold_at or datetime.now()
    mx = db.scalar(select(func.max(GiftCertificate.id))) or 0
    c = GiftCertificate(id=max(mx + 1, CRM_ID_FROM), code=next_code(db), type_name=tpl["name"],
                        patient_id=patient.id if patient else None, cb_client_id=patient.cleverbox_id if patient else None,
                        client_name=(patient.full_name if patient else None) or recipient, seller=seller.name if seller else None,
                        sold_at=sold_at, price=float(tpl.get("price") or tpl["nominal"]), paid=round(paid, 2),
                        nominal=float(tpl["nominal"]), balance=float(tpl["nominal"]),
                        valid_to=(sold_at + timedelta(days=int(tpl.get("days") or 60))).date(),
                        comment=(f"Продано в касі CRM, чек Ч-{sale_id}" + (f" · для {recipient}" if recipient else ""))[:500],
                        cb_status="Оплачено", single_use=tpl.get("usage") == "single")
    db.add(c)
    db.flush()
    return c


def fiscal_name(db: Session, tpl_id: int | None) -> str:
    """Назва сертифіката у фіскальному чеку (поле «Назва у фіскальному чеку» шаблону)."""
    t = get(db, tpl_id)
    return ((t or {}).get("desc") or "").strip() or FISCAL_DEFAULT


def sold_by_sale(db: Session, sale_id: int) -> list[GiftCertificate]:
    return db.scalars(select(GiftCertificate).where(GiftCertificate.comment.like(f"Продано в касі CRM, чек Ч-{sale_id}%"))).all()


def pos_items(db: Session) -> list[dict]:
    return [{"id": x["id"], "name": x["name"], "price": x.get("price") or x["nominal"], "nominal": x["nominal"], "days": x["days"],
             "desc": x.get("desc") or "", "usage": USAGE.get(x.get("usage") or "multi")} for x in active(db)]


# ---------------- нагадування ----------------
def expiring(db: Session, today: date | None = None) -> list[GiftCertificate]:
    """Сертифікати з залишком, що закінчуються за ≤ remind_days (з налаштувань)."""
    today = today or date.today()
    days = int(cfg(db).get("remind_days") or 14)
    out = [c for c in db.scalars(select(GiftCertificate).where(GiftCertificate.balance > 0, GiftCertificate.valid_to >= today,
                                                                GiftCertificate.valid_to <= today + timedelta(days=days)))
           if not (c.cb_status or "").lower().startswith("не оплач")]
    return sorted(out, key=lambda c: c.valid_to)


def admin_chats(db: Session) -> list[str]:
    return [u.telegram_chat_id for u in db.scalars(select(User).where(User.active.is_(True), User.role.in_(("admin", "owner")),
                                                                          User.telegram_chat_id.is_not(None)))]


def notify_expiring(db: Session, certs: list[GiftCertificate], base_url: str = "") -> int:
    """Telegram адміністраторам про сертифікати, що закінчуються (один раз на сертифікат і дату — SyncState gift_notified)."""
    if not certs or not cfg(db).get("notify_tg", True):
        return 0
    from . import alerts
    from html import escape
    done = set(_get(db, "gift_notified") or [])
    fresh = [c for c in certs if f"{c.id}:{c.valid_to:%Y%m%d}" not in done]
    if not fresh:
        return 0
    lines = []
    for c in fresh:
        who = c.patient.full_name if c.patient else (c.client_name or "—")
        lines.append(f"• №{escape(c.code or str(c.id))} {escape(c.type_name or '')}: {escape(who)} — залишок {c.balance:,.0f} ₴, "
                     f"до {c.valid_to:%d.%m.%Y} ({(c.valid_to - date.today()).days} дн.)".replace(",", " "))
    text = "🎁 <b>Сертифікати закінчуються</b> — запросіть пацієнтів:\n" + "\n".join(lines[:30]) + (f"\n… і ще {len(lines) - 30}" if len(lines) > 30 else "") + \
           (f'\n<a href="{base_url.rstrip("/")}/gifts?st=expiring">Усі сертифікати</a>' if base_url else "")
    n = 0
    from ..config import settings as _st
    extra = [c.strip() for c in (_st.telegram_alert_chat_id or "").split(",") if c.strip()]   # без окремої сесії (alerts.recipients)
    for ch in set(admin_chats(db) + extra):
        if alerts._api("sendMessage", data={"chat_id": ch, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"}):
            n += 1
    _put(db, "gift_notified", sorted(done | {f"{c.id}:{c.valid_to:%Y%m%d}" for c in fresh})[-2000:])
    return n
