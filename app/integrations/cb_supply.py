"""Склад поставки CleverBox (позиції документа) — для «провалитися в поставку» з картки постачальника.

Публічного методу «позиції поставки» в документації немає, тому, як і для руху коштів:
1) probe() перебирає можливі методи ЧИТАННЯ (жодних save/change) за останні 60 днів і запамʼятовує той,
   що повертає документи з позиціями товару (SyncState cleverbox_supply_method) — лише назви методів і полів;
2) items_for(invoice) бере документ за № і датою (±2 дні) і зберігає позиції в SupplierInvoice.items.
Якщо API позицій не віддає — поставку можна відкрити й завантажити позиції з Excel CleverBox."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from .cleverbox import CleverBox, _state, d

log = logging.getLogger("cb_supply")

CANDIDATES = ("products/supplies", "products/supply", "products/incoming", "products/income", "products/arrivals", "products/arrival",
              "products/receipts", "products/documents", "products/docs", "products/movements", "products/moves", "products/history",
              "storage/supplies", "storage/supply", "storage/documents", "storage/incoming", "warehouse/supplies", "warehouse/documents",
              "stock/supplies", "stock/documents", "supplies", "supply", "incoming", "arrivals", "documents", "products/invoices")
ITEM_KEYS = ("items", "products", "goods", "positions", "rows", "lines", "details", "product_list")
NUM_KEYS = ("number", "num", "n", "doc_number", "document_number", "supply_id", "id", "document_id", "doc_id")
NAME_KEYS = ("product_name", "item_name", "name", "title", "good_name")
QTY_KEYS = ("quantity", "qty", "count", "amount_qty", "kol", "kolvo")
PRICE_KEYS = ("price", "cost", "price_in", "purchase_price", "cost_price", "buy_price")
SUM_KEYS = ("total", "sum", "summa", "amount", "total_sum")


def _first(row: dict, keys):
    for k in keys:
        if isinstance(row, dict) and k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _num(v) -> float | None:
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _items_of(row: dict) -> list | None:
    for k in ITEM_KEYS:
        v = row.get(k) if isinstance(row, dict) else None
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return None


def _rows(cb: CleverBox, method: str, start: date, end: date, max_rows: int = 500) -> list:
    out = []
    for i, r in enumerate(cb.paged(method, {"date": d(start), "date_end": d(end), "limit": 100})):
        out.append(r)
        if i >= max_rows:
            break
    return out


def probe(db: Session, cb: CleverBox | None = None, log_=lambda m: None) -> dict:
    cb = cb or CleverBox()
    end, start = date.today(), date.today() - timedelta(days=60)
    res, chosen, flat = {}, None, None
    for m in CANDIDATES:
        try:
            rows = _rows(cb, m, start, end, 200)
        except Exception as e:  # noqa: BLE001 — невідомий метод
            res[m] = {"ok": False, "error": str(e)[:100]}
            continue
        keys = sorted({k for r in rows[:30] if isinstance(r, dict) for k in r.keys()})
        nested = any(_items_of(r) for r in rows[:30])
        lines = any(_first(r, NAME_KEYS) and _num(_first(r, QTY_KEYS)) is not None for r in rows[:30] if isinstance(r, dict))
        res[m] = {"ok": True, "rows": len(rows), "keys": keys[:40], "nested": nested, "lines": lines}
        log_(f"{m}: {len(rows)} рядків" + (" · з позиціями" if nested else (" · рядки товару" if lines else "")))
        if nested and not chosen:
            chosen = m
        if lines and not flat:
            flat = m
    method = chosen or flat or ""
    _state(db, "cleverbox_supply_probe", json.dumps({"at": datetime.now().isoformat(timespec="minutes"), "chosen": method,
                                                     "methods": res}, ensure_ascii=False)[:20000])
    _state(db, "cleverbox_supply_method", method)
    db.commit()
    return {"chosen": method, "ok": [m for m, v in res.items() if v.get("ok")]}


def method(db: Session) -> str | None:
    return _state(db, "cleverbox_supply_method") or None


def _norm_items(items: list) -> list[dict]:
    out = []
    for it in items:
        q = _num(_first(it, QTY_KEYS))
        p = _num(_first(it, PRICE_KEYS))
        s = _num(_first(it, SUM_KEYS))
        if s is None and p is not None and q is not None:
            s = round(p * q, 2)
        if p is None and s is not None and q:
            p = round(s / q, 2)
        out.append({"name": str(_first(it, NAME_KEYS) or "—")[:300], "qty": q, "price": p, "sum": s,
                    "cb_product_id": it.get("product_id") or it.get("item_id") or it.get("good_id")})
    return out


def items_for(db: Session, inv, cb: CleverBox | None = None) -> list[dict] | None:
    """Позиції поставки з API (None — метод не знайдено або документ не знайдено)."""
    m = method(db)
    if not m:
        return None
    cb = cb or CleverBox()
    day = inv.inv_date.date()
    rows = _rows(cb, m, day - timedelta(days=2), day + timedelta(days=2), 2000)
    num = str(inv.cb_number).strip()
    docs = [r for r in rows if isinstance(r, dict) and any(str(r.get(k) or "").strip() == num for k in NUM_KEYS)]
    for r in docs:
        it = _items_of(r)
        if it:
            return _norm_items(it)
    if docs and _first(docs[0], NAME_KEYS):            # плоскі рядки товару з номером документа
        return _norm_items(docs)
    return None
