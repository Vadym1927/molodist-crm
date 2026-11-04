"""Амортизація обладнання: апарати (ставка за процедуру) і розхідники з ресурсом (імпульси, хвилини).

Логіка (як у Google-таблиці власника):
- кожна позиція амортизації («Амортизація обладнання … IPL», «маніпула», «хвилини мотору») стоїть у техкартах;
  коли візит закривається, ця позиція списується зі складу «Обладнання» → це і є використання апарата;
- нарахування за півмісяць = списано одиниць × ставка; фіксується 16-го (за 1–15) і 1-го (за 16–кінець);
- погашення — переміщення грошей з каси на рахунок фонду; різниця = нараховано − погашено;
- витрати з фонду (ремонт, кредит) — витрата з рахунку фонду, привʼязана до апарата.
Історія до старту обліку в CRM перенесена з таблиці (source=sheet)."""
from __future__ import annotations

import calendar
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AmortAsset, AmortPeriod, AmortTxn, FinOp, Product, StockMove, SyncState, TechCard, TechCardLine

MONTHS = ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
MONTHS_SHORT = ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]
UNITS = {"процедура": "процедур", "імпульс": "імпульсів", "хвилина": "хвилин"}
CATEGORY_PAY = "Амортизація обладнання"
CATEGORY_EXP = "Обслуговування обладнання"
MIGRATED_KEY = "amort_migrated_v1"
SEEDED_KEY = "amort_seeded_v1"
AMORT_PRODUCT_HINTS = ("амортизац", "манімул", "маніпул", "хвилини роботи мотору")


# ---------------- періоди ----------------
def half_of(d: date) -> tuple[int, int, int]:
    return d.year, d.month, 1 if d.day <= 15 else 2


def bounds(year: int, month: int, half: int) -> tuple[datetime, datetime]:
    """[початок, кінець) півмісяця."""
    if half == 1:
        return datetime(year, month, 1), datetime(year, month, 16)
    last = calendar.monthrange(year, month)[1]
    return datetime(year, month, 16), datetime(year, month, last) + timedelta(days=1)


def prev_half(d: date) -> tuple[int, int, int]:
    y, m, h = half_of(d)
    if h == 2:
        return y, m, 1
    pm = date(y, m, 1) - timedelta(days=1)
    return pm.year, pm.month, 2


def label(year: int, month: int, half: int, short: bool = False) -> str:
    last = calendar.monthrange(year, month)[1]
    rng = "1–15" if half == 1 else f"16–{last}"
    return f"{rng}.{month:02d}" if short else f"{rng} {MONTHS[month - 1]} {year}"


# ---------------- використання з журналу складу ----------------
def usage(db: Session, asset: AmortAsset, a: datetime, b: datetime) -> dict:
    """Списання позиції апарата за період: одиниць (sum −qty), процедур (різних візитів/документів)."""
    if not asset.product_id:
        return {"qty": 0.0, "procs": 0}
    rows = db.execute(select(StockMove.qty, StockMove.doc_id, StockMove.ref)
                      .where(StockMove.product_id == asset.product_id, StockMove.qty < 0, StockMove.at >= a, StockMove.at < b,
                             StockMove.kind.in_(("procedure", "writeoff")))).all()
    qty = round(-sum(float(q or 0) for q, _, _ in rows), 4)
    keys = set()
    for _q, doc_id, ref in rows:
        keys.add(("d", doc_id) if doc_id else ("r", (ref or "").split("#")[0].rsplit(":", 1)[0]))
    return {"qty": qty, "procs": len(keys)}


def compute(db: Session, asset: AmortAsset, year: int, month: int, half: int) -> dict:
    a, b = bounds(year, month, half)
    u = usage(db, asset, a, b)
    return {**u, "amount": round(u["qty"] * float(asset.rate or 0), 2)}


def live_period(db: Session, asset: AmortAsset, today: date | None = None) -> dict:
    """Поточний (ще не зафіксований) півмісяць."""
    y, m, h = half_of(today or date.today())
    c = compute(db, asset, y, m, h)
    return {"year": y, "month": m, "half": h, "label": label(y, m, h), **c}


def accrue(db: Session, year: int, month: int, half: int, force: bool = False, source: str = "crm") -> list[dict]:
    """Зафіксувати нарахування за півмісяць для всіх активних позицій. Перенесені з таблиці (sheet) не чіпаємо."""
    out = []
    for asset in db.scalars(select(AmortAsset).where(AmortAsset.active.is_(True)).order_by(AmortAsset.sort, AmortAsset.id)):
        p = db.scalar(select(AmortPeriod).where(AmortPeriod.asset_id == asset.id, AmortPeriod.year == year,
                                                AmortPeriod.month == month, AmortPeriod.half == half))
        if p and (p.source == "sheet" or (p.accrued_at and not force)):
            out.append({"asset": asset, "period": p, "skipped": True})
            continue
        c = compute(db, asset, year, month, half)
        if not p:
            p = AmortPeriod(asset_id=asset.id, year=year, month=month, half=half)
            db.add(p)
        p.procs, p.qty, p.amount, p.source, p.accrued_at = c["procs"], c["qty"], c["amount"], source, datetime.utcnow()
        out.append({"asset": asset, "period": p, "skipped": False})
    db.commit()
    return out


def recompute_open(db: Session) -> None:
    """Оновити незафіксовані crm-періоди (напр. поточний) — на льоту при показі сторінки."""
    for p in db.scalars(select(AmortPeriod).where(AmortPeriod.source == "crm", AmortPeriod.accrued_at.is_(None))):
        asset = db.get(AmortAsset, p.asset_id)
        c = compute(db, asset, p.year, p.month, p.half)
        p.procs, p.qty, p.amount = c["procs"], c["qty"], c["amount"]
    db.commit()


# ---------------- підсумки ----------------
def totals(db: Session, asset: AmortAsset, today: date | None = None) -> dict:
    today = today or date.today()
    periods = db.scalars(select(AmortPeriod).where(AmortPeriod.asset_id == asset.id)).all()
    y, m, h = half_of(today)
    fixed = [p for p in periods if not (p.year == y and p.month == m and p.half == h and p.source == "crm" and not p.accrued_at)]
    accrued = float(asset.accrued_before or 0) + sum(float(p.amount or 0) for p in fixed)
    used = float(asset.used_before or 0) + sum(float(p.qty or 0) for p in fixed)
    live = live_period(db, asset, today)
    fixed_accrued = accrued                                   # зафіксовано (нарахування 1-го / 16-го + перенесене)
    if not any(p.year == y and p.month == m and p.half == h and p.source == "sheet" for p in periods):
        accrued += live["amount"]                             # з поточним півмісяцем, що оновлюється після кожного візиту
        used += live["qty"]
    else:
        live = {**live, "amount": 0.0, "qty": 0.0, "procs": 0, "frozen": True}
    paid = float(db.scalar(select(func.sum(AmortTxn.amount)).where(AmortTxn.asset_id == asset.id, AmortTxn.kind == "pay")) or 0)
    spent = float(db.scalar(select(func.sum(AmortTxn.amount)).where(AmortTxn.asset_id == asset.id, AmortTxn.kind == "expense")) or 0)
    # до погашення — лише зафіксовані періоди: поточний півмісяць додасться в день нарахування
    out = {"accrued": round(accrued, 2), "fixed": round(fixed_accrued, 2), "paid": round(paid, 2), "diff": round(fixed_accrued - paid, 2),
           "spent": round(spent, 2), "live": live, "used": round(used, 2)}
    if asset.kind == "device":
        out["residual"] = round(float(asset.cost or 0) - accrued, 2)
        out["pct"] = round(100 * accrued / float(asset.cost), 1) if asset.cost else 0.0
        yrs = float(asset.life_years or 0)
        out["per_month_plan"] = round(float(asset.cost or 0) / yrs / 12, 2) if yrs else None
        if asset.commissioned_at and yrs:
            out["end_at"] = asset.commissioned_at.replace(year=asset.commissioned_at.year + int(yrs)) if yrs == int(yrs) \
                else asset.commissioned_at + timedelta(days=int(yrs * 365))
    else:
        res = float(asset.resource or 0)
        out["resource_left"] = round(res - used, 2)
        out["pct"] = round(100 * used / res, 1) if res else 0.0
        out["over"] = used > res > 0
        out["residual"] = round(float(asset.cost or 0) - accrued, 2)
    return out


def series(db: Session, asset: AmortAsset, year: int) -> list[dict]:
    """12 місяців × 2 півмісяці для графіка і таблиці (з поточним незафіксованим)."""
    ps = {(p.month, p.half): p for p in db.scalars(select(AmortPeriod).where(AmortPeriod.asset_id == asset.id, AmortPeriod.year == year))}
    today = date.today()
    out = []
    for m in range(1, 13):
        row = {"month": m, "name": MONTHS[m - 1], "short": MONTHS_SHORT[m - 1]}
        for h in (1, 2):
            p = ps.get((m, h))
            cur = (year, m, h) == half_of(today)
            if p:
                d = {"procs": p.procs, "qty": p.qty, "amount": p.amount, "source": p.source, "fixed": bool(p.accrued_at or p.source == "sheet")}
            elif cur or (date(year, m, 1) <= today and (year, m, h) < half_of(today)):
                c = compute(db, asset, year, m, h)
                d = {**c, "source": "crm", "fixed": False}
            else:
                d = {"procs": 0, "qty": 0, "amount": 0, "source": "", "fixed": False}
            d["current"] = cur
            row[f"h{h}"] = d
        row["amount"] = round(row["h1"]["amount"] + row["h2"]["amount"], 2)
        row["procs"] = row["h1"]["procs"] + row["h2"]["procs"]
        row["qty"] = round(row["h1"]["qty"] + row["h2"]["qty"], 2)
        out.append(row)
    return out


def series_all(db: Session, asset: AmortAsset, today: date | None = None) -> list[dict]:
    """Усі місяці від введення в експлуатацію (або першого нарахування) до поточного — з накопиченням."""
    today = today or date.today()
    first = db.scalar(select(func.min(AmortPeriod.year * 100 + AmortPeriod.month)).where(AmortPeriod.asset_id == asset.id))
    starts = [today.year * 100 + today.month]
    if asset.commissioned_at:
        starts.append(asset.commissioned_at.year * 100 + asset.commissioned_at.month)
    if first:
        starts.append(int(first))
    y, m = divmod(min(starts), 100)
    by_year: dict[int, list] = {}
    out, cum = [], float(asset.accrued_before or 0)
    while (y, m) <= (today.year, today.month):
        if y not in by_year:
            by_year[y] = series(db, asset, y)
        row = dict(by_year[y][m - 1])
        cum += row["amount"]
        row.update(year=y, short=f"{MONTHS_SHORT[m - 1]} {str(y)[2:]}", cum=round(cum, 2))
        out.append(row)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def by_year_totals(rows: list[dict]) -> list[dict]:
    agg: dict[int, dict] = {}
    for r in rows:
        a = agg.setdefault(r["year"], {"year": r["year"], "amount": 0.0, "procs": 0, "qty": 0.0, "months": 0})
        a["amount"] += r["amount"]
        a["procs"] += r["procs"]
        a["qty"] += r["qty"]
        a["months"] += 1
    return [dict(v, amount=round(v["amount"], 2), qty=round(v["qty"], 2)) for _, v in sorted(agg.items())]


def years(db: Session) -> list[int]:
    ys = {y for (y,) in db.execute(select(AmortPeriod.year).distinct())} | {date.today().year}
    return sorted(ys, reverse=True)


def overview(db: Session, today: date | None = None) -> list[dict]:
    recompute_open(db)
    out = []
    for a in db.scalars(select(AmortAsset).order_by(AmortAsset.active.desc(), AmortAsset.kind.desc(), AmortAsset.sort, AmortAsset.id)):
        t = totals(db, a, today)
        out.append({"asset": a, "t": t, "months": series(db, a, (today or date.today()).year)})
    return out


def fund_summary(db: Session, rows: list[dict]) -> dict:
    accrued = sum(r["t"]["accrued"] for r in rows if r["asset"].active)
    paid = sum(r["t"]["paid"] for r in rows if r["asset"].active)
    spent = float(db.scalar(select(func.sum(AmortTxn.amount)).where(AmortTxn.kind == "expense")) or 0)
    live = sum(r["t"]["live"]["amount"] for r in rows if r["asset"].active)
    fixed = sum(r["t"]["fixed"] for r in rows if r["asset"].active)
    return {"accrued": round(accrued, 2), "paid": round(paid, 2), "diff": round(fixed - paid, 2), "spent": round(spent, 2),
            "live": round(live, 2), "live_label": rows[0]["t"]["live"]["label"] if rows else ""}


# ---------------- техкарти ----------------
def techcards_for(db: Session, asset: AmortAsset) -> list[dict]:
    if not asset.product_id:
        return []
    out = []
    for ln in db.scalars(select(TechCardLine).where(TechCardLine.product_id == asset.product_id)):
        card = db.get(TechCard, ln.card_id)
        if card:
            out.append({"card": card, "qty": ln.qty, "amount": round(float(ln.qty or 0) * float(asset.rate or 0), 2)})
    out.sort(key=lambda x: x["card"].name.lower())
    return out


def candidates(db: Session) -> list[Product]:
    """Товари, схожі на позиції амортизації — для вибору в налаштуваннях."""
    return [p for p in db.scalars(select(Product).order_by(Product.name))
            if any(h in (p.name or "").lower() for h in AMORT_PRODUCT_HINTS)]


# ---------------- гроші ----------------
def pay(db: Session, asset: AmortAsset, day: date, amount: float, cashbox_from: str, user_id: int | None = None,
        note: str | None = None, move_money: bool = True) -> AmortTxn:
    """Погашення: переміщення грошей з каси на рахунок фонду (FinOp transfer) + запис у фонді."""
    fin = None
    if move_money and asset.cashbox and cashbox_from and cashbox_from != asset.cashbox:
        fin = FinOp(day=day, kind="transfer", category=CATEGORY_PAY, amount=round(amount, 2), cashbox=cashbox_from,
                    cashbox_to=asset.cashbox, counterparty=asset.name[:200], comment=(note or f"Амортизація: {asset.name}")[:500],
                    created_by=user_id, ref=f"amort:{asset.id}")
        db.add(fin)
        db.flush()
    t = AmortTxn(asset_id=asset.id, kind="pay", day=day, amount=round(amount, 2), cashbox=cashbox_from or None,
                 note=note, finop_id=fin.id if fin else None, created_by=user_id)
    db.add(t)
    db.commit()
    return t


def expense(db: Session, asset: AmortAsset | None, day: date, amount: float, cashbox: str, note: str, user_id: int | None = None,
            move_money: bool = True) -> AmortTxn:
    """Витрата з фонду (ремонт, запчастини, кредит за апарат)."""
    fin = None
    if move_money and cashbox:
        fin = FinOp(day=day, kind="expense", category=CATEGORY_EXP, amount=round(amount, 2), cashbox=cashbox,
                    counterparty=(asset.name if asset else None), comment=note[:500], created_by=user_id,
                    ref=f"amort:{asset.id}" if asset else "amort:fund")
        db.add(fin)
        db.flush()
    t = AmortTxn(asset_id=asset.id if asset else None, kind="expense", day=day, amount=round(amount, 2), cashbox=cashbox or None,
                 note=note, finop_id=fin.id if fin else None, created_by=user_id)
    db.add(t)
    db.commit()
    return t


def delete_txn(db: Session, t: AmortTxn) -> None:
    if t.finop_id:
        f = db.get(FinOp, t.finop_id)
        if f and not f.deleted_at:
            f.deleted_at = datetime.utcnow()
    db.delete(t)
    db.commit()


def txns(db: Session, asset_id: int | None = None, kind: str | None = None) -> list[AmortTxn]:
    q = select(AmortTxn).order_by(AmortTxn.day.desc(), AmortTxn.id.desc())
    if asset_id is not None:
        q = q.where(AmortTxn.asset_id == asset_id)
    if kind:
        q = q.where(AmortTxn.kind == kind)
    return db.scalars(q).all()


# ---------------- перенесення з таблиці + міграція позицій ----------------
def _seed() -> dict:
    return json.loads((Path(__file__).with_name("amort_seed.json")).read_text("utf-8"))


def _flag(db: Session, key: str) -> bool:
    st = db.get(SyncState, key)
    return bool(st and st.value)


def _set_flag(db: Session, key: str) -> None:
    st = db.get(SyncState, key) or SyncState(key=key)
    st.value, st.updated_at = datetime.now().isoformat(timespec="seconds"), datetime.utcnow()
    db.merge(st)
    db.commit()


OPERATIONAL_KEY = "amort_operational"
OPERATIONAL_DEFAULT = 110000.0


def operational(db: Session) -> float:
    """Операційна частина спільного рахунку «Амортизаційний + Операційний Фонд» — в амортизації не показується."""
    st = db.get(SyncState, OPERATIONAL_KEY)
    try:
        return float(st.value) if st and st.value not in (None, "") else OPERATIONAL_DEFAULT
    except ValueError:
        return OPERATIONAL_DEFAULT


def set_operational(db: Session, amount: float) -> None:
    st = db.get(SyncState, OPERATIONAL_KEY) or SyncState(key=OPERATIONAL_KEY)
    st.value, st.updated_at = str(round(max(amount, 0.0), 2)), datetime.utcnow()
    db.merge(st)
    db.commit()


def _product_by_name(db: Session, name: str) -> Product | None:
    nm = " ".join(name.split()).lower()
    for p in db.scalars(select(Product)):
        if " ".join((p.name or "").split()).lower() == nm:
            return p
    return None


def seed_from_sheet(db: Session) -> dict:
    """Створює позиції амортизації й переносить півмісяці, погашення й витрати з таблиці. Повторний запуск нічого не дублює."""
    data = _seed()
    rep = {"assets": 0, "periods": 0, "txns": 0, "no_product": []}
    by_key: dict[str, AmortAsset] = {}
    existing = {a.name: a for a in db.scalars(select(AmortAsset))}
    for i, x in enumerate(data["assets"]):
        a = existing.get(x["name"])
        if not a:
            a = AmortAsset(name=x["name"], kind=x.get("kind", "device"), cost=x["cost"], life_years=x.get("life"), resource=x.get("resource"),
                           unit=x.get("unit", "процедура"), rate=x["rate"], avg_month=x.get("avg"), cashbox=data["cashbox"],
                           commissioned_at=date.fromisoformat(x["since"]) if x.get("since") else None,
                           accrued_before=x.get("accrued_before", 0), sort=i, active=True)
            db.add(a)
            db.flush()
            rep["assets"] += 1
        if not a.product_id:
            p = _product_by_name(db, x["product"])
            if p:
                a.product_id = p.id
            else:
                rep["no_product"].append(x["product"])
        by_key[x["key"]] = a
    for x in data["assets"]:
        if x.get("parent") and by_key.get(x["parent"]):
            by_key[x["key"]].parent_id = by_key[x["parent"]].id
    db.flush()
    for x in data["assets"]:
        a = by_key[x["key"]]
        have = {(p.year, p.month, p.half) for p in db.scalars(select(AmortPeriod).where(AmortPeriod.asset_id == a.id))}
        for y, m, p1, p2, q1, q2, s1, s2 in x["periods"]:
            for h, procs, qty, amt in ((1, p1, q1, s1), (2, p2, q2, s2)):
                if (y, m, h) in have:
                    continue
                db.add(AmortPeriod(asset_id=a.id, year=y, month=m, half=h, procs=procs, qty=qty, amount=amt, source="sheet",
                                   accrued_at=datetime(y, m, 16) if h == 1 else datetime(y, m, 1) + timedelta(days=32)))
                rep["periods"] += 1
        if x.get("paid") and not db.scalar(select(AmortTxn.id).where(AmortTxn.asset_id == a.id, AmortTxn.source == "sheet", AmortTxn.kind == "pay")):
            db.add(AmortTxn(asset_id=a.id, kind="pay", day=date(2026, 10, 31), amount=x["paid"], source="sheet",
                            note="Перенесено з таблиці: відраховано на рахунок до 31.10.2026"))
            rep["txns"] += 1
    for e in data.get("expenses", []):
        a = by_key.get(e["asset"]) if e.get("asset") else None
        dup = db.scalar(select(AmortTxn.id).where(AmortTxn.kind == "expense", AmortTxn.source == "sheet", AmortTxn.note == e["note"]))
        if dup:
            continue
        db.add(AmortTxn(asset_id=a.id if a else None, kind="expense", day=date.fromisoformat(e["day"]), amount=e["amount"],
                        cashbox=data["cashbox"], note=e["note"], source="sheet"))
        rep["txns"] += 1
    db.commit()
    _set_flag(db, SEEDED_KEY)
    return rep


def migrate_products(db: Session) -> dict:
    """Позиції амортизації → склад «Обладнання»; у техкартах — категорія «Обладнання»; старі дублі (маніпула, мотор) прибрати."""
    data = _seed()
    rep = {"moved": 0, "lines": 0, "dupes": 0, "deactivated": 0}
    names = {x["product"] for x in data["assets"]}
    ids: set[int] = set()
    for p in db.scalars(select(Product)):
        if " ".join((p.name or "").split()) in names or any(h in (p.name or "").lower() for h in ("амортизація обладнання",)):
            ids.add(p.id)
            if p.warehouse != "equip":
                p.warehouse = "equip"
                rep["moved"] += 1
            if p.for_expense is not True:
                p.for_expense = True
    legacy = {p.id: p for p in db.scalars(select(Product)) if " ".join((p.name or "").split()) in set(data.get("legacy_products", []))}
    for lid, lp in legacy.items():
        if lp.warehouse != "equip":
            lp.warehouse = "equip"
        if lp.active is not False:
            lp.active = False
            rep["deactivated"] += 1
    for card in db.scalars(select(TechCard)):
        cats = list(card.cats or [])
        if not any(c.get("name") == "Обладнання" for c in cats):
            cats.append({"name": "Обладнання", "wh": "equip"})
        changed = False
        for c in cats:
            if c.get("name") == "Обладнання" and c.get("wh") != "equip":
                c["wh"], changed = "equip", True
        has_new = {ln.product_id for ln in card.lines if ln.product_id in ids}
        for ln in list(card.lines):
            if ln.product_id in ids and ln.cat != "Обладнання":
                ln.cat, changed = "Обладнання", True
                rep["lines"] += 1
            if ln.product_id in legacy:                     # стара маніпула / старі хвилини поруч з новими — дубль
                card.lines.remove(ln)
                rep["dupes"] += 1
                changed = True
        # подвоєний рядок одного й того ж товару (Мікротоки ×2 у RESET) — лишаємо один з сумарною кількістю
        seen: dict[int, TechCardLine] = {}
        for ln in list(card.lines):
            if ln.product_id in ids:
                if ln.product_id in seen:
                    seen[ln.product_id].qty = float(seen[ln.product_id].qty or 0) + float(ln.qty or 0)
                    card.lines.remove(ln)
                    rep["dupes"] += 1
                    changed = True
                else:
                    seen[ln.product_id] = ln
        if changed or cats != (card.cats or []):
            card.cats = cats
    db.commit()
    _set_flag(db, MIGRATED_KEY)
    return rep


def ensure_setup(db: Session) -> dict | None:
    """Один раз після оновлення: перенести позиції й історію з таблиці."""
    if _flag(db, MIGRATED_KEY) and _flag(db, SEEDED_KEY):
        return None
    out = {}
    if not _flag(db, SEEDED_KEY):
        out["seed"] = seed_from_sheet(db)
    if not _flag(db, MIGRATED_KEY):
        out["migrate"] = migrate_products(db)
    return out


# ---------------- планове нарахування ----------------
def run_scheduled(db: Session, today: date | None = None) -> str:
    """16-го — за 1–15, 1-го — за 16–кінець попереднього місяця. Повертає текст для Telegram."""
    today = today or date.today()
    y, m, h = prev_half(today)
    rows = accrue(db, y, m, h)
    done = [r for r in rows if not r["skipped"]]
    if not done:
        return ""
    total = sum(r["period"].amount for r in done)
    lines = [f"• {r['asset'].name}: {r['period'].amount:,.0f} ₴ ({r['period'].procs:g} проц., {r['period'].qty:g} {UNITS.get(r['asset'].unit, 'од.')})".replace(",", " ")
             for r in done if r["period"].amount]
    return (f"🏭 <b>Амортизація за {label(y, m, h)}</b>: нараховано {total:,.0f} ₴\n".replace(",", " ") + "\n".join(lines)
            + ("\n(без руху за період)" if not lines else ""))
