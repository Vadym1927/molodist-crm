"""Ліміти ФОП: оборот двох ФОП (готівка / безготівка) по місяцях, річний ліміт, запас, доступно на місяць.

Дані: Checkbox (автоматично, Z-звіти) або імпорт Google-таблиці «Ліміти ФОП» (аркуші «<ФОП> <рік>»:
рядки «Готівка», «Безготівка», колонки Січень…Грудень) — для історії й місяців без Checkbox."""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FopMonth, SyncState

MONTHS = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"]
DEFAULT_LIMITS = {"2025": 6672000.0, "2026": 7211598.0}     # 3 група ЄП: 1167 мінзарплат на 1 січня року


def fops(db: Session) -> list[str]:
    from ..integrations.checkbox import accounts
    names = [a["name"] for a in accounts(db)]
    for (n,) in db.execute(select(FopMonth.fop).distinct()):
        if n not in names:
            names.append(n)
    return names


def limits(db: Session) -> dict[str, float]:
    st = db.get(SyncState, "fop_limits")
    try:
        return {**DEFAULT_LIMITS, **(json.loads(st.value) if st and st.value else {})}
    except ValueError:
        return dict(DEFAULT_LIMITS)


def set_limit(db: Session, year: int, amount: float) -> None:
    cur = limits(db)
    cur[str(year)] = amount
    st = db.get(SyncState, "fop_limits") or SyncState(key="fop_limits")
    st.value, st.updated_at = json.dumps(cur), datetime.utcnow()
    db.merge(st)
    db.commit()


def _num(v) -> float:
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def is_fop_workbook(sheets: list) -> bool:
    return any(re.search(r"20\d\d", str(t)) and any(str(r[0] if r else "").strip().lower().startswith("готівка")
                                                     for r in rows) for t, rows in sheets)


def import_workbook(db: Session, sheets: list) -> dict:
    """Аркуші «Колган 2026», «Бондар 2025»… Місяці з даними Checkbox не перезаписуються."""
    rep = {"type": "Ліміти ФОП (таблиця)", "rows": 0, "updated": 0, "skipped": 0, "errors": []}
    for title, rows in sheets:
        m = re.match(r"\s*(.+?)\s+(20\d\d)\s*$", str(title))
        if not m:
            continue
        fop, year = m.group(1).strip(), int(m.group(2))
        cash_row = next((r for r in rows if str(r[0] if r else "").strip().lower().startswith("готівка")), None)
        card_row = next((r for r in rows if str(r[0] if r else "").strip().lower().startswith("безготівка")), None)
        if not cash_row and not card_row:
            continue
        lim_cell = rows[0][0] if rows and rows[0] else None
        lim = _num(lim_cell)
        if lim > 100000 and str(year) not in json.loads((db.get(SyncState, "fop_limits") or SyncState(value="{}")).value or "{}"):
            set_limit(db, year, lim)
        for mi in range(12):
            c = _num(cash_row[mi + 1]) if cash_row and len(cash_row) > mi + 1 else 0.0
            b = _num(card_row[mi + 1]) if card_row and len(card_row) > mi + 1 else 0.0
            if not (c or b):
                continue
            fm = db.scalar(select(FopMonth).where(FopMonth.fop == fop, FopMonth.year == year, FopMonth.month == mi + 1))
            if fm and fm.source == "checkbox":
                rep["skipped"] += 1
                continue
            if fm:
                rep["updated"] += 1
            else:
                fm = FopMonth(fop=fop, year=year, month=mi + 1)
                db.add(fm)
            fm.cash, fm.cashless, fm.source, fm.updated_at = c, b, "manual", datetime.utcnow()
            rep["rows"] += 1
        rep["errors"].append(f"{fop} {year}: імпортовано")
    db.commit()
    return rep


def build(db: Session, year: int) -> dict:
    lim = limits(db).get(str(year)) or 0.0
    today = date.today()
    months_left = 12 - today.month + 1 if year == today.year else (12 if year > today.year else 0)
    rows = db.scalars(select(FopMonth).where(FopMonth.year == year)).all()
    out = []
    for name in fops(db):
        mm = {r.month: r for r in rows if r.fop == name}
        cash = sum(r.cash for r in mm.values())
        cashless = sum(r.cashless for r in mm.values())
        total = cash + cashless
        left = max(lim - total, 0) if lim else None
        out.append({"name": name, "months": mm, "cash": cash, "cashless": cashless, "total": total,
                    "pct": round(100 * total / lim, 1) if lim else None, "left": left,
                    "per_month": round(left / months_left) if left is not None and months_left else None,
                    "sources": sorted({r.source for r in mm.values()})})
    total = sum(f["total"] for f in out)
    all_m = {}
    for f in out:
        for mi, r in f["months"].items():
            all_m[mi] = all_m.get(mi, 0) + r.total
    best = max(all_m.items(), key=lambda kv: kv[1], default=None)
    last_m = max(all_m) if all_m else 0
    return {"year": year, "limit": lim, "fops": out, "total": total, "total_limit": lim * len(out),
            "total_left": sum(f["left"] or 0 for f in out), "best": (MONTHS[best[0] - 1], best[1]) if best else None,
            "months_left": months_left, "last_month": last_m, "period": f"{MONTHS[0]}–{MONTHS[last_m - 1]} {year}" if last_m else "",
            "max": max([r.total for r in rows] + [1]), "MONTHS": MONTHS}
