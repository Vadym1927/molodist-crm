"""Техкарти й витратні матеріали візитів (як у CleverBox).

• Техкарта — матеріали на 1 послугу за категоріями («Одноразка», «Препарат», «Обладнання» …), кожна категорія —
  свій склад списання. Обладнання (амортизація апарата) — звичайний товар складу «Обладнання» з ціною за використання.
• Додали послугу у візит — у візит копіюється її техкарта (VisitMaterial). Кількість змінюється, позиції додаються
  й прибираються; собівартість — по послузі й по візиту.
• Оплата візиту в касі CRM закриває візит: по кожному складу — документ списання «Візит №…», проведений одразу.
  Повернення / скасування чека — документи скасовуються, залишки повертаються.
• Собівартість для зарплати («віднімати собівартість») і звітів — з фактичних витратних візиту."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (Appointment, CbService, ProcedureNorm, Product, ProductPurchase, Sale, StockDoc, StockDocLine,
                      SyncState, TechCard, TechCardLine, VisitMaterial)

DEFAULT_CATS = [{"name": "Одноразка", "wh": "single"}, {"name": "Препарат", "wh": "inject"}, {"name": "Обладнання", "wh": "equip"}]
WH_CAT = {"single": "Одноразка", "inject": "Препарат", "classic": "Препарат (класична)", "equip": "Обладнання", "vitrina": "Вітрина"}


# ---------------- ціни ----------------
def _same_unit(a: str | None, b: str | None) -> bool:
    f = lambda x: (x or "").strip().lower().rstrip(".")
    return bool(f(a)) and f(a) == f(b)


def unit_costs(db: Session) -> dict[int, float]:
    """Ціна одиниці обліку (шт., мл, МО, м) для собівартості — як у CleverBox: ціна закупівлі товару (Product.cost),
    якщо її немає — остання закупівля (ProductPurchase, уже за одиницю обліку), далі собівартість залишку.

    Product.cost з CleverBox зазвичай уже за одиницю обліку (МО, шт), а з Excel-імпорту може бути за упаковку.
    Тому на коефіцієнт фасування ділимо, лише якщо:
      • є закупівлі й ціна «за упаковку / коеф.» ближча до ціни останньої закупівлі за одиницю, ніж ціна «як є»;
      • або закупівель немає, а одиниця приходу явно задана й відрізняється від одиниці обліку (уп. → шт.).
    (Раніше ділилось завжди: Нейронокс 70 МО = 16,88 ₴ замість ~3 000 ₴, шприц 0,08 ₴ замість 3,86 ₴.)"""
    import math
    out: dict[int, float] = {}
    for pp in db.scalars(select(ProductPurchase).where(ProductPurchase.unit_cost > 0).order_by(ProductPurchase.bought_on, ProductPurchase.id)):
        out[pp.product_id] = float(pp.unit_cost)
    for p in db.scalars(select(Product)):
        k = float(p.coef) if p.coef and p.coef > 1 else 1.0
        c = float(p.cost or 0)
        if c > 0:
            if k > 1:
                ref = out.get(p.id)
                if ref:
                    per_pack = abs(math.log((c / k) / ref)) < abs(math.log(c / ref))
                else:
                    per_pack = bool(p.purchase_unit) and not _same_unit(p.purchase_unit, p.unit)
                out[p.id] = c / k if per_pack else c
            else:
                out[p.id] = c
        elif p.id not in out:
            out[p.id] = float(p.stock_cost or 0) / k
    return out


def fix_unit_costs_v2(db: Session) -> int:
    """Один раз: збережені витратні візитів, де ціна одиниці була поділена на коеф. фасування вдруге, — перерахувати.
    Кількості й списання зі складу не змінюються — лише собівартість."""
    if db.get(SyncState, "visit_unit_cost_fix_v2"):
        return 0
    uc = unit_costs(db)
    coef = {p.id: float(p.coef) for p in db.scalars(select(Product).where(Product.coef > 1))}
    n = 0
    if coef:
        for m in db.scalars(select(VisitMaterial).where(VisitMaterial.product_id.in_(list(coef)))):
            new, old, k = uc.get(m.product_id, 0.0), float(m.unit_cost or 0), coef[m.product_id]
            if new > 0 and old > 0 and abs(old * k - new) / new < 0.35:      # стара ціна ≈ нова / коеф. — подвійне ділення
                m.unit_cost, n = round(new, 4), n + 1
    db.merge(SyncState(key="visit_unit_cost_fix_v2", value=str(n)))
    db.commit()
    return n


def card_cost(card: TechCard, uc: dict[int, float]) -> float:
    return round(sum(float(ln.qty or 0) * uc.get(ln.product_id, 0.0) for ln in card.lines), 2)


def cat_wh(card: TechCard | None, cat: str, product: Product | None = None) -> str:
    if product is not None and product.warehouse == "equip":       # обладнання й лічильники амортизації — лише зі складу «Обладнання»
        return "equip"
    for c in (card.cats if card and card.cats else DEFAULT_CATS):
        if c.get("name") == cat and c.get("wh"):
            return c["wh"]
    for c in DEFAULT_CATS:
        if c["name"] == cat:
            return c["wh"]
    return (product.warehouse if product and product.warehouse else None) or "single"


# ---------------- міграція з норм списання ----------------
def migrate_from_norms(db: Session) -> int:
    """Один раз: техкарти з «Норм списання» (спільна техкартка → одна техкарта, інакше — на послугу)."""
    if db.get(SyncState, "techcards_from_norms_v1"):
        return 0
    norms: dict[int, list] = defaultdict(list)
    for n in db.scalars(select(ProcedureNorm)):
        norms[n.service_id].append(n)
    made: dict[str, TechCard] = {}
    n_cards = 0
    for sv in db.scalars(select(CbService).order_by(CbService.id)):
        if sv.techcard_id or sv.id not in norms:
            continue
        key = f"t:{sv.techcard.strip().lower()}" if sv.techcard else f"s:{sv.id}"
        card = made.get(key)
        if not card:
            cats, lines = [], []
            for nm in norms[sv.id]:
                wh = nm.warehouse or (nm.product.warehouse if nm.product else None) or "single"
                cat = WH_CAT.get(wh, "Одноразка")
                if not any(c["name"] == cat for c in cats):
                    cats.append({"name": cat, "wh": wh})
                lines.append(TechCardLine(cat=cat, product_id=nm.product_id, qty=nm.qty or 1))
            card = TechCard(name=(sv.techcard or sv.name)[:200], category=sv.category, cats=cats or list(DEFAULT_CATS), lines=lines)
            db.add(card)
            db.flush()
            made[key] = card
            n_cards += 1
        sv.techcard_id = card.id
    db.merge(SyncState(key="techcards_from_norms_v1", value=str(n_cards)))
    db.commit()
    return n_cards


# ---------------- витратні візиту ----------------
def _card_of(db: Session, service_id: int | None) -> TechCard | None:
    sv = db.get(CbService, service_id) if service_id else None
    return db.get(TechCard, sv.techcard_id) if sv and sv.techcard_id else None


def resolve_service(db: Session, sid, name: str | None) -> CbService | None:
    """Послуга прайсу для рядка візиту: за id, якщо назва збігається; інакше — за назвою
    (у записах календаря CleverBox id буває номером рядка запису, а не послуги)."""
    from ..parsers.observ import norm
    sv = db.get(CbService, int(sid)) if str(sid or "").isdigit() else None
    n = norm(name or "")
    if sv and (not n or norm(sv.name) == n):
        return sv
    if not n:
        return sv
    cand = [x for x in db.scalars(select(CbService).where(CbService.active.is_not(False))) if norm(x.name) == n]
    if not cand:
        cand = [x for x in db.scalars(select(CbService)) if norm(x.name) == n]
    return next((x for x in cand if x.techcard_id), cand[0] if cand else sv)


def fix_services(db: Session, a: Appointment) -> bool:
    """Виправити id послуг візиту на послуги прайсу (за назвою), щоб підтягнулись техкарти, категорії й ціни."""
    svcs = [dict(s) for s in (a.services or [])]
    changed = False
    for s in svcs:
        sv = resolve_service(db, s.get("id"), s.get("name"))
        if sv and sv.id != s.get("id"):
            s["id"] = sv.id
            s["category"] = s.get("category") or sv.category
            changed = True
    if changed:
        a.services = svcs
        db.flush()
    return changed


def seed(db: Session, a: Appointment) -> bool:
    """Для кожної послуги візиту — витратні з техкарти (якщо ще немає або послугу замінили)."""
    if not a or closed(db, a):
        return False
    if not a.sale_id:
        fix_services(db, a)
    have: dict[int, list] = defaultdict(list)
    for m in db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == a.id)):
        have[m.svc_idx].append(m)
    uc = None
    changed = False
    svcs = a.services or []
    for idx, s in enumerate(svcs):
        sid = s.get("id")
        cur = have.get(idx, [])
        if cur and all(m.service_id == sid for m in cur):
            continue
        for m in cur:
            db.delete(m)
        card = _card_of(db, sid)
        if card:
            uc = uc if uc is not None else unit_costs(db)
            for ln in card.lines:
                db.add(VisitMaterial(appt_id=a.id, svc_idx=idx, service_id=sid, cat=ln.cat, product_id=ln.product_id, qty=ln.qty,
                                     unit_cost=uc.get(ln.product_id, 0.0), warehouse=cat_wh(card, ln.cat, ln.product)))
        changed = True
    for idx in [i for i in have if i >= len(svcs)]:          # послугу прибрали з візиту
        for m in have[idx]:
            db.delete(m)
        changed = True
    if changed:
        db.commit()
    return changed


# ---------------- спільні розхідники візиту (один комплект на візит) ----------------
SHARED_KEY = "visit_shared_materials"
EDIT_KEY = "visit_materials_editing"
SHARED_HINTS = ("рукавич", "маска мед", "шапоч", "простирад", "бахіл", "рушник", "халат", "ватн", "хлоргексид", "ахд",
                "серветк", "пелюшк", "фартух", "антисепт")


def _js(db: Session, key: str):
    import json
    from ..models import SyncState
    st = db.get(SyncState, key)
    try:
        return json.loads(st.value) if st and st.value else None
    except ValueError:
        return None


def _put(db: Session, key: str, val) -> None:
    import json
    from datetime import datetime
    from ..models import SyncState
    db.merge(SyncState(key=key, value=json.dumps(val, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()


def suggested_shared(db: Session) -> set[int]:
    """Підказка: загальні кабінетні розхідники складу «Одноразка» за назвою."""
    out = set()
    for p in db.scalars(select(Product).where(Product.warehouse == "single")):
        n = (p.name or "").lower()
        if any(h in n for h in SHARED_HINTS):
            out.add(p.id)
    return out


def shared_cfg(db: Session) -> dict:
    v = _js(db, SHARED_KEY)
    if v is None:
        return {"on": True, "products": sorted(suggested_shared(db)), "saved": False}
    return {"on": bool(v.get("on", True)), "products": [int(x) for x in v.get("products") or []], "saved": True}


def save_shared(db: Session, on: bool, products) -> None:
    _put(db, SHARED_KEY, {"on": bool(on), "products": sorted({int(x) for x in products})})


def effective(db: Session, rows) -> dict[int, float]:
    """Скільки реально списати по кожному рядку витратних візиту.
    Алгоритм: розхідники зі списку «один комплект на візит» не сумуються між послугами одного візиту —
    списується НАЙБІЛЬША кількість з усіх послуг (рядок із нею лишається, інші → 0; при рівності — перша послуга).
    Усе інше (препарати, шприци, голки, хвилини апарата…) — сума по кожній послузі, як у техкартах."""
    eff = {m.id: float(m.qty or 0) for m in rows}
    cfg = shared_cfg(db)
    if not cfg["on"]:
        return eff
    sh = set(cfg["products"])
    groups: dict[int, list] = defaultdict(list)
    for m in rows:
        if m.product_id in sh:
            groups[m.product_id].append(m)
    for pid, ms in groups.items():
        if len({m.svc_idx for m in ms}) < 2:
            continue
        per: dict[int, float] = defaultdict(float)
        for m in ms:
            per[m.svc_idx] += float(m.qty or 0)
        keep = max(sorted(per), key=lambda i: per[i])        # послуга з найбільшою кількістю (перша при рівності)
        for m in ms:
            if m.svc_idx != keep:
                eff[m.id] = 0.0
    return eff


def editing(db: Session) -> set[int]:
    return {int(x) for x in (_js(db, EDIT_KEY) or [])}


def set_editing(db: Session, aid: int, on: bool) -> None:
    cur = editing(db)
    cur = (cur | {aid}) if on else (cur - {aid})
    _put(db, EDIT_KEY, sorted(cur))


def materials(db: Session, a: Appointment) -> list[dict]:
    """По послугах візиту: категорії → рядки, собівартість, ліміт техкарти."""
    rows = db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == a.id).order_by(VisitMaterial.svc_idx, VisitMaterial.id)).all()
    if rows and not closed(db, a):                 # відкритий візит — собівартість за поточною ціною закупівлі
        uc = unit_costs(db)
        ch = False
        for m in rows:
            v = round(uc.get(m.product_id, m.unit_cost or 0), 6)
            if abs((m.unit_cost or 0) - v) > 1e-6:
                m.unit_cost, ch = v, True
        if ch:
            db.commit()
    out = []
    eff = effective(db, rows)
    for idx, s in enumerate(a.services or []):
        mine = [m for m in rows if m.svc_idx == idx]
        for m in mine:
            m.eff_qty = eff.get(m.id, m.qty)
        card = _card_of(db, s.get("id"))
        cats: dict[str, list] = {}
        for c in (card.cats if card and card.cats else DEFAULT_CATS):     # усі категорії техкарти (і порожні — щоб додати)
            if c.get("name"):
                cats.setdefault(c["name"], [])
        if "Обладнання" not in cats:
            cats["Обладнання"] = []
        for m in mine:
            cats.setdefault(m.cat, []).append(m)
        cats = {k: v for k, v in sorted(cats.items(), key=lambda kv: (not kv[1], 0))}
        cat_wh_ = {k: cat_wh(card, k) for k in cats}
        cost = round(sum(eff.get(m.id, m.qty) * m.unit_cost for m in mine), 2)
        out.append({"idx": idx, "service": s.get("name"), "service_id": s.get("id"), "card": card, "cats": cats, "cat_wh": cat_wh_, "cost": cost,
                    "max_cost": card.max_cost if card else None, "over": bool(card and card.max_cost and cost > card.max_cost + 0.005)})
    return out


def cost_by_service(db: Session, appt_id: int) -> dict[int, float]:
    out: dict[int, float] = defaultdict(float)
    rows = db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == appt_id)).all()
    eff = effective(db, rows)
    for m in rows:
        out[m.svc_idx] += eff.get(m.id, m.qty) * m.unit_cost
    return {k: round(v, 2) for k, v in out.items()}


# ---------------- закриття візиту ----------------
def _docs(db: Session, appt_id: int) -> list[StockDoc]:
    return db.scalars(select(StockDoc).where(StockDoc.ext_no == f"visit:{appt_id}", StockDoc.status == "posted")).all()


def closed(db: Session, a: Appointment) -> bool:
    if getattr(a, "historic", False):              # історія CleverBox: склад уже списаний у CleverBox
        return True
    return bool(_docs(db, a.id))


def close(db: Session, a: Appointment, user_id: int | None = None) -> list[StockDoc]:
    """Списати витратні візиту: документ «Списання» на кожен склад, проведений одразу."""
    from . import stock
    if closed(db, a):
        return []
    seed(db, a)
    by_wh: dict[str, list] = defaultdict(list)
    rows = db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == a.id)).all()
    eff = effective(db, rows)
    equip = {pid for (pid,) in db.execute(select(Product.id).where(Product.warehouse == "equip"))}
    for m in rows:
        if eff.get(m.id, m.qty):
            by_wh["equip" if m.product_id in equip else (m.warehouse or "single")].append(m)
    docs = []
    for wh, ms in sorted(by_wh.items()):
        doc = stock.new_doc(db, "writeoff", user_id=user_id, wh_from=wh, reason="Списання матеріалів", patient_id=a.patient_id,
                            ext_no=f"visit:{a.id}", note=f"Підстава: візит №{a.id}")
        for m in ms:
            doc.lines.append(StockDocLine(product_id=m.product_id, qty=eff.get(m.id, m.qty), unit_cost=m.unit_cost))
        db.flush()
        stock.post(db, doc)
        docs.append(doc)
    return docs


def reopen(db: Session, a: Appointment) -> int:
    from . import stock
    n = 0
    for d in _docs(db, a.id):
        stock.cancel(db, d)
        n += 1
    return n


def sync(db: Session) -> dict:
    """Оплачені візити — закрити (списати матеріали); повернені / скасовані чеки — відкрити назад."""
    rep = {"closed": 0, "reopened": 0}
    ed = editing(db)
    for a in db.scalars(select(Appointment).where(Appointment.sale_id.is_not(None))):
        if a.id in ed:                     # витратні відкриті для виправлення — списання після «Списати зі складу»
            continue
        s = db.get(Sale, a.sale_id)
        if s and s.status == "paid" and not closed(db, a):
            if close(db, a, s.created_by):
                rep["closed"] += 1
    ids = {int(x.split(":")[1]) for (x,) in db.execute(select(StockDoc.ext_no).where(StockDoc.ext_no.like("visit:%"),
                                                                                     StockDoc.status == "posted")) if x}
    for aid in ids:
        a = db.get(Appointment, aid)
        s = db.get(Sale, a.sale_id) if a and a.sale_id else None
        if not s or s.status != "paid":
            if a:
                rep["reopened"] += reopen(db, a)
    return rep


def sale_service_costs(db: Session, sale: Sale) -> dict:
    """Фактична собівартість послуг чека з витратних усіх його візитів: id послуги → [собівартість, …] по черзі."""
    out: dict = {}
    for aid in (sale.appt_ids or ([sale.appointment_id] if sale.appointment_id else [])):
        a = db.get(Appointment, aid)
        if not a:
            continue
        per = cost_by_service(db, a.id)
        for i, s in enumerate(a.services or []):
            if i in per:
                out.setdefault(s.get("id"), []).append(per[i])
    return out


# ---------------- імпорт з CleverBox ----------------
CB_INACTIVE = "неактивні послуги"


def import_cb(db: Session, data: dict) -> dict:
    """Техкарти з CleverBox (зібрані з веб-кабінету). data = {"cards": [[cb_id, назва, категорія, max, [[кат, склад]],
    [[індекс_кат, cb_product_id, к-сть_в_од_обліку]], [cb_service_id…], коментар]], "products": {cb_id: [назва, од., ціна_од., склад]}}.
    Оновлює техкарту за cb_id (рядки — замінюються), створює відсутні витратні товари, прив'язує послуги.
    Нічого не видаляє: старі техкарти CRM без послуг стають неактивними."""
    from ..parsers.observ import norm
    rep = {"created": 0, "updated": 0, "lines": 0, "products_created": 0, "services_linked": 0, "services_missing": [], "deactivated": 0}
    prods = {int(k): v for k, v in (data.get("products") or {}).items()}
    by_cb = {p.cb_product_id: p for p in db.scalars(select(Product).where(Product.cb_product_id.is_not(None)))}
    cards = data.get("cards") or []
    touched: set[int] = set()
    for row in cards:
        cb_id, name, cat, maxc, cats, items, uses = row[:7]
        comment = row[7] if len(row) > 7 else ""
        card = db.scalar(select(TechCard).where(TechCard.cb_id == int(cb_id)))
        if card:
            rep["updated"] += 1
        else:
            card = TechCard(cb_id=int(cb_id), name=str(name)[:200])
            db.add(card)
            rep["created"] += 1
        cats = [{"name": str(c[0]).strip()[:80] or "Одноразка", "wh": c[1] if c[1] in WH_CAT else "single"} for c in (cats or [])] or list(DEFAULT_CATS)
        card.name, card.category = str(name).strip()[:200], (str(cat).strip()[:200] or None) if cat else None
        card.max_cost = float(maxc) if maxc else None
        card.comment = (str(comment).strip()[:2000] or None) if comment else None
        card.cats, card.mode = cats, "extended"
        card.active = (str(cat or "").strip().lower() != CB_INACTIVE) or bool(uses)
        card.lines.clear()
        for it in items or []:
            vi, pid, qty = int(it[0]), int(it[1]), float(it[2] or 0)
            p = by_cb.get(pid)
            if not p:
                pr = prods.get(pid) or [f"Товар CleverBox #{pid}", None, 0, None]
                p = Product(name=str(pr[0])[:300], name_norm=norm(str(pr[0])), cb_product_id=pid, unit=(str(pr[1]).rstrip(".")[:20] if pr[1] else None),
                            cost=float(pr[2] or 0) or None, warehouse=(pr[3] if len(pr) > 3 and pr[3] in WH_CAT else None) or "single", for_sale=False)
                db.add(p)
                db.flush()
                by_cb[pid] = p
                rep["products_created"] += 1
            c_ = cats[vi] if 0 <= vi < len(cats) else cats[0]
            card.lines.append(TechCardLine(cat=c_["name"], product_id=p.id, qty=qty))
            rep["lines"] += 1
        db.flush()
        touched.add(card.id)
        for sid in uses or []:
            sv = db.get(CbService, int(sid))
            if sv:
                sv.techcard_id = card.id
                rep["services_linked"] += 1
            else:
                rep["services_missing"].append(int(sid))
    db.flush()
    used = {x for (x,) in db.execute(select(CbService.techcard_id).where(CbService.techcard_id.is_not(None)))}
    for c in db.scalars(select(TechCard).where(TechCard.cb_id.is_(None), TechCard.active.is_not(False))):
        if c.id not in used:
            c.active = False
            rep["deactivated"] += 1
    db.merge(SyncState(key="techcards_cb_import", value=f"{datetime.now():%d.%m.%Y %H:%M} · {len(cards)} техкарт"))
    db.commit()
    rep["services_missing"] = sorted(set(rep["services_missing"]))
    return rep
