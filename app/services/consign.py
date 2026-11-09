"""Товар під реалізацію (TEBISKIN, AG SKIN): платимо постачальнику лише за продане.

Поставка → рядки ConsignLot (товар, к-сть, ціна банки). Звіт реалізації за період: продажі товарів постачальника
(рухи складу kind=sale: CleverBox і каса CRM) розписуються на найстаріші поставки, де ще є непродані банки (FIFO) —
за ціною саме тієї поставки. «Зафіксувати» → ConsignReport, у поставках збільшується sold. Оплата однією сумою →
рознесення по звітах і поставках від найстаріших (SupplierInvoice.crm_paid), нові поставки не чіпаються.
Старт — з таблиць власника (consign_seed.json): непродані банки по кожній поставці + знижки / повернення."""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (ConsignLot, ConsignPay, ConsignReport, FinOp, Product, StockMove, Supplier, SupplierInvoice,
                      SyncState)

SEED_KEY = "consign_seed_v1"
DEFAULT = ("TEBISKIN", "AG SKIN")


def _norm(s: str | None) -> str:
    s = (s or "").lower().replace("ʼ", "'").replace("’", "'")
    s = re.sub(r"[^\w]+", " ", s)
    s = re.sub(r"(\d)([^\W\d_])", r"\1 \2", s)          # «50мл» → «50 мл»
    return " ".join(s.split())


def suppliers(db: Session) -> list[str]:
    return [s.name for s in db.scalars(select(Supplier).where(Supplier.consignment.is_(True)).order_by(Supplier.name))]


def _match_product(db: Session, name: str, sku: str | None, cache: dict) -> int | None:
    if "all" not in cache:
        cache["all"] = db.scalars(select(Product)).all()
        cache["sku"] = {(p.sku or "").strip().upper(): p.id for p in cache["all"] if p.sku}
    if sku and sku.upper() in cache["sku"]:
        return cache["sku"][sku.upper()]
    n = _norm(name)
    best, score = None, 0.0
    toks = set(n.split())
    for p in cache["all"]:
        pn = _norm(p.name)
        if pn == n:
            return p.id
        pt = set(pn.split())
        if not toks or not pt:
            continue
        sc = len(toks & pt) / len(toks | pt)
        if sc > score:
            best, score = p.id, sc
    return best if score >= 0.6 else None


def ensure_setup(db: Session) -> bool:
    """Позначає TEBISKIN і AG SKIN як «під реалізацію», заводить залишки з таблиць власника і знижки / повернення."""
    if db.get(SyncState, SEED_KEY):
        return False
    for s in db.scalars(select(Supplier)):
        if s.name.strip().upper() in DEFAULT:
            s.consignment = True
    path = os.path.join(os.path.dirname(__file__), "consign_seed.json")
    data = json.load(open(path, encoding="utf-8"))
    cache: dict = {}
    if not db.scalar(select(func.count(ConsignLot.id))):
        for x in data["lots"]:
            db.add(ConsignLot(supplier=x["supplier"], invoice=x["invoice"], inv_date=date.fromisoformat(x["date"]), name=x["name"][:300],
                              sku=x.get("sku"), qty=float(x["qty"]), cost=float(x["cost"]), sold=0.0, source="seed", note=x.get("note"),
                              product_id=_match_product(db, x["name"], x.get("sku"), cache)))
    for sup, d0 in (data.get("start") or {}).items():
        set_start(db, sup, date.fromisoformat(d0), commit=False)
    inv = {x.cb_number: x for x in db.scalars(select(SupplierInvoice))}
    for a in data["adjust"]:
        x = inv.get(a["invoice"])
        if x and not x.adjust:
            x.adjust, x.adjust_note = float(a["amount"]), a["note"]
    db.merge(SyncState(key=SEED_KEY, value=datetime.now().isoformat(timespec="minutes"), updated_at=datetime.utcnow()))
    db.commit()
    return True


def _invoices(db: Session, supplier: str) -> dict:
    low = supplier.strip().lower()
    return {x.cb_number: x for x in db.scalars(select(SupplierInvoice).where(SupplierInvoice.supplier.is_not(None))) if x.supplier.strip().lower() == low}


def lots(db: Session, supplier: str) -> list[ConsignLot]:
    return db.scalars(select(ConsignLot).where(ConsignLot.supplier == supplier).order_by(ConsignLot.inv_date, ConsignLot.id)).all()


def product_ids(db: Session, supplier: str) -> set[int]:
    """Товари постачальника: з поставок під реалізацію + картки товарів з цим постачальником."""
    ids = {l.product_id for l in lots(db, supplier) if l.product_id}
    low = supplier.strip().lower()
    ids |= {pid for pid, sup in db.execute(select(Product.id, Product.supplier).where(Product.supplier.is_not(None))) if sup.strip().lower() == low}
    return ids


def candidate_products(db: Session, supplier: str) -> list:
    """Для зіставлення: товари постачальника + товари, у назві / бренді яких є назва постачальника чи брендів із його поставок."""
    ids = product_ids(db, supplier)
    low = supplier.lower()
    keys = {"tebiskin", "laser doctor"} if "tebiskin" in low else ({"ag skin", " ag "} if low == "ag skin" else {_norm(supplier)})
    out = []
    for p in db.scalars(select(Product).order_by(Product.name)):
        hay = " " + _norm(f"{p.name} {p.brand or ''} {p.supplier or ''}") + " "
        if p.id in ids or any(k in hay for k in keys) or (supplier.upper() == "AG SKIN" and (p.sku or "").upper().startswith("AG")):
            out.append(p)
    return out


def sales(db: Session, supplier: str, d_from: date, d_to: date) -> dict[int, float]:
    """Продано за період (шт.) по товарах. З дати старту обліку складу в CRM — рухи «продаж» (CleverBox + каса CRM);
    до неї — продажі CleverBox (ProductSale), бо рухів складу тоді ще не було."""
    from ..models import ProductSale
    from . import stock as st
    ids = product_ids(db, supplier)
    if not ids:
        return {}
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to + timedelta(days=1), datetime.min.time())
    start = st.ledger_start(db)
    split = min(max(start, a), b) if start else a
    out: dict = defaultdict(float)
    if split > a:                                            # до старту обліку CRM — продажі CleverBox
        cb = {p.cb_product_id: p.id for p in db.scalars(select(Product).where(Product.id.in_(ids), Product.cb_product_id.is_not(None)))}
        if cb:
            for pid, q in db.execute(select(ProductSale.product_id, ProductSale.quantity)
                                     .where(ProductSale.product_id.in_(list(cb)), ProductSale.sold_at >= a, ProductSale.sold_at < split)):
                out[cb[pid]] += float(q or 1)
    if split < b:
        for pid, q in db.execute(select(StockMove.product_id, StockMove.qty)
                                 .where(StockMove.product_id.in_(ids), StockMove.at >= split, StockMove.at < b, StockMove.kind == "sale")):
            out[pid] += -float(q or 0)
    return {k: round(v, 3) for k, v in out.items() if abs(v) > 1e-9}


def start_of(db: Session, supplier: str) -> date | None:
    """З якої дати рахувати продажі для першого звіту в CRM (наступний день після останньої реалізації в таблиці)."""
    st = db.get(SyncState, f"consign_start:{supplier}")
    try:
        return date.fromisoformat(st.value) if st and st.value else None
    except ValueError:
        return None


def set_start(db: Session, supplier: str, d: date, commit: bool = True) -> None:
    db.merge(SyncState(key=f"consign_start:{supplier}", value=d.isoformat(), updated_at=datetime.utcnow()))
    if commit:
        db.commit()


def last_report_end(db: Session, supplier: str) -> date | None:
    return db.scalar(select(func.max(ConsignReport.d_to)).where(ConsignReport.supplier == supplier, ConsignReport.status == "fixed"))


def build_report(db: Session, supplier: str, d_from: date, d_to: date) -> dict:
    """Чернетка звіту: продане за період → найстаріші поставки з непроданими банками (FIFO), сума за ціною поставки."""
    sold = sales(db, supplier, d_from, d_to)
    names = {p.id: p.name for p in db.scalars(select(Product).where(Product.id.in_(list(sold) or [0])))}
    pool: dict = defaultdict(list)
    for l in lots(db, supplier):
        if l.product_id and l.qty - l.sold > 1e-9:
            pool[l.product_id].append(l)
    lines = []
    for pid, q in sorted(sold.items(), key=lambda kv: names.get(kv[0], "")):
        left, alloc = q, []
        for l in pool.get(pid, []):
            if left <= 1e-9:
                break
            take = round(min(l.qty - l.sold, left), 3)
            alloc.append({"lot": l.id, "invoice": l.invoice, "date": l.inv_date.isoformat(), "qty": take, "cost": l.cost, "sum": round(take * l.cost, 2)})
            left = round(left - take, 3)
        lines.append({"pid": pid, "name": names.get(pid, f"товар #{pid}"), "qty": q, "alloc": alloc,
                      "sum": round(sum(a["sum"] for a in alloc), 2), "short": left if left > 1e-9 else 0})
    return {"supplier": supplier, "d_from": d_from, "d_to": d_to, "lines": lines, "total": round(sum(x["sum"] for x in lines), 2),
            "qty": round(sum(x["qty"] for x in lines), 3), "short": [x for x in lines if x["short"]]}


def fix_report(db: Session, supplier: str, d_from: date, d_to: date, user_id: int | None = None) -> ConsignReport:
    r = build_report(db, supplier, d_from, d_to)
    for ln in r["lines"]:
        for a in ln["alloc"]:
            lot = db.get(ConsignLot, a["lot"])
            lot.sold = round((lot.sold or 0) + a["qty"], 3)
    lines = [{**ln, "alloc": [{**a} for a in ln["alloc"]]} for ln in r["lines"]]
    rep = ConsignReport(supplier=supplier, d_from=d_from, d_to=d_to, status="fixed", lines=lines, total=r["total"], paid=0.0, created_by=user_id)
    db.add(rep)
    db.commit()
    return rep


def cancel_report(db: Session, rep: ConsignReport) -> None:
    if rep.paid > 0.009:
        raise ValueError("За звітом уже є оплата — спершу скасуйте оплату")
    for ln in rep.lines or []:
        for a in ln.get("alloc", []):
            lot = db.get(ConsignLot, a["lot"])
            if lot:
                lot.sold = round(max((lot.sold or 0) - a["qty"], 0), 3)
    rep.status = "cancelled"
    db.commit()


def pay(db: Session, supplier: str, amount: float, day: date, cashbox: str | None = None, article: str | None = None,
        user_id: int | None = None, note: str = "") -> ConsignPay:
    """Оплата однією сумою → звіти від найстарішого, усередині звіту — поставки від найстарішої (SupplierInvoice.crm_paid)."""
    left = round(float(amount), 2)
    alloc = []
    reps = db.scalars(select(ConsignReport).where(ConsignReport.supplier == supplier, ConsignReport.status == "fixed")
                      .order_by(ConsignReport.d_to, ConsignReport.id)).all()
    inv = _invoices(db, supplier)
    for rep in reps:
        due = round(rep.total - (rep.paid or 0), 2)
        if due <= 0.009 or left <= 0.009:
            continue
        # скільки вже оплачено по рядках цього звіту — йдемо по поставках від найстарішої
        per_inv: dict = defaultdict(float)
        for ln in rep.lines or []:
            for a in ln.get("alloc", []):
                per_inv[(a["date"], a["invoice"])] += a["sum"]
        already = rep.paid or 0
        for (d, no), s in sorted(per_inv.items()):
            take_from_paid = min(already, s)
            already -= take_from_paid
            rest = round(s - take_from_paid, 2)
            if rest <= 0.009 or left <= 0.009:
                continue
            t = round(min(rest, left), 2)
            left = round(left - t, 2)
            rep.paid = round((rep.paid or 0) + t, 2)
            if no in inv:
                inv[no].crm_paid = round((inv[no].crm_paid or 0) + t, 2)
            alloc.append({"report": rep.id, "invoice": no, "date": d, "amount": t})
    if left > 0.009:
        alloc.append({"report": None, "invoice": None, "amount": left, "note": "аванс — зарахується в наступні звіти"})
    fin = None
    if cashbox:
        fin = FinOp(day=day, kind="expense", category=(article or "Закупівля косметики")[:60], amount=round(float(amount), 2), cashbox=cashbox,
                    counterparty=supplier[:200], comment=(note or f"Оплата реалізації {supplier}")[:500], created_by=user_id, ref=f"consign:{supplier}")
        db.add(fin)
        db.flush()
    p = ConsignPay(supplier=supplier, day=day, amount=round(float(amount), 2), alloc=alloc, finop_id=fin.id if fin else None,
                   note=note or None, created_by=user_id)
    db.add(p)
    db.commit()
    return p


def cancel_pay(db: Session, p: ConsignPay) -> None:
    inv = _invoices(db, p.supplier)
    for a in p.alloc or []:
        if a.get("report"):
            rep = db.get(ConsignReport, a["report"])
            if rep:
                rep.paid = round(max((rep.paid or 0) - a["amount"], 0), 2)
        if a.get("invoice") in inv:
            x = inv[a["invoice"]]
            x.crm_paid = round(max((x.crm_paid or 0) - a["amount"], 0), 2) or None
    if p.finop_id:
        f = db.get(FinOp, p.finop_id)
        if f and not f.deleted_at:
            f.deleted_at = datetime.utcnow()
    db.delete(p)
    db.commit()


def add_lot(db: Session, supplier: str, invoice: str, inv_date: date, product_id: int, qty: float, cost: float) -> ConsignLot:
    p = db.get(Product, product_id)
    l = ConsignLot(supplier=supplier, invoice=invoice or None, inv_date=inv_date, product_id=product_id, name=(p.name if p else "?")[:300],
                   sku=p.sku if p else None, qty=qty, cost=cost, sold=0.0, source="manual")
    db.add(l)
    db.commit()
    return l


def overview(db: Session, supplier: str, today: date | None = None) -> dict:
    today = today or date.today()
    ls = lots(db, supplier)
    by_inv: dict = {}
    for l in ls:
        g = by_inv.setdefault((l.inv_date, l.invoice), {"date": l.inv_date, "invoice": l.invoice, "lots": [], "left": 0.0, "qty": 0.0})
        g["lots"].append(l)
        g["left"] += (l.qty - l.sold) * l.cost
        g["qty"] += l.qty - l.sold
    invs = _invoices(db, supplier)
    groups = sorted(by_inv.values(), key=lambda g: (g["date"], g["invoice"] or ""))
    for g in groups:
        g["inv"] = invs.get(g["invoice"])
        g["left"] = round(g["left"], 2)
    reps = db.scalars(select(ConsignReport).where(ConsignReport.supplier == supplier, ConsignReport.status == "fixed")
                      .order_by(ConsignReport.d_to.desc(), ConsignReport.id.desc())).all()
    pays = db.scalars(select(ConsignPay).where(ConsignPay.supplier == supplier).order_by(ConsignPay.day.desc(), ConsignPay.id.desc())).all()
    last = last_report_end(db, supplier)
    since = (last + timedelta(days=1)) if last else start_of(db, supplier)
    pending = build_report(db, supplier, since, today) if since and since <= today else None
    no_product = [l for l in ls if not l.product_id and l.qty - l.sold > 1e-9]
    lots_without = [x for x in invs.values() if x.debt > 1 and x.cb_number not in {l.invoice for l in ls}]
    return {"supplier": supplier, "groups": [g for g in groups if g["qty"] > 1e-9], "closed": [g for g in groups if g["qty"] <= 1e-9],
            "on_hand": round(sum(g["left"] for g in groups), 2), "on_hand_qty": round(sum(g["qty"] for g in groups), 2),
            "reports": reps, "pays": pays, "to_pay": round(sum(r.total - (r.paid or 0) for r in reps), 2),
            "paid": round(sum(p.amount for p in pays), 2), "last": last, "since": since, "pending": pending,
            "no_product": no_product, "quick": [(n, since + timedelta(days=n - 1)) for n in (10, 15, 20)] if since else [], "lots_without": sorted(lots_without, key=lambda x: x.inv_date), "today": today}


def to_xlsx(r: dict, title: str) -> bytes:
    """Звіт реалізації для постачальника: товар, к-сть, ціна, сума (по поставках)."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Реалізація"
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["Товар", "К-сть", "Ціна", "Сума", "Поставка"])
    for c in ws[3]:
        c.font = Font(bold=True)
    for ln in r["lines"]:
        for a in ln["alloc"]:
            ws.append([ln["name"], a["qty"], a["cost"], a["sum"], f"№{a['invoice'] or '—'} від {date.fromisoformat(a['date']):%d.%m.%Y}"])
    ws.append(["Разом", sum(a["qty"] for ln in r["lines"] for a in ln["alloc"]), None, r["total"]])
    ws[ws.max_row][0].font = Font(bold=True)
    ws[ws.max_row][3].font = Font(bold=True)
    ws.column_dimensions["A"].width = 70
    ws.column_dimensions["E"].width = 22
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
