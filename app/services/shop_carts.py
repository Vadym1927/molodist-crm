"""Покинуті кошики: кошик покупця, що увійшов у кабінет, зберігається на сервері.

Не оформив за N годин → видно в центрі магазину (покупець, товари, сума, скільки висить), задача адміністраторам,
нагадування покупцю (бот турботи → SMS), дзвінок. Оформив замовлення — кошик закривається сам.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Patient, ShopAccount, ShopCart, Task

STATUS = {"open": "Не оформлено", "ordered": "Оформлено замовлення", "done": "Опрацьовано", "empty": "Очищено"}
DEFAULT_TEXT = "{name}, ви залишили в кошику MOLODIST: {items}. Кошик збережено — оформити можна тут: {link}. Питання? Напишіть нам."


def save(db: Session, pid: int, items: list, data: dict) -> ShopCart:
    from . import shop_front as sf
    clean = []
    for i in items or []:
        try:
            q = max(1, min(99, int(i.get("qty") or 1)))
            clean.append({"id": int(i.get("id")), "qty": q})
        except (TypeError, ValueError, AttributeError):
            continue
    clean = clean[:40]
    cart = db.scalar(select(ShopCart).where(ShopCart.patient_id == pid))
    if not cart:
        cart = ShopCart(patient_id=pid, status="empty")
        db.add(cart)
    if [(x["id"], x["qty"]) for x in cart.items or []] == [(x["id"], x["qty"]) for x in clean] and cart.status != "empty":
        return cart
    total = 0.0
    if clean:
        try:
            total = float(sf.price_cart(db, clean).get("total") or 0)
        except Exception:  # noqa: BLE001
            total = 0.0
    if clean and cart.status != "open":
        cart.task_id, cart.reminded_at = None, None
    cart.items, cart.total, cart.updated_at = clean, total, datetime.utcnow()
    cart.status = "open" if clean else "empty"
    db.flush()
    return cart


def ordered(db: Session, pid: int | None) -> None:
    if not pid:
        return
    cart = db.scalar(select(ShopCart).where(ShopCart.patient_id == pid))
    if cart and cart.status == "open":
        cart.status, cart.items, cart.total = "ordered", [], 0
        _close_task(db, cart, "Покупець оформив замовлення")
        db.flush()


def _close_task(db: Session, cart: ShopCart, result: str) -> None:
    t = db.get(Task, cart.task_id) if cart.task_id else None
    if t and t.status == "open":
        t.status, t.result, t.done_at = "done", result, datetime.utcnow()


def age_text(dt: datetime | None) -> str:
    if not dt:
        return "—"
    m = int((datetime.utcnow() - dt).total_seconds() // 60)
    if m < 60:
        return f"{m} хв"
    if m < 48 * 60:
        return f"{m // 60} год"
    return f"{m // 1440} дн"


def rows(db: Session, data: dict, status: str = "open", min_hours: float = 0) -> list[dict]:
    q = select(ShopCart).order_by(ShopCart.updated_at.desc()).limit(300)
    if status in STATUS:
        q = q.where(ShopCart.status == status)
    carts = list(db.scalars(q))
    pts = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_([c.patient_id for c in carts] or [0])))}
    accs = {a.patient_id: a for a in db.scalars(select(ShopAccount).where(ShopAccount.patient_id.in_([c.patient_id for c in carts] or [0])))}
    by_id = {v["id"]: (it, v) for it in data["items"] for v in it["variants"]}
    out = []
    edge = datetime.utcnow() - timedelta(hours=min_hours)
    for c in carts:
        if status == "open" and c.updated_at and c.updated_at > edge:
            continue
        items = []
        for i in c.items or []:
            it, v = by_id.get(i["id"], (None, None))
            items.append({"name": (v or {}).get("name") or f"товар #{i['id']}", "qty": i["qty"], "price": (v or {}).get("price") or 0,
                          "img": it["imgs"][0] if it and it.get("imgs") else "", "slug": it["slug"] if it else None, "stock": bool(v and v.get("stock"))})
        p = pts.get(c.patient_id)
        a = accs.get(c.patient_id)
        nm = " ".join(x for x in ((a.last_name if a else ""), (a.first_name if a else "")) if x) or (p.full_name if p else "")
        out.append({"cart": c, "patient": p, "name": nm, "phone": getattr(p, "phone", "") if p else "", "lines": items, "age": age_text(c.updated_at),
                    "task": db.get(Task, c.task_id) if c.task_id else None})
    return out


def _items_text(c: ShopCart, data: dict) -> str:
    by_id = {v["id"]: v for it in data["items"] for v in it["variants"]}
    names = [by_id[i["id"]]["name"] for i in c.items or [] if i["id"] in by_id]
    if not names:
        return "товари"
    t = names[0][:50] + (f" та ще {len(names) - 1}" if len(names) > 1 else "")
    return t


def message(db: Session, cfg: dict, c: ShopCart, data: dict, root: str) -> str:
    a = db.scalar(select(ShopAccount).where(ShopAccount.patient_id == c.patient_id))
    nm = (a.first_name if a and a.first_name else "") or "Вітаємо"
    tpl = (cfg.get("cart_text") or "").strip() or DEFAULT_TEXT
    return tpl.replace("{name}", nm).replace("{items}", _items_text(c, data)).replace("{sum}", f"{int(c.total or 0)} ₴").replace("{link}", f"{root}/checkout/")[:480]


def remind(db: Session, cfg: dict, cart_id: int, data: dict, root: str, who: str = "") -> str:
    from . import shop_cabinet as sc
    c = db.get(ShopCart, cart_id)
    if not c or c.status != "open" or not c.items:
        raise ValueError("Кошик уже не активний.")
    p = db.get(Patient, c.patient_id)
    if not p or not getattr(p, "phone", None):
        raise ValueError("У картці немає телефону.")
    ch = sc._deliver(db, sc._phone(p.phone), message(db, cfg, c, data, root), "🛒 ")
    c.reminded_at = datetime.utcnow()
    c.note = ((c.note or "") + f"\n{datetime.now():%d.%m %H:%M} нагадування ({ch}){' — ' + who if who else ''}").strip()[-2000:]
    db.flush()
    return ch


def mark_done(db: Session, cart_id: int, note: str, who: str = "") -> None:
    c = db.get(ShopCart, cart_id)
    if not c:
        return
    c.status = "done"
    c.note = ((c.note or "") + f"\n{datetime.now():%d.%m %H:%M} опрацьовано{' — ' + who if who else ''}: {note}".rstrip(": ")).strip()[-2000:]
    _close_task(db, c, note or "Опрацьовано")
    db.flush()


def job(db: Session, cfg: dict, data: dict, root: str) -> dict:
    """Щогодини: задача адміністраторам через N годин, авто-нагадування покупцю через M годин (якщо ввімкнено)."""
    rep = {"tasks": 0, "reminded": 0}
    hours = float(cfg.get("cart_task_hours") or 0)
    auto = float(cfg.get("cart_auto_hours") or 0)
    now = datetime.utcnow()
    carts = db.scalars(select(ShopCart).where(ShopCart.status == "open")).all()
    from ..integrations import messaging as m
    quiet = m.is_quiet_time()
    for c in carts:
        if not c.items or not c.updated_at:
            continue
        age = (now - c.updated_at).total_seconds() / 3600
        if hours and age >= hours and not c.task_id:
            p = db.get(Patient, c.patient_id)
            t = Task(title=f"Покинутий кошик: {int(c.total or 0)} ₴ — {(p.full_name if p else '')[:80]}",
                     description="Покупець поклав товари в кошик і не оформив. Звʼязатися: уточнити, чи є питання, допомогти з вибором / доставкою. "
                                 "Кошик — у «Магазин → Покинуті кошики».",
                     kind="shop_cart", source_key=f"shop_cart:{c.id}:{c.updated_at:%Y%m%d%H%M}", patient_id=c.patient_id, assignee_role="admin",
                     due_date=date.today(), priority="high" if (c.total or 0) >= 3000 else "normal", created_at=now)
            db.add(t)
            db.flush()
            c.task_id = t.id
            rep["tasks"] += 1
        if cfg.get("cart_auto_on") and auto and age >= auto and not c.reminded_at and not quiet and age < 24 * 7:
            try:
                remind(db, cfg, c.id, data, root, "авто")
                rep["reminded"] += 1
            except Exception:  # noqa: BLE001
                pass
        db.commit()
    return rep
