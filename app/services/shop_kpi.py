"""KPI інтернет-магазину: виручка, замовлення, середній чек, повторні покупці, джерела (UTM / реклама), товари, бренди, кошики, відгуки."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AdsStat, ShopCart, ShopReview, SiteOrder
from . import shop_orders as so


def source_of(o: SiteOrder) -> tuple[str, str, str]:
    u = (o.raw or {}).get("utm") or {}
    src = (u.get("utm_source") or "").lower()
    med = (u.get("utm_medium") or "").lower()
    if u.get("gclid") or u.get("gbraid") or u.get("wbraid"):
        src, med = src or "google", med or "cpc"
    elif u.get("fbclid") and not src:
        src, med = "facebook", med or "social"
    if not src:
        src, med = "(direct)", "(none)"
    return src, med or "—", u.get("utm_campaign") or ""


def channel(src: str, med: str) -> str:
    if med in ("cpc", "ppc", "paid", "paidsocial", "paid_social", "cpm", "display", "shopping", "pmax"):
        return "Реклама"
    if med == "organic":
        return "Пошук (SEO)"
    if src in ("instagram", "facebook", "ig", "fb", "tiktok", "t.me", "telegram", "l.instagram.com", "m.facebook.com", "lm.facebook.com") or med in ("social", "smm"):
        return "Соцмережі"
    if med in ("email", "sms", "viber", "bot", "messenger"):
        return "Розсилки й бот"
    if src == "(direct)":
        return "Прямі заходи"
    return "Інші сайти"


SHOP_WORDS = ("store", "pmax", "shopping", "товар", "магазин")      # кампанії Google Ads, що рекламують магазин
DEF_MARGIN = 35.0                                                   # % валової маржі, якщо в товару немає ціни закупівлі


def _is_google_order(o: SiteOrder) -> bool:
    s, m, _ = source_of(o)
    return s == "google" and m in ("cpc", "ppc", "paid", "shopping", "pmax", "display")


def ads(db: Session, d1: date, d2: date, ok: list[SiteOrder]) -> dict:
    """Окупність реклами Google для магазину: витрати кампаній магазину (з ПДВ) проти замовлень із Google.
    CPA — скільки коштує один продаж; ROAS — виручка на 1 ₴ реклами; прибуток після реклами = валова маржа − реклама."""
    from ..models import AdsStat, Product
    from . import google_ads as ga
    vat = float(ga.cfg(db).get("vat_pct") or 0)
    camps: dict = defaultdict(lambda: {"cost": 0.0, "clicks": 0.0, "impr": 0.0, "conv": 0.0, "value": 0.0})
    for st in db.scalars(select(AdsStat).where(AdsStat.source == "google", AdsStat.level == "campaign", AdsStat.day >= d1, AdsStat.day <= d2)):
        nm = st.name or st.key
        if any(w in nm.lower() for w in SHOP_WORDS):
            c = camps[nm]
            c["cost"] += st.cost or 0
            c["clicks"] += st.clicks or 0
            c["impr"] += st.impressions or 0
            c["conv"] += st.conversions or 0
            c["value"] += st.conv_value or 0
    cost_net = sum(c["cost"] for c in camps.values())
    cost = round(cost_net * (1 + vat / 100), 2)
    clicks = sum(c["clicks"] for c in camps.values())
    g = [o for o in ok if _is_google_order(o)]
    rev = round(sum(float(o.total or 0) for o in g), 2)
    cogs, est = 0.0, False
    for o in g:
        for i in o.items or []:
            pr = db.get(Product, i.get("product_id")) if str(i.get("product_id") or "").isdigit() else None
            t = float(i.get("total") or 0)
            if pr and pr.cost:
                cogs += float(pr.cost) * int(i.get("qty") or 1)
            else:
                cogs += t * (1 - DEF_MARGIN / 100)
                est = True
    gross = round(rev - cogs, 2)
    n = len(g)
    out = {"campaigns": sorted(({"name": k, **v} for k, v in camps.items()), key=lambda x: -x["cost"]), "cost": cost, "cost_net": round(cost_net, 2),
           "vat": vat, "clicks": int(clicks), "impr": int(sum(c["impr"] for c in camps.values())), "orders": n, "revenue": rev, "gross": gross,
           "est": est, "g_conv": round(sum(c["conv"] for c in camps.values()), 1), "g_value": round(sum(c["value"] for c in camps.values()), 2),
           "cpa": round(cost / n, 2) if n else None, "roas": round(rev / cost, 2) if cost else None,
           "cpc": round(cost / clicks, 2) if clicks else None, "cr": round(n / clicks * 100, 2) if clicks else None,
           "be_cpa": round(gross / n, 2) if n else None, "profit": round(gross - cost, 2)}
    out["verdict"] = None if not cost else ("ok" if out["profit"] > 0 else "bad")
    return out


def report(db: Session, d1: date, d2: date, data: dict | None = None) -> dict:
    a, b = datetime.combine(d1, datetime.min.time()), datetime.combine(d2 + timedelta(days=1), datetime.min.time())
    rows = list(db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.created_at >= a, SiteOrder.created_at < b)))
    ok = [o for o in rows if not so.is_lost(o) and o.status != "Фейкове"]
    k = so.kpi(rows)
    prev_len = (d2 - d1).days + 1
    pa = a - timedelta(days=prev_len)
    prev = [o for o in db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.created_at >= pa, SiteOrder.created_at < a))
            if not so.is_lost(o) and o.status != "Фейкове"]
    k["prev_revenue"] = round(sum(o.total or 0 for o in prev), 2)
    k["prev_ok"] = len(prev)
    k["paid_share"] = round(100 * sum(1 for o in ok if o.paid) / len(ok)) if ok else 0
    srcs: dict = defaultdict(lambda: {"n": 0, "sum": 0.0})
    chans: dict = defaultdict(lambda: {"n": 0, "sum": 0.0})
    camps: dict = defaultdict(lambda: {"n": 0, "sum": 0.0})
    cname = {st.key: st.name for st in db.scalars(select(AdsStat).where(AdsStat.level == "campaign")) if st.key and st.name}
    for o in ok:
        s, m, c = source_of(o)
        c = cname.get(c, c)                             # utm_campaign={campaignid} → назва кампанії
        for bucket, key in ((srcs, f"{s} / {m}"), (chans, channel(s, m)), (camps, c)):
            if key:
                bucket[key]["n"] += 1
                bucket[key]["sum"] += float(o.total or 0)
    prods: dict = defaultdict(lambda: {"n": 0, "sum": 0.0, "name": ""})
    brand_of = {}
    if data:
        brand_of = {v["id"]: it["brand"] for it in data["items"] for v in it["variants"]}
    brands: dict = defaultdict(lambda: {"n": 0, "sum": 0.0})
    for o in ok:
        for i in o.items or []:
            pid = i.get("product_id")
            p = prods[pid or i.get("name")]
            p["name"] = i.get("name") or ""
            p["n"] += int(i.get("qty") or 1)
            p["sum"] += float(i.get("total") or 0)
            br = brand_of.get(pid) or "—"
            brands[br]["n"] += int(i.get("qty") or 1)
            brands[br]["sum"] += float(i.get("total") or 0)
    carts_open = db.scalars(select(ShopCart).where(ShopCart.status == "open", ShopCart.updated_at < datetime.utcnow() - timedelta(hours=1))).all()
    carts_back = db.scalar(select(func.count()).select_from(ShopCart).where(ShopCart.status == "ordered", ShopCart.reminded_at.is_not(None))) or 0
    rv = db.execute(select(func.count(), func.avg(ShopReview.rating)).where(ShopReview.status == "ok")).first()
    by_day: dict = defaultdict(float)
    for o in ok:
        by_day[o.created_at.date().isoformat()] += float(o.total or 0)
    days = [(d1 + timedelta(days=n)).isoformat() for n in range(prev_len)]

    def top(dct, n=15):
        return sorted(({"key": kk, **vv} for kk, vv in dct.items()), key=lambda x: -x["sum"])[:n]
    try:
        ad = ads(db, d1, d2, ok)
    except Exception:  # noqa: BLE001 — блок реклами не має ламати KPI
        ad = None
    return {"ads": ad, "k": k, "sources": top(srcs, 20), "channels": top(chans), "campaigns": top(camps), "products": top(prods, 15), "brands": top(brands),
            "carts": {"n": len(carts_open), "sum": round(sum(c.total or 0 for c in carts_open), 2), "back": carts_back},
            "reviews": {"n": rv[0] or 0, "avg": round(float(rv[1]), 1) if rv and rv[1] else 0},
            "days": days, "series": [round(by_day.get(x, 0)) for x in days]}
