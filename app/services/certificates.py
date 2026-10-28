"""Подарункові сертифікати: імпорт вивантаження CleverBox «Сертифікати», стан, нагадування до згорання."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GiftCertificate, Patient

STATES = {"active": "Активний", "expiring": "Скоро згорить", "expired": "Згорів із залишком", "used": "Використано",
          "unpaid": "Не оплачений"}


def is_certificates_export(header: list) -> bool:
    h = {str(x or "").strip().lower() for x in header}
    return "тип gift card" in h and "баланс" in h and "номінал" in h


def _s(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    t = " ".join(str(v).split())
    return None if t in ("", "-", "—") else t


def _n(v) -> float:
    try:
        return float(str(_s(v) or 0).replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def _dt(v) -> datetime | None:
    t = _s(v)
    if not t:
        return None
    for fmt in ("%d.%m.%y %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def import_certificates(db: Session, rows: list[list], header: list) -> dict:
    col = {str(h or "").strip().lower(): i for i, h in enumerate(header)}

    def g(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else None

    pats = {p.cleverbox_id: p for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    rep = {"type": "CleverBox: сертифікати", "rows": 0, "updated": 0, "patients_created": 0, "skipped": 0, "errors": []}
    for row in rows:
        cid = _s(g(row, "id / код"))
        if not cid or not cid.isdigit():
            rep["skipped"] += 1
            continue
        rep["rows"] += 1
        c = db.get(GiftCertificate, int(cid))
        if c:
            rep["updated"] += 1
        else:
            c = GiftCertificate(id=int(cid))
            db.add(c)
        c.code = _s(g(row, "code"))
        c.type_name = _s(g(row, "тип gift card"))
        cl = _s(g(row, "client id"))
        c.cb_client_id = int(cl) if cl and cl.isdigit() else None
        name = _s(g(row, "клієнт"))
        c.client_name = None if (name or "").lower() == "without client" else name
        p = pats.get(c.cb_client_id)
        c.patient_id = p.id if p else None
        c.seller = _s(g(row, "seller"))
        c.sold_at = _dt(g(row, "дата продажу"))
        c.price = _n(g(row, "ціна"))
        c.paid = _n(g(row, "до сплати"))
        c.nominal = _n(g(row, "номінал"))
        c.balance = _n(g(row, "баланс"))
        vt = _dt(g(row, "дійсний до"))
        c.valid_to = vt.date() if vt else None
        c.comment = (_s(g(row, "коментар")) or "")[:500] or None
        c.cb_status = _s(g(row, "статус"))
        c.updated_at = datetime.utcnow()
        if c.sold_at and c.valid_to and c.valid_to < c.sold_at.date() and len(rep["errors"]) < 10:
            rep["errors"].append(f"№{c.code}: «дійсний до» {c.valid_to:%d.%m.%Y} раніше дати продажу {c.sold_at:%d.%m.%Y} — перевірте в CleverBox")
    db.commit()
    return rep


def for_patient(db: Session, patient_id: int) -> list[GiftCertificate]:
    return db.scalars(select(GiftCertificate).where(GiftCertificate.patient_id == patient_id)
                      .order_by(GiftCertificate.sold_at.desc())).all()


def expiring(db: Session, today: date | None = None, days: int = 14) -> list[GiftCertificate]:
    today = today or date.today()
    out = []
    for c in db.scalars(select(GiftCertificate).where(GiftCertificate.balance > 0, GiftCertificate.valid_to >= today)):
        if c.state(today) == "expiring":
            out.append(c)
    return sorted(out, key=lambda c: c.valid_to)
