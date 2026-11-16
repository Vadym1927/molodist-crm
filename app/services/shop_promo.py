"""Промокоди інтернет-магазину: перевірка, розрахунок знижки (пропорційно між рядками, як MOLODIST Coins), облік використань."""
from __future__ import annotations

import re
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ShopPromo, SiteOrder


def norm(code: str) -> str:
    return re.sub(r"[^A-Z0-9_-]", "", str(code or "").upper())[:40]


def _eligible(p: ShopPromo, items: list[dict]) -> list[dict]:
    brands = {b.strip().lower() for b in (p.brands or "").split(",") if b.strip()}
    return [i for i in items if (p.with_sale or not i.get("sale")) and (not brands or str(i.get("brand") or "").lower() in brands)]


def check(db: Session, code: str, items: list[dict], phone: str = "", coins: float = 0) -> dict:
    """{ok, code, amount, msg}. amount — знижка в гривнях (не більше суми товарів, на які діє, мінус уже списані Coins)."""
    c = norm(code)
    if not c:
        return {"ok": False, "code": "", "amount": 0, "msg": ""}
    p = db.scalar(select(ShopPromo).where(ShopPromo.code == c))
    today = date.today()
    bad = {"ok": False, "code": c, "amount": 0}
    if not p or not p.active:
        return {**bad, "msg": "Такого промокоду немає або він не активний."}
    if (p.starts and today < p.starts) or (p.ends and today > p.ends):
        return {**bad, "msg": "Термін дії промокоду минув або ще не почався."}
    if p.max_uses and (p.used or 0) >= p.max_uses:
        return {**bad, "msg": "Промокод уже використано максимальну кількість разів."}
    if phone and p.once_per_phone:
        rows = db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.phone == phone)).all()
        if any(((o.raw or {}).get("promo") or {}).get("code") == c and o.status not in ("Скасовано", "Фейкове") for o in rows):
            return {**bad, "msg": "Цей промокод ви вже використали."}
    el = _eligible(p, items)
    base = sum(float(i["total"]) for i in el)
    total = sum(float(i["total"]) for i in items)
    if p.min_sum and total < p.min_sum:
        return {**bad, "msg": f"Промокод діє від {int(p.min_sum)} ₴ у кошику."}
    if base <= 0:
        return {**bad, "msg": "Промокод не діє на товари в кошику" + ("" if p.with_sale else " (товари зі знижкою не враховуються)") + "."}
    amt = base * float(p.value) / 100 if p.kind == "pct" else float(p.value)
    amt = round(max(0.0, min(amt, base - float(coins or 0))), 0)
    if amt <= 0:
        return {**bad, "msg": "Знижка вже максимальна."}
    lbl = f"−{p.value:g}%" if p.kind == "pct" else f"−{int(p.value)} ₴"
    return {"ok": True, "code": c, "amount": amt, "msg": f"Промокод {c}: {lbl}", "id": p.id, "lines": [i["id"] for i in el]}


def split(items: list[dict], amount: float, lines: list[int]) -> list[float]:
    """Знижка промокоду пропорційно між рядками, на які він діє; сума точно = amount."""
    base = [float(i["total"]) if i["id"] in lines else 0.0 for i in items]
    tot = sum(base)
    if amount <= 0 or tot <= 0:
        return [0.0] * len(items)
    kop = int(round(amount * 100))
    raw = [b * kop / tot for b in base]
    out = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: -(raw[i] - out[i]))[:kop - sum(out)]:
        out[i] += 1
    return [x / 100 for x in out]


def used(db: Session, pid: int) -> None:
    p = db.get(ShopPromo, pid)
    if p:
        p.used = (p.used or 0) + 1


def save(db: Session, d: dict, pid: int | None = None) -> ShopPromo:
    code = norm(d.get("code"))
    if len(code) < 3:
        raise ValueError("Код — щонайменше 3 символи: латиниця, цифри, - або _.")
    p = db.get(ShopPromo, pid) if pid else None
    other = db.scalar(select(ShopPromo).where(ShopPromo.code == code))
    if other and (not p or other.id != p.id):
        raise ValueError("Такий промокод уже є.")
    if not p:
        p = ShopPromo(created_at=datetime.utcnow(), used=0)
        db.add(p)
    kind = "sum" if d.get("kind") == "sum" else "pct"
    try:
        val = float(str(d.get("value") or 0).replace(",", "."))
    except ValueError:
        val = 0
    if val <= 0 or (kind == "pct" and val > 90):
        raise ValueError("Знижка: від 1 до 90 % або сума в гривнях.")

    def _d(x):
        try:
            return date.fromisoformat(str(x)) if x else None
        except ValueError:
            return None

    def _n(x):
        try:
            return float(str(x).replace(",", ".")) if str(x or "").strip() else None
        except ValueError:
            return None
    p.code, p.kind, p.value = code, kind, val
    p.min_sum, p.max_uses = _n(d.get("min_sum")), int(_n(d.get("max_uses")) or 0) or None
    p.starts, p.ends = _d(d.get("starts")), _d(d.get("ends"))
    p.once_per_phone, p.with_sale, p.active = bool(d.get("once_per_phone")), bool(d.get("with_sale")), bool(d.get("active"))
    p.brands = str(d.get("brands") or "").strip()[:300] or None
    p.note = str(d.get("note") or "").strip()[:200] or None
    db.flush()
    return p


def stats(db: Session) -> dict[str, dict]:
    """По кожному коду: замовлень і виручка (не скасовані)."""
    out: dict[str, dict] = {}
    for o in db.scalars(select(SiteOrder).where(SiteOrder.source == "crm")):
        pr = (o.raw or {}).get("promo") or {}
        if pr.get("code") and o.status not in ("Скасовано", "Фейкове"):
            s = out.setdefault(pr["code"], {"n": 0, "sum": 0.0, "disc": 0.0})
            s["n"] += 1
            s["sum"] += float(o.total or 0)
            s["disc"] += float(pr.get("amount") or 0)
    return out
