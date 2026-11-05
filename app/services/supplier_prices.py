"""Прайс постачальника по бренду → закупівельні ціни в «Плануванні ціни реалізації».

Завантаження (Excel .xlsx / .xls / CSV): шукаємо рядок заголовків, колонки «артикул / назва / штрихкод / ціна закупівлі / РРЦ»,
валюту (з заголовків або вибрану). Рядки зіставляються з товарами бренду: артикул → штрихкод → назва.
Прайс зберігається (файл, дата прайсу, дата й час завантаження) і видно в картці постачальника.
«Застосувати» ставить Product.cost_currency / cost_fx (закупівля у валюті) і, за бажанням, rrp_fx (РРЦ)."""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, Supplier, SupplierPrice
from ..parsers.observ import norm

COST_KEYS = ("закуп", "вхідн", "вхідна", "опт", "дилер", "дистриб", "салон", "професійн", "собіварт", "wholesale", "purchase",
             "net price", "dealer", "cost", "закупочн", "оптов")
RRP_KEYS = ("ррц", "роздр", "рекоменд", "retail", "rrp", "msrp", "рц ")
PRICE_KEYS = ("ціна", "цена", "price", "вартість", "стоимость", "prijs", "preis")
SKU_KEYS = ("артикул", "арт.", "код товар", "sku", "article", "art.", "ref", "код")
BAR_KEYS = ("штрих", "barcode", "ean", "штрихкод")
NAME_KEYS = ("назва", "найменування", "наименование", "название", "товар", "продукц", "name", "product", "description", "опис")


def _s(v) -> str:
    return " ".join(str(v if v is not None else "").split())


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    t = re.sub(r"[^\d,.\-]", "", str(v).replace("\xa0", "").replace(" ", ""))
    if t.count(",") and t.count("."):
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    try:
        x = float(t)
    except ValueError:
        return None
    return x if x > 0 else None


def _cur(text: str) -> str | None:
    t = (text or "").lower()
    if "$" in t or "usd" in t or "дол" in t:
        return "USD"
    if "€" in t or "eur" in t or "євро" in t or "евро" in t:
        return "EUR"
    if "грн" in t or "uah" in t or "₴" in t:
        return "UAH"
    return None


def read_table(data: bytes, filename: str) -> list[list]:
    """Файл → таблиця (усі аркуші підряд). xlsx / xls / csv."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        out = []
        for ws in wb.worksheets:
            out += [list(r) for r in ws.iter_rows(values_only=True)]
            out.append([])
        return out
    if name.endswith(".xls"):
        import xlrd
        bk = xlrd.open_workbook(file_contents=data, ignore_workbook_corruption=True)
        out = []
        for sh in bk.sheets():
            out += [sh.row_values(i) for i in range(sh.nrows)]
            out.append([])
        return out
    if name.endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", "ignore")
        try:
            dialect = csv.Sniffer().sniff(text[:4000], delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        return [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    raise ValueError("Підтримуються прайси Excel (.xlsx, .xls) і CSV")


def _has(cell: str, keys) -> bool:
    return any(k in cell for k in keys)


def _header(row: list) -> dict | None:
    cells = [_s(c).lower() for c in row]
    if sum(1 for c in cells if c) < 2:
        return None
    cols: dict = {}
    for i, c in enumerate(cells):
        if not c:
            continue
        if "bar" not in cols and _has(c, BAR_KEYS):
            cols["bar"] = i
        elif "sku" not in cols and _has(c, SKU_KEYS) and not _has(c, PRICE_KEYS):
            cols["sku"] = i
        elif "name" not in cols and _has(c, NAME_KEYS) and not _has(c, PRICE_KEYS):
            cols["name"] = i
        elif _has(c, RRP_KEYS):
            cols.setdefault("rrp", i)
        elif _has(c, COST_KEYS):
            cols.setdefault("cost", i)
        elif _has(c, PRICE_KEYS):
            cols.setdefault("price", i)
    if "cost" not in cols and "price" in cols:
        cols["cost"] = cols["price"]
    if "name" in cols and "cost" in cols:
        cols["_cur"] = {k: _cur(cells[cols[k]]) for k in ("cost", "rrp") if k in cols}
        return cols
    return None


def parse(data: bytes, filename: str) -> dict:
    """→ {rows: [{name, sku, barcode, cost, rrp}], currency (з заголовків або None), header: {...}}."""
    tab = read_table(data, filename)
    rows, hdr, cur, rcur = [], None, None, None
    for r in tab:
        h = _header(r) if r and not any(isinstance(c, (int, float)) and c for c in r) else None
        if h:
            hdr = h
            cur = cur or h["_cur"].get("cost")
            rcur = rcur or h["_cur"].get("rrp")
            continue
        if not hdr or not r:
            continue
        g = lambda k: r[hdr[k]] if k in hdr and hdr[k] < len(r) else None    # noqa: E731
        name, cost = _s(g("name")), _num(g("cost"))
        if not name or not cost:
            continue
        sku = _s(g("sku"))
        if isinstance(g("sku"), float) and g("sku").is_integer():
            sku = str(int(g("sku")))
        bar = g("bar")
        bar = str(int(bar)) if isinstance(bar, float) and bar.is_integer() else _s(bar)
        rows.append({"name": name[:300], "sku": sku[:100], "barcode": bar[:64], "cost": round(cost, 4), "rrp": _num(g("rrp"))})
    if not hdr:
        raise ValueError("Не знайшов у файлі колонки «Назва» і «Ціна». Перевірте, що в прайсі є рядок заголовків.")
    if not rows:
        raise ValueError("У прайсі немає рядків з назвою і ціною")
    return {"rows": rows, "currency": cur, "rrp_currency": rcur}


def _k(v: str | None) -> str:
    return re.sub(r"[^0-9a-zа-яіїєґ]", "", (v or "").lower())


def _tok(v: str | None) -> set:
    return {w for w in norm(v or "").split() if len(w) > 1}


def match(db: Session, brand: str | None, rows: list[dict]) -> list[dict]:
    """Зіставити рядки прайсу з товарами бренду (активні): артикул → штрихкод → назва (схожість ≥ 75 %)."""
    q = select(Product).where(Product.active.is_not(False))
    if brand:
        q = q.where(Product.brand == brand)
    prods = db.scalars(q).all()
    by_sku: dict = {}
    by_bar: dict = {}
    for p in prods:
        if p.sku and _k(p.sku):
            by_sku.setdefault(_k(p.sku), p)
        if p.barcode and _k(p.barcode):
            by_bar.setdefault(_k(p.barcode), p)
    toks = [(p, _tok(p.name)) for p in prods]
    used: set = set()
    out = []
    for r in rows:
        p, how = None, None
        if r.get("sku") and _k(r["sku"]) in by_sku:
            p, how = by_sku[_k(r["sku"])], "артикул"
        elif r.get("barcode") and _k(r["barcode"]) in by_bar:
            p, how = by_bar[_k(r["barcode"])], "штрихкод"
        else:
            t = _tok(r["name"])
            best, sc = None, 0.0
            for pp, pt in toks:
                if not t or not pt or pp.id in used:
                    continue
                j = len(t & pt) / len(t | pt)
                if j > sc:
                    best, sc = pp, j
            if best and sc >= 0.75:
                p, how = best, f"назва {round(sc * 100)}%"
        if p:
            used.add(p.id)
        out.append({**r, "pid": p.id if p else None, "how": how})
    return out


def default_supplier(db: Session, brand: str | None) -> Supplier | None:
    """Основний постачальник бренду — найчастіший Product.supplier серед товарів бренду."""
    if not brand:
        return None
    c = Counter(s for (s,) in db.execute(select(Product.supplier).where(Product.brand == brand, Product.supplier.is_not(None))) if s)
    if not c:
        return None
    name = c.most_common(1)[0][0].strip().lower()
    return next((s for s in db.scalars(select(Supplier)) if s.name.strip().lower() == name), None)


def create(db: Session, data: bytes, filename: str, brand: str | None, supplier_id: int | None, currency: str | None,
           price_date: date | None, user_id: int | None) -> SupplierPrice:
    from . import ingest
    res = parse(data, filename)
    cur = currency if currency in ("USD", "EUR", "UAH") else (res["currency"] or "UAH")
    rows = match(db, brand or None, res["rows"])
    rc = res.get("rrp_currency")
    if rc and rc != cur:                                       # РРЦ в іншій валюті (напр. закупівля в EUR, РРЦ у грн) — у валюту закупівлі
        from .pricing import rates
        rt = rates(db)
        for r in rows:
            if r.get("rrp"):
                r["rrp"] = round(r["rrp"] * rt.get(rc, 1.0) / (rt.get(cur, 1.0) or 1.0), 2)
                r["rrp_src"] = f"{rc}"
    if not supplier_id:
        s = default_supplier(db, brand)
        supplier_id = s.id if s else None
    sp = SupplierPrice(supplier_id=supplier_id, brand=brand or None, file_path=ingest.store(data, filename or "prais.xlsx"),
                       file_name=(filename or "прайс")[:300], price_date=price_date or date.today(), currency=cur,
                       uploaded_at=datetime.now(), user_id=user_id, rows=rows, n_rows=len(rows),
                       n_matched=sum(1 for r in rows if r["pid"]))
    db.add(sp)
    db.commit()
    return sp


def preview(db: Session, sp: SupplierPrice) -> list[dict]:
    """Рядки прайсу з товаром CRM і тим, що зміниться."""
    ids = [r["pid"] for r in sp.rows or [] if r.get("pid")]
    prods = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)))} if ids else {}
    out = []
    for i, r in enumerate(sp.rows or []):
        p = prods.get(r.get("pid"))
        same = bool(p and p.cost_currency == sp.currency and p.cost_fx and abs(p.cost_fx - r["cost"]) < 0.005
                    and (not r.get("rrp") or (p.rrp_fx and abs(p.rrp_fx - r["rrp"]) < 0.005)))
        out.append({"i": i, **r, "p": p, "old_cur": p.cost_currency if p else None, "old": p.cost_fx if p else None,
                    "old_rrp": p.rrp_fx if p else None, "same": same})
    return out


def apply(db: Session, sp: SupplierPrice, idx: list[int], with_rrp: bool = True) -> int:
    """Підставити закупівлю у валюті (і РРЦ) вибраним товарам."""
    rows = sp.rows or []
    n = 0
    for i in idx:
        if not (0 <= i < len(rows)) or not rows[i].get("pid"):
            continue
        p = db.get(Product, rows[i]["pid"])
        if not p:
            continue
        p.cost_currency, p.cost_fx = sp.currency or "UAH", rows[i]["cost"]
        if with_rrp and rows[i].get("rrp"):
            p.rrp_fx = rows[i]["rrp"]
        n += 1
    sp.applied_at, sp.n_applied = datetime.now(), n
    db.commit()
    return n


def set_match(db: Session, sp: SupplierPrice, i: int, pid: int | None) -> None:
    rows = [dict(r) for r in sp.rows or []]
    if 0 <= i < len(rows):
        rows[i]["pid"], rows[i]["how"] = pid, ("вручну" if pid else None)
        sp.rows = rows
        sp.n_matched = sum(1 for r in rows if r.get("pid"))
        db.commit()


def for_supplier(db: Session, sup: Supplier) -> list[SupplierPrice]:
    return db.scalars(select(SupplierPrice).where(SupplierPrice.supplier_id == sup.id).order_by(SupplierPrice.uploaded_at.desc()).limit(20)).all()


def latest_by_brand(db: Session) -> dict[str, SupplierPrice]:
    out: dict = {}
    for sp in db.scalars(select(SupplierPrice).order_by(SupplierPrice.uploaded_at.desc()).limit(300)):
        if sp.brand and sp.brand not in out:
            out[sp.brand] = sp
    return out
