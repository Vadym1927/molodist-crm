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
def unit_costs(db: Session) -> dict[int, float]:
    """Поточна ціна одиниці обліку: остання закупівля, інакше собівартість товару."""
    out = {p.id: float(p.cost or 0) for p in db.scalars(select(Product))}
    for pp in db.scalars(select(ProductPurchase).where(ProductPurchase.unit_cost > 0).order_by(ProductPurchase.bought_on, ProductPurchase.id)):
        out[pp.product_id] = float(pp.unit_cost)
    return out


def card_cost(card: TechCard, uc: dict[int, float]) -> float:
    return round(sum(float(ln.qty or 0) * uc.get(ln.product_id, 0.0) for ln in card.lines), 2)


def cat_wh(card: TechCard | None, cat: str, product: Product | None = None) -> str:
    for c in (card.cats if card and card.cats else DEFAULT_CATS):
        if c.get("name") == cat and c.get("wh"):
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


def seed(db: Session, a: Appointment) -> bool:
    """Для кожної послуги візиту — витратні з техкарти (якщо ще немає або послугу замінили)."""
    if not a or closed(db, a):
        return False
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


def materials(db: Session, a: Appointment) -> list[dict]:
    """По послугах візиту: категорії → рядки, собівартість, ліміт техкарти."""
    rows = db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == a.id).order_by(VisitMaterial.svc_idx, VisitMaterial.id)).all()
    out = []
    for idx, s in enumerate(a.services or []):
        mine = [m for m in rows if m.svc_idx == idx]
        card = _card_of(db, s.get("id"))
        cats: dict[str, list] = {}
        for m in mine:
            cats.setdefault(m.cat, []).append(m)
        cost = round(sum(m.qty * m.unit_cost for m in mine), 2)
        out.append({"idx": idx, "service": s.get("name"), "service_id": s.get("id"), "card": card, "cats": cats, "cost": cost,
                    "max_cost": card.max_cost if card else None, "over": bool(card and card.max_cost and cost > card.max_cost + 0.005)})
    return out


def cost_by_service(db: Session, appt_id: int) -> dict[int, float]:
    out: dict[int, float] = defaultdict(float)
    for m in db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == appt_id)):
        out[m.svc_idx] += m.qty * m.unit_cost
    return {k: round(v, 2) for k, v in out.items()}


# ---------------- закриття візиту ----------------
def _docs(db: Session, appt_id: int) -> list[StockDoc]:
    return db.scalars(select(StockDoc).where(StockDoc.ext_no == f"visit:{appt_id}", StockDoc.status == "posted")).all()


def closed(db: Session, a: Appointment) -> bool:
    return bool(_docs(db, a.id))


def close(db: Session, a: Appointment, user_id: int | None = None) -> list[StockDoc]:
    """Списати витратні візиту: документ «Списання» на кожен склад, проведений одразу."""
    from . import stock
    if closed(db, a):
        return []
    seed(db, a)
    by_wh: dict[str, list] = defaultdict(list)
    for m in db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == a.id)):
        if m.qty:
            by_wh[m.warehouse or "single"].append(m)
    docs = []
    for wh, ms in sorted(by_wh.items()):
        doc = stock.new_doc(db, "writeoff", user_id=user_id, wh_from=wh, reason="Списання матеріалів", patient_id=a.patient_id,
                            ext_no=f"visit:{a.id}", note=f"Підстава: візит №{a.id}")
        for m in ms:
            doc.lines.append(StockDocLine(product_id=m.product_id, qty=m.qty, unit_cost=m.unit_cost))
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
    for a in db.scalars(select(Appointment).where(Appointment.sale_id.is_not(None))):
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
