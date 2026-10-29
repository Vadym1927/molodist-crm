"""Постачальники: юридичні особи (кілька ТОВ / ФОП на одного постачальника), картка-вікно, пошук за реквізитами."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, StockDoc, Supplier, SupplierEntity, SupplierInvoice

KINDS = ["ТОВ", "ФОП", "ПП", "АТ", "Інше"]
FIELDS = ("kind", "legal_name", "edrpou", "iban", "bank", "mfo", "pay_purpose", "address", "director", "contract",
          "tax_group", "email", "note")
MIRROR = ("legal_name", "edrpou", "iban", "bank", "pay_purpose", "address", "contract", "vat")


def clean_iban(v: str | None) -> str:
    return re.sub(r"\s", "", v or "").upper()


def clean_code(v: str | None) -> str:
    return re.sub(r"\D", "", v or "")


def guess_kind(name: str | None, edrpou: str | None) -> str | None:
    n = (name or "").upper()
    for k in ("ТОВ", "ФОП", "ПП", "АТ"):
        if re.search(rf"(^|[^А-ЯІЇЄҐ]){k}([^А-ЯІЇЄҐ]|$)", n):
            return k
    code = clean_code(edrpou)
    return "ФОП" if len(code) == 10 else ("ТОВ" if len(code) == 8 else None)


def entities(db: Session, sup: Supplier) -> list[SupplierEntity]:
    """Юр. особи постачальника; старі реквізити з картки постачальника переносяться в першу (основну)."""
    rows = db.scalars(select(SupplierEntity).where(SupplierEntity.supplier_id == sup.id)
                      .order_by(SupplierEntity.is_main.desc(), SupplierEntity.id)).all()
    if not rows and (sup.legal_name or sup.edrpou or sup.iban):
        e = SupplierEntity(supplier_id=sup.id, is_main=True, kind=guess_kind(sup.legal_name, sup.edrpou),
                           **{k: getattr(sup, k) for k in MIRROR})
        db.add(e)
        db.commit()
        rows = [e]
    return list(rows)


def _mirror(db: Session, sup: Supplier) -> None:
    """Основна юр. особа → поля постачальника (ними користуються старі звіти й копіювання реквізитів)."""
    rows = db.scalars(select(SupplierEntity).where(SupplierEntity.supplier_id == sup.id)
                      .order_by(SupplierEntity.is_main.desc(), SupplierEntity.id)).all()
    main = rows[0] if rows else None
    if main and not main.is_main:
        main.is_main = True
    for k in MIRROR:
        setattr(sup, k, getattr(main, k) if main else None)


def save_entity(db: Session, sup: Supplier, data: dict, eid: int | None = None) -> SupplierEntity:
    e = db.get(SupplierEntity, eid) if eid else None
    if e and e.supplier_id != sup.id:
        raise ValueError("Юр. особа належить іншому постачальнику")
    if not e:
        e = SupplierEntity(supplier_id=sup.id)
        db.add(e)
    for k in FIELDS:
        v = (data.get(k) or "").strip()
        if k == "iban":
            v = clean_iban(v)
            if v and not re.fullmatch(r"UA\d{27}", v):
                raise ValueError("IBAN має формат UA і 27 цифр")
        elif k == "edrpou":
            v = clean_code(v)
            if v and len(v) not in (8, 10):
                raise ValueError("ЄДРПОУ — 8 цифр, РНОКПП (ФОП) — 10 цифр")
        setattr(e, k, v[:300] or None)
    e.kind = e.kind or guess_kind(e.legal_name, e.edrpou)
    e.vat = bool(data.get("vat")) or None
    db.flush()
    others = db.scalars(select(SupplierEntity).where(SupplierEntity.supplier_id == sup.id, SupplierEntity.id != e.id)).all()
    if data.get("is_main") or not others:
        for o in others:
            o.is_main = None
        e.is_main = True
    _mirror(db, sup)
    db.commit()
    return e


def delete_entity(db: Session, sup: Supplier, eid: int) -> None:
    e = db.get(SupplierEntity, eid)
    if e and e.supplier_id == sup.id:
        db.delete(e)
        db.flush()
        _mirror(db, sup)
        db.commit()


def find(db: Session, iban: str | None = None, edrpou: str | None = None) -> tuple[Supplier, SupplierEntity | None] | None:
    """Постачальник за IBAN / ЄДРПОУ будь-якої його юр. особи (потім — старі поля картки)."""
    ib, ed = clean_iban(iban), clean_code(edrpou)
    if not ib and not ed:
        return None
    ents = db.scalars(select(SupplierEntity)).all()
    for test in ((lambda e: ib and clean_iban(e.iban) == ib), (lambda e: ed and clean_code(e.edrpou) == ed)):
        e = next((x for x in ents if test(x)), None)
        if e:
            s = db.get(Supplier, e.supplier_id)
            if s:
                return s, e
    sups = db.scalars(select(Supplier)).all()
    s = next((x for x in sups if ib and clean_iban(x.iban) == ib), None) or \
        next((x for x in sups if ed and clean_code(x.edrpou) == ed), None)
    return (s, None) if s else None


def requisites_text(sup: Supplier, e: SupplierEntity) -> str:
    purpose = e.pay_purpose or ("Оплата за товар згідно рахунку № …" + (", у т.ч. ПДВ" if e.vat else ", без ПДВ"))
    lines = [f"Отримувач: {e.legal_name or sup.name}", f"{'РНОКПП' if e.kind == 'ФОП' else 'ЄДРПОУ'}: {e.edrpou or '—'}",
             f"IBAN: {e.iban or '—'}", f"Банк: {e.bank or '—'}" + (f", МФО {e.mfo}" if e.mfo else ""),
             f"Призначення: {purpose}"]
    return "\n".join(lines)


def card(db: Session, sup: Supplier) -> dict:
    """Усе для вікна постачальника: юр. особи, цифри, останні поставки, товари."""
    from . import payments as pay
    from .stock import supplier_stats
    key = sup.name.lower()
    ss = supplier_stats(db).get(key) or {}
    groups = pay.groups(db, sup.name)
    crm_total = sum(g.total for g in groups)
    crm_left = sum(g.left for g in groups if g.left > 1)
    invoices = [x for x in db.scalars(select(SupplierInvoice).order_by(SupplierInvoice.inv_date.desc()))
                if (x.supplier or "").lower() == key][:15]
    products = sorted([p for p in db.scalars(select(Product).where(Product.supplier.is_not(None)))
                       if p.supplier.lower() == key], key=lambda p: p.name.lower())
    orders = [d for d in db.scalars(select(StockDoc).where(StockDoc.kind == "order", StockDoc.status.in_(("draft", "sent"))))
              if (d.supplier or "").lower() == key]
    _d = lambda v: v.date() if hasattr(v, "date") and callable(v.date) else v      # noqa: E731
    last = max(filter(None, [_d(ss.get("last")), *(_d(g.date) for g in groups)]), default=None)
    return {"sup": sup, "entities": entities(db, sup), "stats": ss, "groups": groups[:12], "crm_total": crm_total,
            "crm_left": crm_left, "invoices": invoices, "products": products, "orders": orders, "last": last,
            "debt": (ss.get("debt") or 0) + crm_left, "KINDS": KINDS, "req": requisites_text}
