"""Відгуки про товари магазину: запит після отримання замовлення (бот турботи → SMS), форма за посиланням, модерація, показ на сторінці товару.

Лише справжні відгуки покупців: форма відкривається за підписаним посиланням із конкретного замовлення або з кабінету.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ShopAccount, ShopReview, SiteOrder

DONE = ("Отримано", "Закрито")
STATUS = {"new": "Новий", "ok": "Опубліковано", "hidden": "Приховано"}
DEFAULT_TEXT = ("{name}, дякуємо за покупку в MOLODIST! Як вам {product}? Поставте оцінку й напишіть пару слів — "
                "це хвилина, а іншим покупцям допоможе з вибором: {link}")


def token(o: SiteOrder) -> str:
    sig = hmac.new(settings.secret_key.encode(), f"rv:{o.id}".encode(), hashlib.sha256).hexdigest()[:12]
    return f"{o.id}-{sig}"


def by_token(db: Session, tok: str) -> SiteOrder | None:
    try:
        oid, sig = str(tok or "").split("-", 1)
        o = db.get(SiteOrder, int(oid))
    except (ValueError, TypeError):
        return None
    return o if o and hmac.compare_digest(token(o), f"{oid}-{sig}") else None


def link(o: SiteOrder, root: str) -> str:
    return f"{root}/review/{token(o)}/"


def _pid(i: dict) -> int | None:
    return i.get("product_id") or None


def order_items(db: Session, o: SiteOrder, data: dict) -> list[dict]:
    """Товари замовлення для форми: найдорожчі першими (пріоритет — головна покупка), з позначкою, чи вже є відгук."""
    by_id = {v["id"]: it for it in data["items"] for v in it["variants"]}
    done = {r.product_id: r for r in db.scalars(select(ShopReview).where(ShopReview.order_id == o.id))}
    out = []
    for i in sorted(o.items or [], key=lambda x: -float(x.get("price") or 0)):
        pid = _pid(i)
        if not pid or any(x["id"] == pid for x in out):
            continue
        it = by_id.get(pid)
        out.append({"id": pid, "name": i.get("name") or (it or {}).get("name") or "", "img": (it or {}).get("imgs", [""])[0] if it and it.get("imgs") else "",
                    "slug": (it or {}).get("slug"), "review": done.get(pid)})
    return out


def first_name(o: SiteOrder, db: Session) -> str:
    acc = db.scalar(select(ShopAccount).where(ShopAccount.patient_id == o.patient_id)) if o.patient_id else None
    nm = (acc.first_name or "") if acc else ""
    if not nm:
        parts = (o.name or "").split()
        nm = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    return nm.strip()[:40] or "Вітаємо"


def save(db: Session, o: SiteOrder, product_id: int, rating: int, text: str, name: str) -> ShopReview:
    if product_id not in {_pid(i) for i in o.items or []}:
        raise ValueError("Цього товару немає в замовленні.")
    rating = int(rating or 0)
    if not 1 <= rating <= 5:
        raise ValueError("Поставте оцінку від 1 до 5.")
    r = db.scalar(select(ShopReview).where(ShopReview.order_id == o.id, ShopReview.product_id == product_id))
    if r and r.status == "ok":
        raise ValueError("Відгук на цей товар уже опубліковано. Дякуємо!")
    if not r:
        r = ShopReview(order_id=o.id, product_id=product_id, patient_id=o.patient_id, created_at=datetime.utcnow(), status="new")
        db.add(r)
    r.rating, r.text = rating, (text or "").strip()[:2000] or None
    r.name = (name or "").strip()[:60] or first_name(o, db)
    r.created_at = datetime.utcnow()
    db.flush()
    raw = dict(o.raw or {})
    raw["review_got"] = True
    o.raw = raw
    db.flush()
    return r


def published(db: Session, ids: list[int], limit: int = 30) -> dict:
    ids = list(ids) or [0]
    rows = db.scalars(select(ShopReview).where(ShopReview.product_id.in_(ids), ShopReview.status == "ok")
                      .order_by(ShopReview.created_at.desc()).limit(limit)).all()
    n = db.scalar(select(func.count()).select_from(ShopReview).where(ShopReview.product_id.in_(ids), ShopReview.status == "ok")) or 0
    avg = db.scalar(select(func.avg(ShopReview.rating)).where(ShopReview.product_id.in_(ids), ShopReview.status == "ok"))
    return {"rows": rows, "n": n, "avg": round(float(avg), 1) if avg else 0}


def ratings(db: Session) -> dict[int, tuple[float, int]]:
    """Середня оцінка й кількість опублікованих відгуків по товарах (для карток)."""
    q = select(ShopReview.product_id, func.avg(ShopReview.rating), func.count()).where(ShopReview.status == "ok").group_by(ShopReview.product_id)
    return {pid: (round(float(a), 1), n) for pid, a, n in db.execute(q)}


def moderate(db: Session, rid: int, status: str, answer: str | None, user_id: int | None) -> ShopReview | None:
    r = db.get(ShopReview, rid)
    if not r:
        return None
    if status in STATUS:
        r.status = status
    if answer is not None:
        r.answer = answer.strip()[:1000] or None
    r.moderated_by = user_id
    db.flush()
    return r


def award(db: Session, r: ShopReview, c: dict) -> bool:
    """MOLODIST Coins за опублікований відгук — один раз на відгук, за будь-яку оцінку (не лише позитивну)."""
    from . import coins as cn
    rl = cn.rules(c)
    amt = int(float(rl.get("coin_review") or 0))
    if r.status != "ok" or not r.patient_id or not amt or not rl.get("coin_on"):
        return False
    return cn.add(db, r.patient_id, amt, "review", f"rv:{r.id}", days=int(rl["coin_days"]), live=bool(rl["coin_spend_on"]))


def mark_due(o: SiteOrder, c: dict) -> None:
    """Замовлення отримано / закрито → запланувати запит відгуку через N днів (лише один раз)."""
    raw = dict(o.raw or {})
    if o.status in DONE and o.source == "crm" and not raw.get("review_due"):
        raw["review_due"] = (datetime.now() + timedelta(days=int(c.get("review_days") or 0))).isoformat(timespec="minutes")
        o.raw = raw


def message(db: Session, c: dict, o: SiteOrder, root: str, data: dict) -> str:
    items = order_items(db, o, data)
    prod = items[0]["name"] if items else "покупка"
    if len(prod) > 45:
        prod = prod[:44].rstrip() + "…"
    tpl = (c.get("review_text") or "").strip() or DEFAULT_TEXT
    bonus = int(float(c.get("coin_review") or 0)) if c.get("coin_on", True) else 0
    if bonus and "{coins}" not in tpl and "Coins" not in tpl:
        tpl += " За відгук — +{coins} MOLODIST Coins."
    return tpl.replace("{coins}", str(bonus)).replace("{name}", first_name(o, db)).replace("{product}", prod).replace("{n}", o.number or "").replace("{link}", link(o, root))[:480]


def short_sms(o: SiteOrder, root: str, db: Session | None = None) -> str:
    """Коротка SMS в 1 сегмент кирилиці (≤70 символів): без https:// і зайвих слів."""
    url = link(o, root).split("://", 1)[-1]
    nm = first_name(o, db) if db is not None else ""
    for t in ((f"{nm}, оцініть покупку в MOLODIST: {url}" if nm else ""), f"Оцініть покупку в MOLODIST: {url}", f"Оцініть покупку: {url}", url):
        if t and len(t) <= 70:
            return t
    return url


def _send(db: Session, c: dict, o: SiteOrder, text: str, root: str, phone: str | None = None) -> str:
    from . import shop_cabinet as sc
    return sc._deliver(db, sc._phone(phone or o.phone), text, "⭐ ", sms=short_sms(o, root, db), sms_ok=bool(c.get("review_sms")))


def send_due(db: Session, c: dict, root: str, data: dict) -> dict:
    rep = {"sent": 0, "failed": 0, "skipped": 0}
    if not c.get("review_on"):
        return rep
    from ..integrations import messaging as m
    if m.is_quiet_time():
        return rep
    from . import shop_cabinet as sc
    now = datetime.now().isoformat(timespec="minutes")
    rows = db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.status.in_(DONE)).order_by(SiteOrder.id.desc()).limit(300)).all()
    for o in rows:
        raw = o.raw or {}
        if not raw.get("review_due") or raw.get("review_sent") or raw.get("review_got") or raw["review_due"] > now or not o.phone:
            continue
        try:
            ch = _send(db, c, o, message(db, c, o, root, data), root)
        except Exception:  # noqa: BLE001
            rep["failed"] += 1
            continue
        raw = dict(raw)
        raw["review_sent"] = now
        if ch == "none":                                 # немає Telegram, SMS вимкнено — не надсилаємо й більше не пробуємо
            raw["review_skip"] = True
            raw["log"] = (raw.get("log") or []) + [{"at": now, "by": "", "text": "Запит відгуку не надіслано: у покупця немає Telegram, SMS вимкнено"}]
        else:
            raw["log"] = (raw.get("log") or []) + [{"at": now, "by": "", "text": f"Запит відгуку надіслано ({ch})"}]
        o.raw = raw
        db.commit()
        rep["skipped" if ch == "none" else "sent"] += 1
        if rep["sent"] >= 30:
            break
    return rep


def send_test(db: Session, c: dict, phone: str, root: str, data: dict) -> str:
    """Тестовий запит відгуку на вказаний номер: за останнім замовленням цього номера (або будь-яким останнім)."""
    from . import shop_cabinet as sc
    ph = sc._phone(phone)
    o = db.scalar(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.phone == ph).order_by(SiteOrder.id.desc()).limit(1)) \
        or db.scalar(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.items.is_not(None)).order_by(SiteOrder.id.desc()).limit(1))
    if not o:
        raise ValueError("Ще немає жодного замовлення з сайту — тест буде після першого.")
    return _send(db, c, o, "[ТЕСТ] " + message(db, c, o, root, data), root, phone=ph)


def admin_list(db: Session, status: str = "") -> list[ShopReview]:
    q = select(ShopReview).order_by(ShopReview.created_at.desc()).limit(300)
    if status in STATUS:
        q = q.where(ShopReview.status == status)
    return list(db.scalars(q))


def count_new(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(ShopReview).where(ShopReview.status == "new")) or 0
