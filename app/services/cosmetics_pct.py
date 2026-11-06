"""% лікарям від продажу косметики (Налаштування → Співробітники → Бонус за продаж косметики).
Загальне правило: X% без знижки, Y% зі знижкою, Z% якщо ціна товару менша за поріг; база — сума продажу або ціна.
Винятки (вищий пріоритет — вище): товар → бренд → лікар (свої % або «не нараховувати»).
Порожнє поле у винятку = береться з правила нижчого рівня."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SyncState

KEY = "cosmetics_pct"
DEFAULT = {"full": 10.0, "disc": 5.0, "min_price": 1000.0, "low": 0.0, "disc_from": 0.0, "base": "paid",
           "staff": {}, "brands": {}, "products": {}, "tiers": []}
BASES = {"paid": "від суми продажу (після знижки)", "price": "від ціни товару (без знижки)"}


def _k(s: str | None) -> str:
    return " ".join((s or "").lower().replace("ʼ", "'").replace("’", "'").split())


def _num(v, default=None):
    if v in (None, ""):
        return default
    try:
        return round(float(str(v).replace(",", ".")), 2)
    except ValueError:
        return default


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        raw = json.loads(st.value) if st and st.value else {}
    except ValueError:
        raw = {}
    return {**DEFAULT, **raw}


def save(db: Session, c: dict) -> None:
    out = {k: _num(c.get(k), DEFAULT[k]) for k in ("full", "disc", "min_price", "low", "disc_from")}
    out["base"] = c.get("base") if c.get("base") in BASES else "paid"
    tiers = []                                              # велика знижка: «знижка більше X% → Y%» (Y = 0 — не нараховувати)
    for t in c.get("tiers") or []:
        fr = _num(t.get("from"))
        if fr is None or fr <= 0:
            continue
        tiers.append({"from": min(fr, 100.0), "pct": 0.0 if t.get("off") else (_num(t.get("pct"), 0.0) or 0.0)})
    out["tiers"] = sorted({t["from"]: t for t in tiers}.values(), key=lambda t: t["from"])
    for grp in ("staff", "brands", "products"):
        g = {}
        for name, r in (c.get(grp) or {}).items():
            name = " ".join(str(name or "").split())[:200]
            if not name:
                continue
            row = {"full": _num(r.get("full")), "disc": _num(r.get("disc"))}
            if grp == "staff":
                row["off"] = bool(r.get("off"))
                row["min_price"] = _num(r.get("min_price"))
            if any(v not in (None, False) for v in row.values()):
                g[name] = row
        out[grp] = g
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(out, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


class Rules:
    """Обчислення % для рядка продажу (кешує налаштування й бренди товарів на один звіт)."""

    def __init__(self, db: Session):
        from ..models import Product
        self.c = cfg(db)
        self.staff = {_k(n): r for n, r in self.c["staff"].items()}
        self.brands = {_k(n): r for n, r in self.c["brands"].items()}
        self.products = {_k(n): r for n, r in self.c["products"].items()}
        self.brand_of_name, self.brand_of_id, self.name_of_id = {}, {}, {}
        for pid, nm, br in db.execute(select(Product.id, Product.name, Product.brand)):
            self.name_of_id[pid] = nm
            if br:
                self.brand_of_id[pid] = br
                self.brand_of_name.setdefault(_k(nm), br)

    def apply(self, x: dict) -> None:
        """x: staff, name, price, qty, paid, disc_pct, product_id? → дописує pct, sum, rule."""
        c, price, qty = self.c, float(x.get("price") or 0), float(x.get("qty") or 1)
        disc = float(x.get("disc_pct") or 0) > float(c["disc_from"] or 0) + 1e-9
        base = float(x.get("paid") or 0) if c["base"] == "paid" else round(price * qty, 2)
        st = self.staff.get(_k(x.get("staff")), {})
        nm = self.name_of_id.get(x.get("product_id")) or x.get("name")
        pr = self.products.get(_k(nm)) or self.products.get(_k(x.get("name")))
        brand = self.brand_of_id.get(x.get("product_id")) or self.brand_of_name.get(_k(nm))
        br = self.brands.get(_k(brand)) if brand else None
        kind = "disc" if disc else "full"
        lbl = "зі знижкою" if disc else "без знижки"
        dp = float(x.get("disc_pct") or 0)
        tier = next((t for t in reversed(c.get("tiers") or []) if dp > float(t["from"]) + 1e-9), None)
        if st.get("off"):
            pct, rule = 0.0, "лікарю не нараховується"
        elif tier:
            pct = float(tier["pct"])
            rule = f"знижка {dp:g}% > {tier['from']:g}% · " + (f"{pct:g}%" if pct else "не нараховується")
        elif pr and pr.get(kind) is not None:
            pct, rule = pr[kind], f"товар · {lbl}"
        elif br and br.get(kind) is not None:
            pct, rule = br[kind], f"бренд {brand} · {lbl}"
        else:
            mp = st.get("min_price") if st.get("min_price") is not None else c["min_price"]
            if mp and price < mp:
                pct, rule = float(c["low"] or 0), f"ціна < {mp:g} ₴"
            elif st.get(kind) is not None:
                pct, rule = st[kind], f"свій % лікаря · {lbl}"
            else:
                pct, rule = c[kind], lbl
        x["pct"], x["sum"], x["rule"], x["disc"] = pct, round(base * pct / 100, 2), rule, disc

    def describe(self) -> str:
        c = self.c
        s = (f"Відсоток лікарю з кожного проданого товару {BASES[c['base']]}: {c['full']:g}% без знижки, {c['disc']:g}% — зі знижкою"
             + (f" (понад {c['disc_from']:g}%)" if c["disc_from"] else "")
             + (f", {c['low']:g}% — якщо ціна товару менше {c['min_price']:g} ₴" if c["min_price"] else "")
             + "".join(f"; знижка більше {t['from']:g}% — " + (f"{t['pct']:g}%" if t["pct"] else "не нараховується") for t in c.get("tiers") or []) + ".")
        n = len(c["staff"]) + len(c["brands"]) + len(c["products"])
        if n:
            s += f" Винятків для лікарів, брендів і товарів: {n}."
        return s + " Змінюється в «Налаштування → Співробітники → Бонус за продаж косметики»."
