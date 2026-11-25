"""Центр моніторингу цін.

1) Товари складів «Вітрина» (косметика), «Ін'єкційна», «Класична», «Одноразка»: стартова ціна закупівлі й кожен
   новий прихід (закупівлі / поставки CRM). Подорожчання = остання ціна − стартова, ₴ за одиницю приходу.
2) Собівартість послуг прайсу: техкарта (норми списання × поточна ціна закупівлі) проти стартового знімка.

Для кожного складу і для послуг задаються 4 зони подорожчання в гривнях (напр. 100 / 200 / 300 / 400).
Перехід у вищу зону — сповіщення в CRM (PriceAlert) і в Telegram-бот власнику. «Прийняти ціну» — нова ціна стає стартовою.
Налаштування й стартові ціни — SyncState price_monitor."""
from __future__ import annotations

import json
import threading
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
    d.setdefault("brand_rules", {})     # бренд (lower) → {name, unit: uah|pct, zones: [4], off: bool}
    d.setdefault("item_rules", {})      # pid → {unit, zones, off}
    return d


def bkey(brand: str | None) -> str:
    return (brand or "").strip().lower()


def rule_for(d: dict, pid: int, brand: str | None, wh: str) -> dict:
    """Правило зон для товару: власне правило позиції → правило бренду → зони складу (₴)."""
    r = d["item_rules"].get(str(pid))
    if r:
        return {"src": "item", "label": "своє правило позиції", "unit": r.get("unit") or "uah",
                "zones": r.get("zones") or list(DEFAULT_ZONES), "off": bool(r.get("off"))}
    b = d["brand_rules"].get(bkey(brand)) if brand else None
    if b:
        return {"src": "brand", "label": f"правило бренду {b.get('name') or brand}", "unit": b.get("unit") or "uah",
                "zones": b.get("zones") or list(DEFAULT_ZONES), "off": bool(b.get("off"))}
    return {"src": "wh", "label": f"зони складу «{WAREHOUSES.get(wh, wh)}»", "unit": "uah",
            "zones": d["zones"].get(wh, DEFAULT_ZONES), "off": False}


def zone_rule(diff: float, pct: float | None, rule: dict) -> int:
    if rule["off"]:
        return 0
    if rule["unit"] == "pct":
        return zone_of(pct or 0.0, rule["zones"]) if pct is not None else 0
    return zone_of(diff, rule["zones"])


def set_rule(d: dict, kind: str, key: str, unit: str, zones: list[float], off: bool, name: str = "") -> None:
    rule = {"unit": "pct" if unit == "pct" else "uah", "zones": sorted(round(float(z or 0), 2) for z in zones), "off": bool(off)}
    if kind == "brand":
        rule["name"] = name or key
        d["brand_rules"][bkey(key)] = rule
    else:
        d["item_rules"][str(key)] = rule


def drop_rule(d: dict, kind: str, key: str) -> None:
    (d["brand_rules"] if kind == "brand" else d["item_rules"]).pop(bkey(key) if kind == "brand" else str(key), None)


def brands(rows: list[dict], d: dict) -> list[dict]:
    """Бренди серед товарів моніторингу: скільки позицій, у яких зонах, правило бренду."""
    by: dict[str, dict] = {}
    for r in rows:
        name = (r.get("brand") or "").strip() or "Без бренду"
        x = by.setdefault(bkey(name) if r.get("brand") else "", {"name": name, "key": r.get("brand") or "", "n": 0, "up": 0, "zones": [0] * 5,
                                                              "own": 0, "max_pct": None})
        x["n"] += 1
        x["zones"][r["zone"]] += 1
        x["up"] += 1 if (r["diff"] or 0) > 0 else 0
        x["own"] += 1 if r["rule"]["src"] == "item" else 0
        if r["pct"] is not None and (x["max_pct"] is None or r["pct"] > x["max_pct"]):
            x["max_pct"] = r["pct"]
    for k, x in by.items():
        x["rule"] = d["brand_rules"].get(k) if k else None
    return sorted(by.values(), key=lambda x: (-sum(x["zones"][1:]), -x["up"], x["name"].lower()))


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


WINDOW_DAYS = 365      # стартова ціна за замовчуванням — перший прихід за останній рік (оперативний контроль)


def _norm(rows: list, k: float) -> list[tuple]:
    """Приводить ціни приходів до ціни за упаковку. Старі записи бувають у різних одиницях (за мл / за упаковку)
    або в іншій валюті: беремо найщільніший кластер значень і відкидаємо записи, що не вкладаються в нього в 3 рази."""
    if not rows:
        return []
    cands = [[float(r.unit_cost) * k] + ([float(r.unit_cost)] if k > 1 else []) for r in rows]
    best, center = -1, 0.0
    for i, cs in enumerate(cands):
        for v in cs:
            n = sum(1 for o in cands if any(v / 1.6 <= x <= v * 1.6 for x in o))
            if n > best or (n == best and i == len(cands) - 1):
                best, center = n, v
    out = []
    for r, cs in zip(rows, cands):
        v = min(cs, key=lambda x: abs(x / center - 1) if x >= center else abs(center / x - 1))
        if center / 3 <= v <= center * 3:
            out.append((r, round(v, 2)))
    return out


def planned_costs(db: Session) -> dict[int, dict]:
    """Закупівля з «Планування ціни реалізації» (прайс постачальника): pid → {cost ₴ за од. приходу, at, cur, fx}.
    Курс — постачальника (картка), інакше загальний. Дата — коли закупівлю у валюті востаннє змінено (історія планування)."""
    from ..models import PriceSnapshot, Supplier
    from .pricing import rates
    rt = rates(db)
    sfx = {s.name.strip().lower(): s for s in db.scalars(select(Supplier)) if s.name}
    when: dict[int, datetime] = {}
    for sn in db.scalars(select(PriceSnapshot).where(PriceSnapshot.kind != "version").order_by(PriceSnapshot.created_at)):
        for it in sn.items or []:
            b, a = it.get("before") or {}, it.get("after") or {}
            if it.get("pid") and (b.get("cost_fx"), b.get("cost_currency")) != (a.get("cost_fx"), a.get("cost_currency")):
                when[int(it["pid"])] = sn.created_at
    out = {}
    for p in db.scalars(select(Product).where(Product.cost_fx.is_not(None), Product.cost_fx > 0)):
        cur = p.cost_currency or "UAH"
        s = sfx.get((p.supplier or "").strip().lower())
        own = {"USD": getattr(s, "fx_usd", None), "EUR": getattr(s, "fx_eur", None)}.get(cur) if s else None
        fx = 1.0 if cur == "UAH" else float(own or rt.get(cur, 1.0))
        out[p.id] = {"cost": round(float(p.cost_fx) * fx, 2), "at": when.get(p.id), "cur": cur, "fx_cost": float(p.cost_fx), "fx": fx}
    return out


def products(db: Session, d: dict | None = None) -> list[dict]:
    d = d or cfg(db)
    plan = planned_costs(db)
    hist: dict[int, list] = defaultdict(list)
    for pp in db.scalars(select(ProductPurchase).where(ProductPurchase.unit_cost > 0).order_by(ProductPurchase.bought_on, ProductPurchase.id)):
        hist[pp.product_id].append(pp)
    out = []
    for p in db.scalars(select(Product).where(Product.warehouse.in_(list(WAREHOUSES)), Product.active.is_not(False)).order_by(Product.name)):
        k = _k(p)
        raw = hist.get(p.id, [])
        arr = [{"at": pp.bought_on, "cost": v, "supplier": pp.supplier, "qty": pp.qty, "doc": pp.doc_no} for pp, v in _norm(raw, k)]
        pl = plan.get(p.id)
        if not arr and not p.cost and not pl:
            continue
        b = d["base"].get(str(p.id))
        since = date.fromordinal(date.today().toordinal() - WINDOW_DAYS)
        if b:
            base, base_at = float(b["cost"]), b.get("at")
        elif arr:
            f = next((x for x in arr if x["at"] and x["at"] >= since), arr[-1])
            base, base_at = f["cost"], f["at"].isoformat()
        else:
            base, base_at = round(float(p.cost) * k, 2), None
        cur = arr[-1]["cost"] if arr else round(float(p.cost or 0) * k, 2)
        last_at = arr[-1]["at"] if arr else None
        src = "supply" if arr else "card"
        # ціна з прайсу в «Плануванні ціни» новіша за останній прихід (або дати зміни немає) — вона і є поточною
        if pl and (not last_at or not pl["at"] or pl["at"].date() >= last_at):
            cur, src = pl["cost"], "plan"
            last_at = pl["at"].date() if pl["at"] else last_at
            if not arr and not d["base"].get(str(p.id)) and not p.cost:
                base, base_at = cur, None
        diff = round(cur - base, 2)
        pct = round(100 * diff / base, 1) if base else None
        rule = rule_for(d, p.id, p.brand, p.warehouse)
        z = zone_rule(diff, pct, rule)
        out.append({"id": p.id, "name": p.name, "wh": p.warehouse, "brand": p.brand, "supplier": p.supplier, "base": base,
                    "base_at": base_at, "cur": cur, "diff": diff, "pct": pct, "rule": rule, "src": src, "plan": pl,
                    "zone": z, "hist": arr[-12:], "n": len(arr), "skipped": len(raw) - len(arr), "last_at": last_at})
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


_SCAN_LOCK = threading.Lock()


def scan(db: Session, notify: bool = True) -> int:
    """Фіксує стартові знімки послуг і створює сповіщення при переході у вищу зону.
    Один перегляд за раз: сторінка «Моніторинг цін» і планувальник не дублюють сповіщення."""
    if not _SCAN_LOCK.acquire(blocking=False):
        return 0
    try:
        return _scan(db, notify)
    finally:
        _SCAN_LOCK.release()


def _scan(db: Session, notify: bool = True) -> int:
    db.expire_all()
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
            if r["zone"] > was and not (r.get("rule") or {}).get("off"):
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
                     f"(+{a.current - a.base:.2f} ₴{f', +{100 * (a.current - a.base) / a.base:.1f}%' if a.base else ''}, {ZONE_NAMES[a.zone]})"
                     for a in new[:20]]
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
