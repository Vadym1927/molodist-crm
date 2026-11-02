"""Історія візитів з CleverBox (з 2022 р.) → повноцінні візити CRM (Appointment).

Дані збираються у веб-кабінеті CleverBox (лише читання): історія візитів кожного клієнта (/clients/visits/{id}) і
фактичні витратні кожної послуги (/tcards/finds). Формат одного візиту:
  {"rid": № запису, "cid": id клієнта CB, "start": "дд.мм.рррр гг:хх", "stt": назва статусу, "st": тип статусу,
   "author": хто записав, "added": unix-час створення, "total", "disc", "paid", "check": id чека CB,
   "items": [{"row", "sid", "t": назва, "p": ціна, "s": сума, "d": знижка, "q": к-сть, "m": id майстра CB, "mn": ПІБ, "min": хвилин,
              "mats": [[id товару CB, к-сть, ціна од., назва]]}]}
Візит з кількома спеціалістами ділиться на записи CRM по спеціалістах (той самий № CleverBox).
Повторний імпорт оновлює ті самі записи (без дублів). Склад не чіпається: витратні вже списані в CleverBox."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, CbService, Doctor, Patient, Product, VisitMaterial

WH_PREF = ("inject", "classic", "single", "equip", "vitrina")


def _status(title: str, typ) -> str:
    t = (title or "").lower()
    if "скас" in t or "відмін" in t or "отмен" in t or "помилк" in t or "запізн" in t:
        return "cancelled"
    if "не прийш" in t or "неявк" in t or "не з'яв" in t or "не зʼяв" in t:
        return "no_show"
    if str(typ) == "3" or "заверш" in t or "викон" in t or "оплач" in t:
        return "done"
    return "booked"


def _dt(s: str) -> datetime | None:
    for f in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(s).strip()[:16], f)
        except ValueError:
            continue
    return None


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def import_visits(db: Session, visits: list[dict], until: datetime | None = None) -> dict:
    rep = {"visits": len(visits), "appts_new": 0, "appts_upd": 0, "no_patient": 0, "skipped_after": 0, "deleted": 0,
           "materials": 0, "materials_unknown": 0, "no_date": 0}
    pats = {p.cleverbox_id: p.id for p in db.scalars(select(Patient).where(Patient.cleverbox_id.is_not(None)))}
    docs = {d.cleverbox_id: d.id for d in db.scalars(select(Doctor).where(Doctor.cleverbox_id.is_not(None)))}
    svc_ids = {x for (x,) in db.execute(select(CbService.id))}
    prods: dict[int, Product] = {}
    for pr in db.scalars(select(Product).where(Product.cb_product_id.is_not(None))):
        cur = prods.get(pr.cb_product_id)
        if not cur or WH_PREF.index(pr.warehouse if pr.warehouse in WH_PREF else "vitrina") < \
                WH_PREF.index(cur.warehouse if cur.warehouse in WH_PREF else "vitrina"):
            prods[pr.cb_product_id] = pr
    rids = [int(v["rid"]) for v in visits if str(v.get("rid") or "").isdigit()]
    existing: dict[int, list[Appointment]] = defaultdict(list)
    for a in db.scalars(select(Appointment).where(Appointment.cb_record_id.in_(rids or [0]))):
        existing[a.cb_record_id].append(a)
    for v in visits:
        rid = int(v.get("rid") or 0)
        if not rid:
            continue
        if v.get("del"):
            rep["deleted"] += 1
            continue
        start = _dt(v.get("start"))
        if not start:
            rep["no_date"] += 1
            continue
        if until and start >= until:
            rep["skipped_after"] += 1
            continue
        pid = pats.get(int(v.get("cid") or 0))
        if not pid:
            rep["no_patient"] += 1
            continue
        items = [it for it in v.get("items") or [] if it]
        groups: dict[int, list] = defaultdict(list)
        for it in items:
            groups[int(it.get("m") or 0)].append(it)
        if not groups:
            groups[0] = []
        total = sum(_f(it.get("s")) for it in items) or 1.0
        status = _status(v.get("stt"), v.get("st"))
        added = None
        try:
            added = datetime.utcfromtimestamp(int(v["added"])) if v.get("added") else None
        except (TypeError, ValueError, OverflowError):
            added = None
        mine = existing.get(rid, [])
        t = start
        for mid, its in groups.items():
            doc = docs.get(mid)
            a = next((x for x in mine if x.doctor_id == doc), None) or (mine[0] if len(mine) == 1 and len(groups) == 1 else None)
            new = a is None
            if new:
                a = Appointment(patient_id=pid, cb_record_id=rid, source="cleverbox", start=t, end=t)
                db.add(a)
                mine.append(a)
            mins = sum(max(5, int(_f(it.get("min")) or 30)) for it in its) or 30
            a.patient_id, a.doctor_id, a.start, a.end = pid, doc, t, t + timedelta(minutes=mins)
            a.services = [{"id": int(it["sid"]) if str(it.get("sid") or "").isdigit() and int(it["sid"]) in svc_ids else None,
                           "cb_id": int(it["sid"]) if str(it.get("sid") or "").isdigit() else None,
                           "name": str(it.get("t") or "Послуга")[:300], "price": round(_f(it.get("s")) or _f(it.get("p")) * (_f(it.get("q")) or 1), 2),
                           "list_price": round(_f(it.get("p")), 2), "qty": _f(it.get("q")) or 1, "discount": round(_f(it.get("d")), 2),
                           "minutes": max(5, int(_f(it.get("min")) or 30)), "master": it.get("mn") or None} for it in its]
            share = sum(_f(it.get("s")) for it in its) / total if len(groups) > 1 else 1.0
            a.status, a.cb_status = status, (str(v.get("stt") or "") or status)[:80]
            a.cb_check_id = int(v["check"]) if str(v.get("check") or "").isdigit() and int(v["check"]) else None
            a.cb_paid = round(_f(v.get("paid")) * share, 2) if v.get("paid") not in (None, "") else None
            a.cb_discount = round(_f(v.get("disc")) * share, 2) or None
            a.cb_author = (str(v.get("author") or "")[:120]) or None
            if added and new:
                a.created_at = added
            a.updated_at = datetime.utcnow()
            db.flush()
            rep["appts_new" if new else "appts_upd"] += 1
            # фактичні витратні з CleverBox (замінюють будь-які попередні)
            for m in db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == a.id)):
                db.delete(m)
            for idx, it in enumerate(its):
                for row in it.get("mats") or []:
                    try:
                        cpid, qty, price = int(row[0]), _f(row[1]), _f(row[2])
                    except (TypeError, ValueError, IndexError):
                        continue
                    pr = prods.get(cpid)
                    if not pr or qty <= 0:
                        rep["materials_unknown"] += 1
                        continue
                    db.add(VisitMaterial(appt_id=a.id, svc_idx=idx, service_id=a.services[idx].get("id"), cat="Витратні (CleverBox)",
                                         product_id=pr.id, qty=qty, unit_cost=price, warehouse=pr.warehouse))
                    rep["materials"] += 1
            t = a.end
    db.commit()
    return rep
