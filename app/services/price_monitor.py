"""Центр моніторингу цін.

1) Товари складів «Вітрина» (косметика), «Ін'єкційна», «Класична», «Одноразка»: стартова ціна закупівлі й кожен
   новий прихід (закупівлі / поставки CRM). Подорожчання = остання ціна − стартова, ₴ за одиницю приходу.
2) Собівартість послуг прайсу: техкарта (норми списання × поточна ціна закупівлі) проти стартового знімка.

Для кожного складу і для послуг задаються 4 зони подорожчання в гривнях (напр. 100 / 200 / 300 / 400).
Перехід у вищу зону — сповіщення в CRM (PriceAlert) і в Telegram-бот власнику. «Прийняти ціну» — нова ціна стає стартовою.
Налаштування й стартові ціни — SyncState price_monitor."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CbService, PriceAlert, ProcedureNorm, Product, ProductPurchase, SyncState

KEY = "price_monitor"
WAREHOUSES = {"vitrina": "Косметика", "inject": "Ін'єкції", "classic": "Класична", "single": "Одноразка"}
DEFAULT_ZONES = [100.0, 200.0, 300.0, 400.0]
ZONE_COLORS = ["#EEF7EE", "#FFF4C2", "#FFE0B2", "#FFCDD2", "#E57373"]          # 0 — без подорожчання
ZONE_INK = ["#2E7D32", "#8A6D00", "#B25E00", "#B23B2E", "#7A1010"]
ZONE_NAMES = ["Без змін", "Зона 1", "Зона 2", "Зона 3", "Зона 4"]


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    d.setdefault("zones", {})
    for k in WAREHOUSES:
        d["zones"].setdefault(k, list(DEFAULT_ZONES))
    d.setdefault("svc_zones", list(DEFAULT_ZONES))
    d.setdefault("base", {})            # pid → {cost, at}
    d.setdefault("svc_base", {})        # sid → {cost, at}
    d.setdefault("notified", {})        # "p:12" / "s:5" → остання повідомлена зона
    d.setdefault("tg", True)
    return d


def save_cfg(db: Session, d: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def zone_of(diff: float, zones: list[float]) -> int:
    z = 0
    for i, t in enumerate(sorted(float(x or 0) for x in zones), 1):
        if t > 0 and diff >= t - 1e-9:
            z = i
    return z


def _k(p: Product) -> float:
    return p.coef if p.coef and p.coef > 0 else 1


def products(db: Session, d: dict | None = None) -> list[dict]:
    d = d or cfg(db)
    hist: dict[int, list] = defaultdict(list)
    for pp in db.scalars(select(ProductPurchase).where(ProductPurchase.unit_cost > 0).order_by(ProductPurchase.bought_on, ProductPurchase.id)):
        hist[pp.product_id].append(pp)
    out = []
    for p in db.scalars(select(Product).where(Product.warehouse.in_(list(WAREHOUSES)), Product.active.is_not(False)).order_by(Product.name)):
        k = _k(p)
        arr = [{"at": pp.bought_on, "cost": round(pp.unit_cost * k, 2), "supplier": pp.supplier, "qty": pp.qty, "doc": pp.doc_no}
               for pp in hist.get(p.id, [])]
        if not arr and not p.cost:
            continue
        b = d["base"].get(str(p.id))
        if b:
            base, base_at = float(b["cost"]), b.get("at")
        elif arr:
            base, base_at = arr[0]["cost"], arr[0]["at"].isoformat()
        else:
            base, base_at = round(float(p.cost) * k, 2), None
        cur = arr[-1]["cost"] if arr else round(float(p.cost or 0) * k, 2)
        diff = round(cur - base, 2)
        z = zone_of(diff, d["zones"].get(p.warehouse, DEFAULT_ZONES))
        out.append({"id": p.id, "name": p.name, "wh": p.warehouse, "brand": p.brand, "supplier": p.supplier, "base": base,
                    "base_at": base_at, "cur": cur, "diff": diff, "pct": round(100 * diff / base, 1) if base else None,
                    "zone": z, "hist": arr[-12:], "n": len(arr), "last_at": arr[-1]["at"] if arr else None})
    return out


def _unit_cost_now(db: Session) -> dict[int, float]:
    """Поточна ціна одиниці обліку: остання закупівля, інакше собівартість товару."""
    out = {p.id: float(p.cost or 0) for p in db.scalars(select(Product))}
    for pp in db.scalars(select(ProductPurchase).where(ProductPurchase.unit_cost > 0).order_by(ProductPurchase.bought_on, ProductPurchase.id)):
        out[pp.product_id] = float(pp.unit_cost)
    return out


def services(db: Session, d: dict | None = None) -> list[dict]:
    d = d or cfg(db)
    uc = _unit_cost_now(db)
    names = {p.id: p.name for p in db.scalars(select(Product))}
    norms: dict[int, list] = defaultdict(list)
    for n in db.scalars(select(ProcedureNorm)):
        norms[n.service_id].append(n)
    svcs = db.scalars(select(CbService)).all()
    card: dict[str, int] = {}
    for sv in svcs:
        if sv.techcard and sv.id in norms:
            card.setdefault(sv.techcard.strip().lower(), sv.id)
    out = []
    for sv in sorted(svcs, key=lambda x: ((x.category or "яяя"), x.name)):
        src = sv.id if sv.id in norms else card.get((sv.techcard or "").strip().lower())
        if not src:
            continue
        parts = [{"pid": n.product_id, "name": names.get(n.product_id, f"#{n.product_id}"), "qty": n.qty,
                  "cost": round(float(n.qty or 0) * uc.get(n.product_id, 0), 2)} for n in norms[src]]
        cur = round(sum(x["cost"] for x in parts), 2)
        b = d["svc_base"].get(str(sv.id))
        base = float(b["cost"]) if b else cur
        diff = round(cur - base, 2)
        out.append({"id": sv.id, "name": sv.name, "category": sv.category, "price": sv.price, "techcard": sv.techcard,
                    "parts": sorted(parts, key=lambda x: -x["cost"]), "base": base, "base_at": (b or {}).get("at"), "cur": cur,
                    "diff": diff, "pct": round(100 * diff / base, 1) if base else None, "zone": zone_of(diff, d["svc_zones"]),
                    "margin": round(100 * (sv.price - cur) / sv.price, 1) if sv.price else None})
    return out


def scan(db: Session, notify: bool = True) -> int:
    """Фіксує стартові знімки послуг і створює сповіщення при переході у вищу зону."""
    d = cfg(db)
    changed, new = False, []
    today = date.today().isoformat()
    for r in services(db, d):
        if str(r["id"]) not in d["svc_base"]:
            d["svc_base"][str(r["id"])] = {"cost": r["cur"], "at": today}
            changed = True
    for kind, rows in (("product", products(db, d)), ("service", services(db, d))):
        for r in rows:
            key = f"{kind[0]}:{r['id']}"
            was = int(d["notified"].get(key, 0))
            if r["zone"] > was:
                new.append(PriceAlert(kind=kind, item_id=r["id"], name=r["name"][:300], zone=r["zone"], base=r["base"],
                                      current=r["cur"], note=(WAREHOUSES.get(r.get("wh")) if kind == "product" else "собівартість послуги")))
            if r["zone"] != was:
                d["notified"][key] = r["zone"]
                changed = True
    for a in new:
        db.add(a)
    if changed:
        save_cfg(db, d)
    elif new:
        db.commit()
    if new and notify and d.get("tg", True):
        try:
            from . import alerts
            lines = [f"{'📦' if a.kind == 'product' else '💉'} {a.name}: {a.base:.2f} → {a.current:.2f} ₴ "
                     f"(+{a.current - a.base:.2f} ₴, {ZONE_NAMES[a.zone]})" for a in new[:20]]
            alerts.send("📈 <b>Подорожчання — центр моніторингу цін</b>\n" + "\n".join(lines) +
                        (f"\n…і ще {len(new) - 20}" if len(new) > 20 else ""))
        except Exception:  # noqa: BLE001
            pass
    return len(new)


def accept(db: Session, kind: str, item_id: int) -> None:
    """Прийняти поточну ціну як стартову (зона скидається)."""
    d = cfg(db)
    rows = products(db, d) if kind == "product" else services(db, d)
    r = next((x for x in rows if x["id"] == item_id), None)
    if not r:
        return
    (d["base"] if kind == "product" else d["svc_base"])[str(item_id)] = {"cost": r["cur"], "at": date.today().isoformat()}
    d["notified"][f"{kind[0]}:{item_id}"] = 0
    save_cfg(db, d)


def unseen(db: Session) -> int:
    return db.query(PriceAlert).filter(PriceAlert.seen_at.is_(None)).count()
