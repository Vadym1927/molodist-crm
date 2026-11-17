"""Автознижка на товари з коротким терміном придатності (за замовчуванням: вітрина, термін < 12 міс → −25%).

Знижка діє лише на одиниці з короткої партії:
• каса CRM — коли в рядку обрано термін (партію), що спливає раніше ніж за N місяців; знижка на к-сть цієї партії;
• сайт — товар показується зі знижкою, поки на складі є короткі одиниці; у кошику знижка лише на їхню к-сть,
  решта — за повною ціною; при оплаті продаж списує саме коротку партію, тож після викупу знижка зникає сама.
Протерміновані партії знижку не отримують (їх треба списати).
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from ..models import Product, SyncState

KEY = "short_expiry"
DEFAULTS = {"on": True, "months": 12, "pct": 25, "whs": ["vitrina"]}
LABEL = "Короткий термін придатності"


def cfg(db: Session) -> dict:
    r = db.get(SyncState, KEY)
    try:
        c = json.loads(r.value) if r and r.value else {}
    except ValueError:
        c = {}
    return {**DEFAULTS, **c}


def save(db: Session, d: dict) -> dict:
    def num(k, lo, hi, dv):
        try:
            return max(lo, min(hi, float(str(d.get(k) or dv).replace(",", "."))))
        except ValueError:
            return dv
    whs = [w for w in (d.get("whs") or []) if w] or ["vitrina"]
    c = {"on": bool(d.get("on")), "months": int(num("months", 1, 36, 12)), "pct": num("pct", 1, 90, 25), "whs": whs}
    db.merge(SyncState(key=KEY, value=json.dumps(c, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    return c


def edge(c: dict, today: date | None = None) -> date:
    t = today or date.today()
    m = t.month - 1 + int(c.get("months") or 12)
    y, m = t.year + m // 12, m % 12 + 1
    import calendar
    return date(y, m, min(t.day, calendar.monthrange(y, m)[1]))


def is_short(c: dict, expiry: date | None, today: date | None = None) -> bool:
    t = today or date.today()
    return bool(c.get("on") and expiry and t <= expiry < edge(c, t))


def label(c: dict) -> str:
    return f"{LABEL} −{c['pct']:g}%"


def short_stock(db: Session, c: dict | None = None) -> dict[int, dict]:
    """{товар: {qty, expiry (найближчий), lots: [(термін, к-сть)]}} — короткі партії на складах правила."""
    from .stock import batch_balances, ledger_active
    c = c or cfg(db)
    if not c.get("on") or not ledger_active(db):
        return {}
    out: dict[int, dict] = {}
    for (pid, wh, e), q in batch_balances(db).items():
        if wh in c["whs"] and q > 1e-6 and is_short(c, e):
            r = out.setdefault(pid, {"qty": 0.0, "expiry": e, "lots": []})
            r["qty"] = round(r["qty"] + q, 3)
            r["lots"].append((e, q))
            r["expiry"] = min(r["expiry"], e)
    for r in out.values():
        r["lots"].sort()
        r["qty"] = int(r["qty"] + 1e-6)                       # продаємо цілими одиницями
    return {k: v for k, v in out.items() if v["qty"] >= 1}


def price(c: dict, full: float) -> float:
    return float(round(full * (1 - float(c["pct"]) / 100)))


def pos_lines(db: Session, items: list[dict]) -> list[float]:
    """Каса: знижка (₴) по рядках, де обрано коротку партію складу правила; на к-сть не більшу за залишок партії."""
    from .stock import batch_balances
    c = cfg(db)
    out = [0.0] * len(items)
    if not c.get("on"):
        return out
    left: dict = {}
    for i, it in enumerate(items):
        e = it.get("expiry")
        if it.get("kind") != "product" or not it.get("ref_id") or not e:
            continue
        if isinstance(e, str):
            try:
                e = date.fromisoformat(e[:10])
            except ValueError:
                continue
        if not is_short(c, e):
            continue
        p = db.get(Product, it["ref_id"])
        wh = it.get("warehouse") or (p.warehouse if p else None) or "vitrina"
        if wh not in c["whs"]:
            continue
        k = (it["ref_id"], wh, e)
        if k not in left:
            left[k] = max(0.0, batch_balances(db, it["ref_id"], wh).get(k, 0.0))
        q = min(float(it.get("qty") or 1), left[k])
        left[k] -= q
        out[i] = round(float(it.get("price") or 0) * q * float(c["pct"]) / 100, 2)
    return out
