"""Планування ціни реалізації: валютна закупівля × курс, РРЦ постачальника, маржа, рекомендована ціна."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from ..models import SyncState

log = logging.getLogger("pricing")
CURRENCIES = ("USD", "EUR", "UAH")
MARGINS = (60, 47, 40, 30)
NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"


def _get(db, key, default=None):
    st = db.get(SyncState, key)
    return st.value if st and st.value else default


def _set(db, key, value):
    st = db.get(SyncState, key) or SyncState(key=key)
    st.value, st.updated_at = value, datetime.utcnow()
    db.merge(st)


def rates(db: Session) -> dict:
    def f(k, d):
        try:
            return float(_get(db, k, d))
        except (TypeError, ValueError):
            return float(d)
    return {"USD": f("fx_usd", 41.5), "EUR": f("fx_eur", 48.0), "UAH": 1.0,
            "round": int(f("fx_round", 10)), "updated": _get(db, "fx_updated"), "source": _get(db, "fx_source", "вручну")}


def save_rates(db: Session, usd: float | None, eur: float | None, rnd: int | None, source: str = "вручну") -> None:
    if usd and usd > 0:
        _set(db, "fx_usd", str(round(usd, 4)))
    if eur and eur > 0:
        _set(db, "fx_eur", str(round(eur, 4)))
    if rnd is not None:
        _set(db, "fx_round", str(rnd))
    _set(db, "fx_updated", datetime.now().isoformat(timespec="minutes"))
    _set(db, "fx_source", source)
    db.commit()


def nbu_rates() -> dict[str, float]:
    """Офіційний курс НБУ (bank.gov.ua)."""
    r = httpx.get(NBU_URL, timeout=15)
    r.raise_for_status()
    out = {}
    for x in r.json():
        if x.get("cc") in ("USD", "EUR"):
            out[x["cc"]] = float(x["rate"])
    return out


def round_price(v: float | None, step: int) -> float | None:
    if v is None:
        return None
    if step and step > 1:
        return float(round(v / step) * step)
    return round(v, 2)


@dataclass
class PriceRow:
    product: object
    cost_uah_crm: float | None          # закупівля з CRM (сер. ціна, грн) — якщо валютної не задано
    rate: float

    @property
    def cur(self):
        return self.product.cost_currency or ("UAH" if not self.product.cost_fx else "EUR")

    @property
    def cost_uah(self):
        p = self.product
        if p.cost_fx:
            return round(p.cost_fx * self.rate, 2)
        return self.cost_uah_crm

    @property
    def price_now(self):
        return self.product.price or self.product.price_manual

    @property
    def price_at_rate(self):
        return round(self.product.rrp_fx * self.rate, 2) if self.product.rrp_fx else None


def margin(price, cost):
    return round(100 * (price - cost) / price, 1) if price and cost is not None else None


def markup(price, cost):
    return round(100 * (price - cost) / cost, 1) if price and cost else None


def price_for_margin(cost, m):
    return cost / (1 - m / 100) if cost else None
