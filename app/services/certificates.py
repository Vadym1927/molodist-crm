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


# ---------------- Абонементи ----------------
SUB_STATES = {"active": "Активний", "expiring": "Скоро закінчиться", "expired": "Згорів із залишком",
              "used": "Використано", "unpaid": "Не оплачений"}

# колонка CRM → можливі назви в Excel CleverBox (точний збіг, потім «починається з»)
SUB_COLS = {
    "id": ("id / код", "id", "№", "номер"),
    "code": ("code", "код"),
    "type": ("тип абонемента", "тип абонементу", "абонемент", "тип", "назва"),
    "services": ("послуги", "послуга", "services"),
    "client_id": ("client id", "id клієнта"),
    "client": ("клієнт", "client"),
    "seller": ("seller", "продавець", "продав"),
    "sold": ("дата продажу", "продано", "дата"),
    "price": ("ціна",),
    "paid": ("до сплати", "сплачено", "оплачено"),
    "total": ("кількість візитів", "к-сть візитів", "візитів", "кількість", "всього візитів", "к-сть"),
    "used": ("використано", "відвідано", "використано візитів", "списано"),
    "left": ("залишок візитів", "залишилось", "залишок"),
    "balance": ("баланс",),
    "valid": ("дійсний до", "термін дії", "діє до", "закінчення"),
    "comment": ("коментар",),
    "status": ("статус",),
}


def _sub_cols(header: list) -> dict[str, int]:
    h = [str(x or "").strip().lower() for x in header]
    out = {}
    for key, names in SUB_COLS.items():
        for nm in names:
            idx = next((i for i, x in enumerate(h) if x == nm and i not in out.values()), None)
            if idx is None:
                idx = next((i for i, x in enumerate(h) if x.startswith(nm) and i not in out.values()), None)
            if idx is not None:
                out[key] = idx
                break
    return out


def is_subscriptions_export(header: list) -> bool:
    h = " | ".join(str(x or "").strip().lower() for x in header)
    if is_certificates_export(header):
        return False
    return "абонемент" in h or ("візит" in h and ("залиш" in h or "використ" in h) and "клієнт" in h)


def import_subscriptions(db: Session, rows: list[list], header: list) -> dict:
    from ..models import Subscription
    col = _sub_cols(header)

    def g(row, key):
        i = col.get(key)
        return row[i] if i is not None and i < len(row) else None

    pats = {p.cleverbox_id: p for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    names = {}
    for p in pats.values():
        names.setdefault(" ".join(sorted((p.full_name or "").lower().split())), p)
    rep = {"type": "CleverBox: абонементи", "rows": 0, "updated": 0, "patients_created": 0, "skipped": 0,
           "columns": {k: str(header[i]) for k, i in col.items()}, "errors": []}
    if "id" not in col or ("total" not in col and "left" not in col and "balance" not in col):
        rep["error"] = "Не знайдено колонок ID і кількості візитів / залишку — надішліть файл розробнику"
        return rep
    for n_row, row in enumerate(rows, 2):
        sid = _s(g(row, "id"))
        if not sid or not sid.isdigit():
            rep["skipped"] += 1
            continue
        rep["rows"] += 1
        s = db.get(Subscription, int(sid))
        if s:
            rep["updated"] += 1
        else:
            s = Subscription(id=int(sid))
            db.add(s)
        s.code = _s(g(row, "code"))
        s.type_name = (_s(g(row, "type")) or "")[:300] or None
        s.services = _s(g(row, "services"))
        cl = _s(g(row, "client_id"))
        s.cb_client_id = int(cl) if cl and cl.isdigit() else None
        nm = _s(g(row, "client"))
        s.client_name = None if (nm or "").lower() in ("without client", "без клієнта") else nm
        p = pats.get(s.cb_client_id) or (names.get(" ".join(sorted(s.client_name.lower().split()))) if s.client_name else None)
        s.patient_id = p.id if p else None
        s.seller = _s(g(row, "seller"))
        s.sold_at = _dt(g(row, "sold"))
        s.price = _n(g(row, "price"))
        s.paid = _n(g(row, "paid")) if "paid" in col else s.price
        tot = _s(g(row, "total"))
        s.visits_total = _n(tot) if tot is not None else None
        used, left = _s(g(row, "used")), _s(g(row, "left"))
        if used is not None:
            s.visits_used = _n(used)
        elif left is not None and s.visits_total is not None:
            s.visits_used = max(s.visits_total - _n(left), 0)
        elif s.visits_total is not None:
            s.visits_used = 0
        if "balance" in col:
            s.balance = _n(g(row, "balance"))
        vt = _dt(g(row, "valid"))
        s.valid_to = vt.date() if vt else None
        s.comment = (_s(g(row, "comment")) or "")[:500] or None
        s.cb_status = _s(g(row, "status"))
        s.updated_at = datetime.utcnow()
    db.commit()
    return rep


def subs_for_patient(db: Session, patient_id: int):
    from ..models import Subscription
    return db.scalars(select(Subscription).where(Subscription.patient_id == patient_id)
                      .order_by(Subscription.sold_at.desc())).all()


def subs_expiring(db: Session, today: date | None = None):
    from ..models import Subscription
    today = today or date.today()
    return [s for s in db.scalars(select(Subscription).where(Subscription.valid_to >= today))
            if s.state(today) == "expiring"]
