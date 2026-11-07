"""Центр планування ціни послуг (Фінанси → Планування ціни послуг) — як таблиця «Прайс 2026».

На кожну послугу:
  Ціна · Матеріали (техкарта, остання поставка) · Амортизація (позиції обладнання в техкарті) ·
  Валовий = ціна − матеріали − амортизація · ЗП лікаря (за окладом лікаря або вручну) · ЗП за хвилину ·
  Собівартість = матеріали + амортизація + ЗП · Накладні = хвилини × ставка ₴/хв (оренда, адміни, реклама…) ·
  Чистий прибуток = ціна − собівартість − накладні · Заробіток за хвилину · Чиста маржа = чистий / ціна · ROI = чистий / витрати.
Рекомендована ціна — щоб чиста маржа = цільова (ЗП у % від ціни теж росте з ціною — рахуємо підбором).
Версії й історія — SyncState service_price_versions; кожна зміна ціни — ServiceLog послуги.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CbService, Doctor, SyncState, TechCard

CFG_KEY = "service_pricing"
VER_KEY = "service_price_versions"
MARGINS = [30, 35, 40, 45, 50, 55, 60]


def cfg(db: Session) -> dict:
    st = db.get(SyncState, CFG_KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    d.setdefault("ovh_min", 0.0)         # накладні ₴ за хвилину — за замовчуванням не закладаємо (0)
    d.setdefault("target", 45.0)         # цільова чиста маржа, %
    d.setdefault("round", 100)           # округлення рекомендованої ціни
    d.setdefault("doctor", "")           # лікар для ЗП: "" — найдорожчий з тих, хто надає послугу
    return d


def save_cfg(db: Session, d: dict) -> None:
    st = db.get(SyncState, CFG_KEY) or SyncState(key=CFG_KEY)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def is_amort(cat: str | None, p) -> bool:
    c = (cat or "").lower()
    return "обладн" in c or "амортиз" in c or (p is not None and (p.warehouse == "equip" or "амортиз" in (p.name or "").lower()))


def material_costs(db: Session, card: TechCard | None, uc: dict[int, float]) -> tuple[float, float]:
    """(матеріали, амортизація) за поточною ціною одиниці."""
    mat = am = 0.0
    for ln in (card.lines if card else []):
        v = float(ln.qty or 0) * float(uc.get(ln.product_id, 0) or 0)
        if is_amort(ln.cat, ln.product):
            am += v
        else:
            mat += v
    return round(mat, 2), round(am, 2)


class Zp:
    """ЗП лікаря за послугу: вручну в картці послуги (zp_fixed) або за окладом лікаря (Налаштування окладів)."""

    def __init__(self, db: Session):
        from . import salary_plans as spl
        self.spl = spl
        self.docs = [d for d in db.scalars(select(Doctor).where(Doctor.active.is_(True)).order_by(Doctor.name))]
        self.plans = {d.id: spl.by_name(db, (d.extra or {}).get("salary")) for d in self.docs}
        self.plans = {k: v for k, v in self.plans.items() if v and v.get("comm", {}).get("on")}

    def doctors(self):
        return [d for d in self.docs if d.id in self.plans]

    def calc(self, sv: CbService, price: float, cost_tc: float, doctor_id: int | None = None) -> tuple[float, str]:
        """(сума, звідки)."""
        if sv.zp_fixed is not None:
            return float(sv.zp_fixed), "вручну"
        ids = [doctor_id] if doctor_id else ([i for i in (sv.doctor_ids or []) if i in self.plans] or list(self.plans))
        best, who = 0.0, ""
        ln = {"type": "service", "name": sv.name or "", "category": sv.category or "", "cost": price, "paid": price, "qty": 1,
              "cost_tc": cost_tc, "cost_fx": float(sv.cost_fixed or 0)}
        for i in ids:
            p = self.plans.get(i)
            if not p:
                continue
            amt, v, m = self.spl.line_amount(p, ln)
            if amt > best or not who:
                best, who = amt, next((d.name for d in self.docs if d.id == i), "")
        return round(best, 2), who


def zp_linear(zp: Zp, sv: CbService, price: float, cost_tc: float, doctor_id: int | None = None) -> tuple[float, float]:
    """ЗП як a + b × ціна (для живого перерахунку в браузері)."""
    p1 = price or 1000.0
    z1, _ = zp.calc(sv, p1, cost_tc, doctor_id)
    z2, _ = zp.calc(sv, p1 * 2, cost_tc, doctor_id)
    b = (z2 - z1) / p1
    return round(z1 - b * p1, 4), round(b, 6)


def calc(sv: CbService, price: float, mat: float, am: float, zp: Zp, c: dict, doctor_id: int | None = None) -> dict:
    mins = int(sv.duration or sv.time_min or 0)
    z, who = zp.calc(sv, price, mat + am, doctor_id)
    ovh = round(mins * float(c.get("ovh_min") or 0), 2)
    gross = round(price - mat - am, 2)
    cost = round(mat + am + z, 2)
    net = round(price - cost - ovh, 2)
    spend = cost + ovh
    return {"mins": mins, "price": price, "mat": mat, "am": am, "gross": gross, "zp": z, "zp_who": who,
            "zp_min": round(z / mins, 2) if mins else None, "cost": cost, "ovh": ovh, "net": net,
            "earn_min": round(net / mins, 2) if mins else None,
            "gross_m": round(100 * gross / price, 1) if price else None,
            "net_m": round(100 * net / price, 1) if price else None,
            "roi": round(100 * net / spend, 1) if spend else None}


def recommend(sv: CbService, mat: float, am: float, zp: Zp, c: dict, target: float, doctor_id: int | None = None) -> float | None:
    """Ціна, за якої чиста маржа = target (підбір — ЗП може залежати від ціни)."""
    if target >= 95:
        return None
    lo, hi = 0.0, max(1000.0, (mat + am + 1) * 50)
    for _ in range(60):
        mid = (lo + hi) / 2
        r = calc(sv, mid, mat, am, zp, c, doctor_id)
        if r["net_m"] is not None and r["net_m"] >= target:
            hi = mid
        else:
            lo = mid
    from .pricing import round_price
    return round_price(hi, int(c.get("round") or 1)) if hi > 1 else None


def rows(db: Session, c: dict | None = None, cat: str = "", q: str = "", only: str = "") -> list[dict]:
    from .techcards import unit_costs
    from ..models import ProductPurchase
    c = c or cfg(db)
    uc = unit_costs(db)
    for pp in db.scalars(select(ProductPurchase).where(ProductPurchase.unit_cost > 0).order_by(ProductPurchase.bought_on, ProductPurchase.id)):
        uc[pp.product_id] = float(pp.unit_cost)          # як у картці послуги: ціна останньої поставки
    zp = Zp(db)
    cards = {k.id: k for k in db.scalars(select(TechCard))}
    did = int(c["doctor"]) if str(c.get("doctor") or "").isdigit() else None
    out = []
    for sv in db.scalars(select(CbService).where(CbService.active.is_not(False)).order_by(CbService.category, CbService.name)):
        if cat and (sv.category or "") != cat:
            continue
        if q and q.lower() not in (sv.name or "").lower():
            continue
        card = cards.get(sv.techcard_id)
        mat, am = material_costs(db, card, uc)
        price = float(sv.price or 0)
        r = calc(sv, price, mat, am, zp, c, did)
        r["za"], r["zb"] = zp_linear(zp, sv, price, mat + am, did)
        r.update(sv=sv, card=card, rec=recommend(sv, mat, am, zp, c, float(c.get("target") or 0), did) if (mat or am or r["zp"]) else None)
        if only == "low" and not (r["net_m"] is not None and r["net_m"] < float(c.get("target") or 0)):
            continue
        if only == "nocard" and card:
            continue
        out.append(r)
    return out


# ---------------- версії ----------------

def versions(db: Session) -> list[dict]:
    st = db.get(SyncState, VER_KEY)
    try:
        return json.loads(st.value) if st and st.value else []
    except ValueError:
        return []


def _put_versions(db: Session, vs: list[dict]) -> None:
    st = db.get(SyncState, VER_KEY) or SyncState(key=VER_KEY)
    st.value, st.updated_at = json.dumps(vs[:40], ensure_ascii=False, default=str), datetime.utcnow()
    db.merge(st)
    db.commit()


def add_version(db: Session, kind: str, items: list[dict], user: str | None, note: str | None = None) -> dict:
    vs = versions(db)
    v = {"id": (max((x["id"] for x in vs), default=0) + 1), "at": datetime.now().isoformat(timespec="minutes"), "kind": kind,
         "user": user, "note": (note or "")[:120], "items": items, "n": len(items)}
    _put_versions(db, [v] + vs)
    return v


def snapshot(db: Session, user: str | None, note: str | None) -> dict:
    items = [{"sid": r["sv"].id, "name": r["sv"].name, "price": r["price"], "net_m": r["net_m"], "cost": r["cost"]} for r in rows(db)]
    return add_version(db, "version", items, user, note)


def apply_prices(db: Session, prices: dict[int, float], user_id: int | None, user_name: str | None, note: str | None = None,
                 kind: str = "save") -> int:
    """Записати нові ціни: лог у картці послуги, ціна закріплена від синку CleverBox, дата формування — сьогодні."""
    from . import service_card as sc
    items = []
    for sid, p in prices.items():
        sv = db.get(CbService, sid)
        if not sv or p is None or p <= 0 or abs((sv.price or 0) - p) < 0.005:
            continue
        items.append({"sid": sid, "name": sv.name, "before": sv.price, "after": p})
        sc.log(db, sid, "price", sv.price, p, user_id, commit=False)
        sv.price, sv.price_locked, sv.price_set_on = p, True, date.today()
    db.commit()
    if items:
        add_version(db, kind, items, user_name, note)
    return len(items)


def restore(db: Session, vid: int, user_id: int | None, user_name: str | None) -> int:
    v = next((x for x in versions(db) if x["id"] == vid), None)
    if not v:
        return 0
    if v["kind"] == "version":
        prices = {int(i["sid"]): float(i["price"]) for i in v["items"] if i.get("price")}
    else:                                            # скасувати збереження — повернути «до»
        prices = {int(i["sid"]): float(i["before"]) for i in v["items"] if i.get("before")}
    return apply_prices(db, prices, user_id, user_name, f"Повернення до №{vid}", "restore")
