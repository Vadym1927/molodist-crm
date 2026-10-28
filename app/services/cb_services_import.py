"""Імпорт вивантаження CleverBox «Послуги» (Excel, Services_*.xls): назва в чеку / для SMS, напрямок, техкартка,
тривалість. Зіставлення з довідником послуг CRM (CbService, з API CleverBox) — за назвою або назвою в чеку.
ID у файлі немає, тому нових послуг не створюємо: їх приносить синхронізація з CleverBox."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CbService
from ..parsers.observ import norm


def is_services_export(header: list) -> bool:
    h = {str(x or "").strip().lower() for x in header}
    return "назва" in h and "техкартка" in h and ("назва у чеку" in h or "направленість" in h)


def _s(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    t = " ".join(str(v).split())
    return t or None


def import_services(db: Session, rows: list[list], header: list) -> dict:
    col = {str(h or "").strip().lower(): i for i, h in enumerate(header)}

    def g(row, name):
        i = col.get(name)
        return _s(row[i]) if i is not None and i < len(row) else None

    svcs = db.scalars(select(CbService)).all()
    by_name: dict[str, list[CbService]] = {}
    for s in svcs:
        for k in {norm(s.name or ""), norm(s.check_name or "")} - {""}:
            by_name.setdefault(k, []).append(s)
    rep = {"type": "CleverBox: послуги", "rows": 0, "updated": 0, "skipped": 0, "errors": []}
    for row in rows:
        name = g(row, "назва")
        if not name:
            continue
        rep["rows"] += 1
        chk = g(row, "назва у чеку")
        hits = by_name.get(norm(name)) or (by_name.get(norm(chk)) if chk else None) or []
        if not hits:
            rep["skipped"] += 1
            if len(rep["errors"]) < 15:
                rep["errors"].append(f"немає в CleverBox-довіднику CRM: {name[:70]}")
            continue
        for s in hits:
            s.check_name = chk or s.check_name
            s.sms_name = g(row, "назва для sms") or s.sms_name
            s.direction = g(row, "направленість") or s.direction
            s.techcard = g(row, "техкартка") or s.techcard
            if not s.category and g(row, "категорія"):
                s.category = g(row, "категорія")[:200]
            t = g(row, "тривалість")
            if t and t.isdigit() and not s.time_min:
                s.time_min = int(t)
        rep["updated"] += 1
    db.commit()
    return rep


def service_index(db: Session) -> dict[str, CbService]:
    """Назва / назва в чеку → послуга (для звітів по напрямках)."""
    out = {}
    for s in db.scalars(select(CbService)):
        for k in (s.name, s.check_name):
            if k:
                out.setdefault(norm(k), s)
    return out
