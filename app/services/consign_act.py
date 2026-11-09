"""Акт звірки з постачальником під реалізацію (TEBISKIN, AG SKIN): поштучно й до копійки.

1) Акт CRM на дату: товар на реалізації по кожній позиції (к-сть × ціна банки, з яких поставок) + гроші по кожній поставці
   (сума, оплачено CleverBox, оплачено в CRM, знижки / повернення, борг). Excel для відправки постачальнику.
2) Акт постачальника (Excel / CSV / XLS) → автоматичне порівняння:
   • товари — за артикулом або назвою: к-сть і сума CRM ↔ постачальника, різниця;
   • документи — поставки за сумою (і датою ±5 днів): є в CRM / немає, сума збігається чи ні;
   • підсумок — борг CRM ↔ сальдо постачальника.
Заголовки колонок шукаються в перших 30 рядках (товар / найменування / назва, артикул / код, к-сть / кількість,
ціна, сума, дата, документ / №, дебет / кредит / сальдо)."""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from . import consign as cs

H_NAME = ("найменування", "назва товару", "товар", "номенклатура", "назва", "продукція")
H_SKU = ("артикул", "код товару", "код", "sku", "арт")
H_QTY = ("кількість", "к-сть", "к-ть", "кол-во", "залишок, шт", "шт")
H_PRICE = ("ціна",)
H_SUM = ("сума", "вартість")
H_DATE = ("дата",)
H_DOC = ("документ", "№", "номер", "назва документа", "зміст операції")
H_DEB = ("дебет",)
H_CRED = ("кредит",)


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\xa0", "").replace(" ", "").replace("₴", "").replace("грн", "").replace(",", ".").strip()
    if not s or not re.match(r"^-?\d+(\.\d+)?$", s):
        return None
    return float(s)


def _date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", str(v or ""))
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y += 2000 if y < 100 else 0
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def read_rows(path: str, raw: bytes | None = None) -> list[list]:
    low = path.lower()
    if low.endswith(".csv") or low.endswith(".txt"):
        txt = (raw or open(path, "rb").read()).decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(txt[:4000], delimiters=",;\t") if txt.strip() else csv.excel
        return [r for r in csv.reader(io.StringIO(txt), dialect)]
    if low.endswith(".xls"):
        import xlrd
        sh = xlrd.open_workbook(path, ignore_workbook_corruption=True).sheet_by_index(0)
        out = []
        for i in range(sh.nrows):
            row = []
            for j, c in enumerate(sh.row(i)):
                if c.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate_as_datetime(c.value, sh.book.datemode))
                else:
                    row.append(c.value)
            out.append(row)
        return out
    from openpyxl import load_workbook
    ws = load_workbook(path, read_only=True, data_only=True).worksheets[0]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _find(head: list[str], keys: tuple) -> int | None:
    for k in keys:
        for i, h in enumerate(head):
            if h == k:
                return i
    for k in keys:
        for i, h in enumerate(head):
            if k in h:
                return i
    return None


def parse(rows: list[list]) -> dict:
    """Акт постачальника → товари [{name, sku, qty, price, sum}] і документи [{date, doc, debit, credit}]."""
    best, hi = None, -1
    for i, r in enumerate(rows[:30]):
        head = [str(c or "").strip().lower() for c in r]
        score = sum(1 for keys in (H_NAME, H_QTY, H_SUM, H_DATE, H_DEB, H_CRED, H_SKU) if _find(head, keys) is not None)
        if score > hi:
            best, hi = i, score
    if best is None or hi < 2:
        return {"goods": [], "docs": [], "head": None, "error": "Не знайшов рядок заголовків (товар / к-сть / сума або дата / дебет / кредит)"}
    head = [str(c or "").strip().lower() for c in rows[best]]
    ci = {k: _find(head, keys) for k, keys in (("name", H_NAME), ("sku", H_SKU), ("qty", H_QTY), ("price", H_PRICE), ("sum", H_SUM),
                                                ("date", H_DATE), ("doc", H_DOC), ("deb", H_DEB), ("cred", H_CRED))}
    if ci["sku"] == ci["name"]:
        ci["sku"] = None
    goods, docs = [], []
    for r in rows[best + 1:]:
        g = lambda k: r[ci[k]] if ci[k] is not None and ci[k] < len(r) else None  # noqa: E731
        name = str(g("name") or "").strip()
        qty, sm, price = _num(g("qty")), _num(g("sum")), _num(g("price"))
        low = name.lower()
        if name and qty is not None and not low.startswith(("разом", "всього", "итого", "усього")):
            if sm is None and price is not None:
                sm = round(price * qty, 2)
            goods.append({"name": name, "sku": str(g("sku") or "").strip() or None, "qty": qty, "price": price, "sum": sm})
            continue
        d, deb, cred = _date(g("date")), _num(g("deb")), _num(g("cred"))
        if d and (deb or cred):
            docs.append({"date": d, "doc": str(g("doc") or "").strip(), "debit": deb or 0.0, "credit": cred or 0.0})
    return {"goods": goods, "docs": docs, "head": head, "cols": ci}


def crm_act(db: Session, supplier: str, on: date | None = None) -> dict:
    """Акт CRM: товар на реалізації по позиціях і гроші по поставках."""
    on = on or date.today()
    goods: dict = {}
    for l in cs.lots(db, supplier):
        left = round(l.qty - l.sold, 3)
        if left <= 1e-9 or l.inv_date > on:
            continue
        k = l.product_id or ("n:" + cs._norm(l.name))
        g = goods.setdefault(k, {"pid": l.product_id, "name": l.name, "sku": l.sku, "qty": 0.0, "sum": 0.0, "by": []})
        g["qty"] += left
        g["sum"] += left * l.cost
        g["by"].append({"invoice": l.invoice, "date": l.inv_date, "qty": left, "cost": l.cost})
    rows = sorted(goods.values(), key=lambda g: g["name"])
    for g in rows:
        g["qty"], g["sum"] = round(g["qty"], 3), round(g["sum"], 2)
    invs = sorted([x for x in cs._invoices(db, supplier).values() if x.inv_date.date() <= on and (x.status or "") != "Чернетка"],
                  key=lambda x: x.inv_date)
    money = [{"date": x.inv_date.date(), "no": x.cb_number, "amount": round(x.amount or 0, 2), "paid": round(x.paid or 0, 2),
              "crm_paid": round(x.crm_paid or 0, 2), "adjust": round(x.adjust or 0, 2), "adjust_note": x.adjust_note, "debt": x.debt} for x in invs]
    return {"supplier": supplier, "on": on, "goods": rows, "qty": round(sum(g["qty"] for g in rows), 3), "sum": round(sum(g["sum"] for g in rows), 2),
            "money": money, "debt": round(sum(m["debt"] for m in money), 2), "supplied": round(sum(m["amount"] for m in money), 2),
            "paid": round(sum(m["paid"] + m["crm_paid"] for m in money), 2), "adjust": round(sum(m["adjust"] for m in money), 2)}


def _key_match(crm: list[dict], name: str, sku: str | None) -> dict | None:
    if sku:
        s = sku.strip().upper()
        hit = next((g for g in crm if (g.get("sku") or "").upper() == s), None)
        if hit:
            return hit
    n = cs._norm(name)
    hit = next((g for g in crm if cs._norm(g["name"]) == n), None)
    if hit:
        return hit
    toks = set(n.split())
    best, sc = None, 0.0
    for g in crm:
        gt = set(cs._norm(g["name"]).split())
        if toks and gt:
            s = len(toks & gt) / len(toks | gt)
            if s > sc:
                best, sc = g, s
    return best if sc >= 0.55 else None


def compare(db: Session, supplier: str, parsed: dict, on: date | None = None) -> dict:
    act = crm_act(db, supplier, on)
    crm = [dict(g, s_qty=0.0, s_sum=0.0, s_names=[]) for g in act["goods"]]
    extra = []
    for r in parsed.get("goods", []):
        g = _key_match(crm, r["name"], r.get("sku"))
        if g:
            g["s_qty"] += r["qty"] or 0
            g["s_sum"] += r["sum"] or 0
            g["s_names"].append(r["name"])
        else:
            extra.append(r)
    goods = []
    for g in crm:
        dq, ds = round(g["s_qty"] - g["qty"], 3), round(g["s_sum"] - g["sum"], 2)
        goods.append({**g, "s_qty": round(g["s_qty"], 3), "s_sum": round(g["s_sum"], 2), "dq": dq, "ds": ds, "ok": abs(dq) < 1e-6 and abs(ds) < 0.01})
    for r in extra:
        goods.append({"name": r["name"], "sku": r.get("sku"), "qty": 0.0, "sum": 0.0, "s_qty": r["qty"] or 0, "s_sum": r["sum"] or 0,
                      "dq": r["qty"] or 0, "ds": r["sum"] or 0, "ok": False, "missing_crm": True, "by": []})
    # документи: поставки постачальника (дебет або кредит — залежно від сторони акту) ↔ поставки CRM за сумою
    docs, used = [], set()
    for d in parsed.get("docs", []):
        amt = d["debit"] or d["credit"]
        hit = None
        for m in act["money"]:
            if m["no"] in used:
                continue
            if abs(m["amount"] - amt) < 0.01 and abs((m["date"] - d["date"]).days) <= 5:
                hit = m
                break
        if hit:
            used.add(hit["no"])
        docs.append({**d, "amount": amt, "crm": hit, "ok": bool(hit)})
    not_in_act = [m for m in act["money"] if m["no"] not in used] if parsed.get("docs") else []
    s_debt = None
    if parsed.get("docs"):
        s_debt = round(sum(d["debit"] for d in parsed["docs"]) - sum(d["credit"] for d in parsed["docs"]), 2)
    return {"act": act, "goods": goods, "goods_ok": all(g["ok"] for g in goods) if goods else None,
            "s_qty": round(sum(g["s_qty"] for g in goods), 3), "s_sum": round(sum(g["s_sum"] for g in goods), 2),
            "docs": docs, "not_in_act": not_in_act, "s_debt": s_debt, "n_goods": len(parsed.get("goods", [])), "n_docs": len(parsed.get("docs", []))}


def to_xlsx(act: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Товар на реалізації"
    ws.append([f"Акт звірки · {act['supplier']} · товар на реалізації станом на {act['on']:%d.%m.%Y}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append(["Артикул", "Товар", "К-сть", "Сума", "Поставки (№ · дата · к-сть × ціна)"])
    for c in ws[3]:
        c.font = Font(bold=True)
    for g in act["goods"]:
        ws.append([g.get("sku") or "", g["name"], g["qty"], g["sum"],
                   "; ".join(f"№{b['invoice']} · {b['date']:%d.%m.%Y} · {b['qty']:g}×{b['cost']:g}" for b in g["by"])])
    ws.append(["", "Разом", act["qty"], act["sum"]])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    ws.column_dimensions["B"].width = 70
    ws.column_dimensions["E"].width = 60
    w2 = wb.create_sheet("Поставки й оплати")
    w2.append(["Дата", "№ поставки", "Сума поставки", "Оплачено (CleverBox)", "Оплачено (CRM)", "Знижки / повернення", "Примітка", "Борг"])
    for c in w2[1]:
        c.font = Font(bold=True)
    for m in act["money"]:
        w2.append([m["date"].strftime("%d.%m.%Y"), m["no"], m["amount"], m["paid"], m["crm_paid"] or None, m["adjust"] or None, m["adjust_note"] or "", m["debt"]])
    w2.append(["Разом", "", act["supplied"], None, None, act["adjust"], "", act["debt"]])
    for c in w2[w2.max_row]:
        c.font = Font(bold=True)
    w2.column_dimensions["G"].width = 50
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
