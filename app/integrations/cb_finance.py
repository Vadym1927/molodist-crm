"""Рух коштів з API CleverBox → CashDay (cashflow).

Публічної документації фінансових методів CleverBox немає, тому:
1) probe() перебирає можливі методи ЧИТАННЯ (жодних save/change) за останні 7 днів і зберігає в SyncState
   лише назви методів, полів і кількість рядків — без сум і персональних даних;
2) метод, що повернув рядки з датою й сумою, запамʼятовується (SyncState cleverbox_finance_method);
3) sync_cash() тягне операції за період і складає по днях: надходження (готівка / безготівка),
   витрати; внутрішні перекази між касами виключаються. Дні записуються з source="api"."""
from __future__ import annotations

import json
import logging
from collections import defaultdict, Counter
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from ..models import CashDay
from .cleverbox import CleverBox, _state, d, parse_dt

log = logging.getLogger("cb_finance")

CANDIDATES = ("finance/transactions", "finance/operations", "finance/list", "finance", "finances",
              "cashbox/operations", "cashbox/transactions", "cashbox/list", "cashboxes/operations",
              "transactions", "operations", "money/operations", "payments", "sales/payments",
              "expenses", "finance/expenses", "costs", "reports/profit", "reports/finance", "reports/cashflow")
DATE_KEYS = ("date", "datetime", "date_time", "created", "created_at", "date_create", "operation_date", "time")
AMOUNT_KEYS = ("amount", "sum", "summa", "total", "value", "money", "payd_amount")
TYPE_KEYS = ("type", "_type", "operation", "operation_type", "kind", "direction", "type_name", "category",
             "category_name", "article", "article_name")
CASHBOX_KEYS = ("cashbox_name", "cashbox", "payment_name", "payment_type", "account", "account_name", "kassa",
                "pay_type", "method")
TRANSFER_WORDS = ("transfer", "переказ", "переміщ", "перемещ", "inner", "internal", "між кас", "инкасац", "інкасац")
EXPENSE_WORDS = ("expense", "витрат", "расход", "outcome", "out", "cost", "списан", "виплат", "зарплат", "оплата постач")
INCOME_WORDS = ("income", "надход", "приход", "доход", "in", "sale", "продаж", "оплата")
CASH_WORDS = ("готів", "налич", "cash", "каса")


def _first(row: dict, keys):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _num(v) -> float | None:
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _text(row: dict, keys) -> str:
    return " ".join(str(_flat(row.get(k))) for k in keys if row.get(k) not in (None, "")).lower()


def _flat(v):
    if isinstance(v, dict):
        return v.get("name") or v.get("title") or v.get("value") or ""
    return v


def _rows(cb: CleverBox, method: str, start: date, end: date, limit_pages: int | None = None):
    body = {"date": d(start), "date_end": d(end), "limit": 100}
    out = []
    for i, r in enumerate(cb.paged(method, body)):
        out.append(r)
        if limit_pages and i >= 100 * limit_pages:
            break
    return out


def probe(db: Session, cb: CleverBox | None = None) -> dict:
    """Шукає метод руху коштів. Результат (без сум) — у SyncState cleverbox_finance_probe."""
    cb = cb or CleverBox()
    end, start = date.today(), date.today() - timedelta(days=7)
    res, chosen = {}, None
    for m in CANDIDATES:
        try:
            rows = _rows(cb, m, start, end, limit_pages=1)
        except Exception as e:  # noqa: BLE001 — невідомий метод / вимкнений модуль
            res[m] = {"ok": False, "error": str(e)[:120]}
            continue
        keys = sorted({k for r in rows[:50] if isinstance(r, dict) for k in r.keys()})
        types = Counter(_text(r, TYPE_KEYS)[:40] for r in rows[:200] if isinstance(r, dict))
        res[m] = {"ok": True, "rows": len(rows), "keys": keys[:40], "types": dict(types.most_common(12))}
        has = rows and any(_first(r, DATE_KEYS) is not None and _num(_first(r, AMOUNT_KEYS)) is not None
                           for r in rows[:20] if isinstance(r, dict))
        if has and not chosen and m not in ("payments", "sales/payments"):
            chosen = m
    if not chosen:          # лише оплати чеків — краще, ніж нічого (без витрат)
        chosen = next((m for m in ("payments", "sales/payments") if res.get(m, {}).get("rows")), None)
    _state(db, "cleverbox_finance_probe", json.dumps({"at": datetime.now().isoformat(timespec="minutes"),
                                                       "chosen": chosen, "methods": res}, ensure_ascii=False)[:20000])
    _state(db, "cleverbox_finance_method", chosen or "")
    db.commit()
    return {"chosen": chosen, "ok": [m for m, v in res.items() if v.get("ok")]}


def classify(row: dict) -> tuple[str, float, bool] | None:
    """(in|out|transfer, сума ≥ 0, готівка?) або None, якщо рядок не схожий на операцію."""
    amt = _num(_first(row, AMOUNT_KEYS))
    if amt is None or amt == 0:
        return None
    t = _text(row, TYPE_KEYS)
    cashbox = _text(row, CASHBOX_KEYS)
    if any(w in t for w in TRANSFER_WORDS) or row.get("to_cashbox") or row.get("cashbox_to") or row.get("transfer"):
        return "transfer", abs(amt), False
    is_cash = any(w in cashbox for w in CASH_WORDS)
    if amt < 0:
        return "out", -amt, is_cash
    if any(w in t.split() or w in t for w in EXPENSE_WORDS if len(w) > 3) or t.strip() in ("out", "2", "-1", "expense"):
        return "out", amt, is_cash
    return "in", amt, is_cash


def sync_cash(db: Session, start: date, end: date, cb: CleverBox | None = None) -> dict:
    method = _state(db, "cleverbox_finance_method")
    if method is None:
        probe(db, cb)
        method = _state(db, "cleverbox_finance_method")
    if not method:
        return {"method": None, "days": 0}
    cb = cb or CleverBox()
    agg = defaultdict(lambda: {"cash": 0.0, "card": 0.0, "out": 0.0, "transfers": 0})
    skipped = 0
    # по місяцях — щоб не впертися в ліміти відповіді
    cur = start
    while cur <= end:
        nxt = min(end, (cur.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1))
        for r in _rows(cb, method, cur, nxt):
            if not isinstance(r, dict):
                continue
            dt = parse_dt(str(_first(r, DATE_KEYS) or ""))
            c = classify(r)
            if not dt or not c:
                skipped += 1
                continue
            kind, amt, is_cash = c
            a = agg[dt.date()]
            if kind == "transfer":
                a["transfers"] += 1
            elif kind == "out":
                a["out"] += amt
            else:
                a["cash" if is_cash else "card"] += amt
        cur = nxt + timedelta(days=1)
    only_income = method in ("payments", "sales/payments")
    n = 0
    day = start
    while day <= end:
        cd = db.get(CashDay, day)
        if cd is not None and cd.source == "cleverbox":
            day += timedelta(days=1)                 # повні дані зі звіту «Прибуток» (Excel) не чіпаємо
            continue
        a = agg.get(day) or {"cash": 0.0, "card": 0.0, "out": 0.0}
        cd = cd or CashDay(day=day)
        cd.cash_in, cd.card_in = round(a["cash"], 2), round(a["card"], 2)
        cd.income = round(a["cash"] + a["card"], 2)
        cd.expense = 0.0 if only_income else round(a["out"], 2)
        cd.source, cd.updated_at = ("checks" if only_income else "api"), datetime.utcnow()
        db.merge(cd)
        n += 1
        day += timedelta(days=1)
    db.commit()
    _state(db, "cleverbox_finance_last", datetime.now().isoformat(timespec="minutes"))
    db.commit()
    return {"method": method, "days": n, "skipped": skipped,
            "transfers": sum(v["transfers"] for v in agg.values())}
