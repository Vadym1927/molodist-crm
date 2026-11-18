"""Нова пошта: автоматичні статуси замовлень сайту за ТТН.

Раз на годину: «Відправлено» з ТТН → питаємо НП (TrackingDocument.getStatusDocuments, до 100 ТТН за раз).
- прибуло у відділення / поштомат → одна SMS (бот турботи → SMS) покупцю;
- отримано → статус «Отримано» + запит відгуку одразу; через 3 дні після отримання → «Закрито»;
- відмова / повернення / не вручено → задача адміністратору (раз на замовлення).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SiteOrder, Task

ARRIVED = {7, 8}
RECEIVED = {9, 10, 11, 106}
REFUSED = {102, 103, 105, 108, 111}
CLOSE_DAYS = 3


def _codes(c: dict, ttns: list[tuple[str, str]], http: httpx.Client | None = None) -> dict[str, dict]:
    from .shop_front import np_call
    out: dict[str, dict] = {}
    for i in range(0, len(ttns), 100):
        docs = [{"DocumentNumber": t, "Phone": (p or "")[-12:]} for t, p in ttns[i:i + 100]]
        for r in np_call(c, "TrackingDocument", "getStatusDocuments", {"Documents": docs}, http):
            try:
                out[str(r.get("Number") or "")] = {"code": int(r.get("StatusCode") or 0), "text": str(r.get("Status") or "")[:120],
                                                   "wh": str(r.get("WarehouseRecipientNumber") or r.get("WarehouseRecipient") or "")[:80]}
            except (TypeError, ValueError):
                continue
    return out


def _task(db: Session, o: SiteOrder, st: dict) -> None:
    t = Task(title=f"Нова пошта: {st['text'] or 'відмова / повернення'} — замовлення №{o.number or o.id}"[:200],
             description=f"ТТН {(o.raw or {}).get('ttn')}. Звʼязатися з покупцем ({o.name or ''}, {o.phone or ''}): уточнити причину, "
                         "домовитися про повторну відправку або оформити повернення. Замовлення — «Магазин → Замовлення».",
             kind="shop_np", source_key=f"shop_np:{o.id}", patient_id=o.patient_id, assignee_role="admin",
             due_date=date.today(), priority="high", created_at=datetime.utcnow())
    db.add(t)


def job(db: Session, c: dict, http: httpx.Client | None = None) -> dict:
    from . import shop_front as sf
    rep = {"checked": 0, "arrived": 0, "received": 0, "refused": 0, "closed": 0}
    now = datetime.now()
    # «Отримано» понад 3 дні → «Закрито»
    for o in db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.status == "Отримано")).all():
        at = (o.raw or {}).get("recv_at")
        if at and at <= (now - timedelta(days=CLOSE_DAYS)).isoformat(timespec="minutes"):
            sf.set_status(db, o, "Закрито")
            sf.log(o, f"Закрито автоматично: {CLOSE_DAYS} дні після отримання")
            rep["closed"] += 1
    if not (c.get("np_key") or "").strip() or c.get("np_track") is False:
        db.flush()
        return rep
    rows = [o for o in db.scalars(select(SiteOrder).where(SiteOrder.source == "crm", SiteOrder.status == "Відправлено")).all()
            if (o.raw or {}).get("ttn")]
    if not rows:
        db.flush()
        return rep
    codes = _codes(c, [((o.raw or {})["ttn"], o.phone or "") for o in rows], http)
    for o in rows:
        st = codes.get(str((o.raw or {})["ttn"]))
        if not st:
            continue
        rep["checked"] += 1
        raw = dict(o.raw or {})
        if raw.get("np_code") != st["code"]:
            raw["np_code"], raw["np_status"] = st["code"], st["text"]
            o.raw = raw
            sf.log(o, f"Нова пошта: {st['text']}")
            raw = dict(o.raw)
        if st["code"] in ARRIVED and "arrived" not in (raw.get("cust_msgs") or []):
            raw["np_wh"] = st["wh"]
            o.raw = raw
            sf._cust(db, o, "arrived")
            rep["arrived"] += 1
        elif st["code"] in RECEIVED:
            raw.setdefault("review_due", now.isoformat(timespec="minutes"))   # запит відгуку — одразу після отримання
            o.raw = raw
            sf.set_status(db, o, "Отримано")
            sf.log(o, "Отримано (за даними Нової пошти)")
            rep["received"] += 1
        elif st["code"] in REFUSED and not raw.get("np_refused"):
            raw["np_refused"] = now.isoformat(timespec="minutes")
            o.raw = raw
            _task(db, o, st)
            sf.log(o, "Задача адміністратору: відмова / повернення")
            rep["refused"] += 1
    db.flush()
    return rep
