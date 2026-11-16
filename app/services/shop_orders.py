"""Замовлення інтернет-магазину в CRM: перенос усіх замовлень і покупців із Shop-Express (molodist.store) і щоденне оновлення,
поки магазин працює на Shop-Express. Далі сюди ж ляжуть замовлення власного магазину CRM (source="crm").

• Замовлення — SiteOrder (upsert за order_id: статус, оплата, суми оновлюються).
• Покупець → картка пацієнта за телефоном; якщо картки немає — створюється (джерело «Інтернет-магазин»).
• Позиції → товари CRM за id товару на сайті (shop_id) / батьківським id / артикулом.
• Повні сирі експорти (замовлення, покупці, категорії, бренди, статуси) — файлами на сервері (files/shop_express/)."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Patient, Product, SiteOrder, SyncState

KEY = "shop_orders_report"
CANCEL = ("скасован", "фейк", "поверн", "cancel")


def _uk(v) -> str:
    if isinstance(v, dict):
        val = v.get("value", v)
        if isinstance(val, dict):
            return str(val.get("uk") or val.get("ua") or val.get("ru") or next(iter(val.values()), "") or "")
        return str(v.get("uk") or v.get("ua") or v.get("name") or v.get("title") or "")
    return str(v or "")


def _dt(s) -> datetime | None:
    for f in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(s)[:19], f)
        except (TypeError, ValueError):
            continue
    return None


def _f(v) -> float | None:
    try:
        return round(float(str(v).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def is_lost(o: SiteOrder) -> bool:
    return any(k in (o.status or "").lower() for k in CANCEL)


def _client(db: Session):
    from ..integrations import shop_express as sx
    c = sx.creds(db)
    if not c.get("login"):
        raise sx.ShopError("Не задано API-користувача Shop-Express")
    cl = sx.ShopExpress(c["site"], c["login"], c["password"])
    cl.how = (sx.last_report(db) or {}).get("token_in") or "body"
    return cl


def _backup(name: str, rows) -> str:
    d = Path(settings.files_dir) / "shop_express"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}-{datetime.now():%Y%m%d-%H%M}.json"
    f.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return f.name


def _patient(db: Session, name: str, phone: str | None, email: str | None, cache: dict, create: bool = True) -> Patient | None:
    from . import appeals as ap
    from ..integrations.messaging import normalize_phone
    ph = normalize_phone(phone) if phone else None
    if not ph:
        return None
    if ph in cache:
        return cache[ph]
    p = ap.find_patient(db, ph)
    if not p and create:
        p = Patient(full_name=(name or "").strip()[:200] or ph, phone=ph, source="Інтернет-магазин",
                    notes="Картку створено з замовлення інтернет-магазину molodist.store")
        if email and hasattr(p, "email"):
            p.email = email[:200]
        db.add(p)
        db.flush()
    cache[ph] = p
    return p


def sync(db: Session, client=None, full: bool = False, job=None) -> dict:
    """Замовлення з Shop-Express → CRM. full=True — усі замовлення (перший перенос), інакше — за останні 60 днів."""
    from ..integrations import shop_express as sx
    log = (lambda m: job.log(m)) if job else (lambda m: None)
    client = client or _client(db)
    body = {"additional_data": 1}
    if not full:
        body["from"] = (datetime.now() - timedelta(days=60)).strftime("%Y.%m.%d")
    log("Завантажую замовлення…")
    rows = list(sx.export(client, "api/orders/export", "orders", body))
    rep = {"at": datetime.now().isoformat(timespec="minutes"), "orders": len(rows), "new": 0, "updated": 0, "patients_new": 0,
           "linked": 0, "items": 0, "items_matched": 0, "full": full}
    if full:
        rep["backup"] = _backup("orders", rows)
        for path, key, nm in (("api/users/export", "users", "users"), ("api/categories/export", "categories", "categories"),
                              ("api/brands/export", "brands", "brands")):
            try:
                data = list(sx.export(client, path, key, {}))
                rep[f"backup_{nm}"] = _backup(nm, data)
                rep[nm] = len(data)
                if nm in ("categories", "brands"):
                    db.merge(SyncState(key=f"shop_{nm}", value=json.dumps(data, ensure_ascii=False), updated_at=datetime.utcnow()))
            except Exception as e:  # noqa: BLE001 — не критично для замовлень
                rep[f"{nm}_error"] = str(e)[:200]
    prods = db.scalars(select(Product)).all()
    by_shop = {str(p.shop_id): p for p in prods if p.shop_id}
    by_sku = {p.sku.strip().lower(): p for p in prods if p.sku}
    have = {o.ext_id: o for o in db.scalars(select(SiteOrder).where(SiteOrder.source == "shop_express"))}
    cache: dict = {}
    n_pat_before = db.query(Patient).count()
    now = datetime.now()
    for r in rows:
        ext = str(r.get("order_id") or r.get("id") or "")
        if not ext:
            continue
        o = have.get(ext)
        if not o:
            o = SiteOrder(source="shop_express", ext_id=ext)
            db.add(o)
            have[ext] = o
            rep["new"] += 1
        else:
            rep["updated"] += 1
        st = r.get("stat_status") or {}
        o.number = str(r.get("order_number") or "")[:40] or None
        o.created_at = _dt(r.get("stat_created")) or o.created_at
        o.status_id = st.get("id") if isinstance(st, dict) else None
        o.status = _uk(st)[:80] or None
        o.paid = bool(int(r.get("payed") or 0))
        o.total = _f(r.get("total_sum"))
        o.total_full = _f(r.get("total_default"))
        o.delivery_price = _f(r.get("delivery_price"))
        o.delivery = _uk(r.get("delivery_type"))[:120] or None
        o.payment = _uk(r.get("payment_type"))[:120] or None
        o.name = (r.get("delivery_name") or "")[:200] or None
        o.phone = (r.get("delivery_phone") or "")[:40] or None
        o.email = (r.get("delivery_email") or "")[:200] or None
        o.city = (r.get("delivery_city") or "")[:120] or None
        o.address = (r.get("delivery_address") or "").strip(" ,")[:500] or None
        o.comment = r.get("comment") or None
        o.shop_user = str(r.get("user") or "")[:40] or None
        items = []
        for it in r.get("product") or []:
            p = by_shop.get(str(it.get("id"))) or by_shop.get(str(it.get("parent_id"))) or by_sku.get(str(it.get("sku") or "").strip().lower())
            rep["items"] += 1
            rep["items_matched"] += bool(p)
            items.append({"shop_id": it.get("id"), "parent_id": it.get("parent_id"), "sku": it.get("sku"),
                          "name": (p.name if p else None) or it.get("title") or it.get("name") or f"Товар сайту {it.get('id')}",
                          "qty": _f(it.get("quantity")) or 1, "price": _f(it.get("price")), "total": _f(it.get("total_price")),
                          "discount": it.get("discount_marker"), "product_id": p.id if p else None})
        o.items = items
        o.extra = [{"name": _uk(a), "price": _f(a.get("price")), "on": bool(a.get("checked"))} for a in r.get("order_additional") or []] or None
        o.raw = r
        o.synced_at = now
        if not o.patient_id:
            pat = _patient(db, o.name or "", o.phone, o.email, cache)
            if pat:
                o.patient_id = pat.id
                rep["linked"] += 1
    db.commit()
    rep["patients_new"] = db.query(Patient).count() - n_pat_before
    db.merge(SyncState(key=KEY, value=json.dumps(rep, ensure_ascii=False), updated_at=datetime.utcnow()))
    db.commit()
    return rep


def last(db: Session) -> dict | None:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else None
    except ValueError:
        return None


# ---------------- для сторінок ----------------
def listing(db: Session, d_from: date | None, d_to: date | None, status: str = "", q: str = "") -> list[SiteOrder]:
    qq = select(SiteOrder).order_by(SiteOrder.created_at.desc().nullslast(), SiteOrder.id.desc())
    if d_from:
        qq = qq.where(SiteOrder.created_at >= datetime.combine(d_from, datetime.min.time()))
    if d_to:
        qq = qq.where(SiteOrder.created_at <= datetime.combine(d_to, datetime.max.time()))
    if status:
        qq = qq.where(SiteOrder.status == status)
    rows = db.scalars(qq).all()
    if q.strip():
        ql = q.strip().lower()
        dq = re.sub(r"\D", "", ql)
        rows = [o for o in rows if ql in f"{o.number} {o.name} {o.email} {o.city}".lower() or (dq and len(dq) >= 4 and dq in re.sub(r"\D", "", o.phone or ""))]
    return rows


def statuses(db: Session) -> list[str]:
    return sorted({s for (s,) in db.execute(select(SiteOrder.status).distinct()) if s})


def for_patient(db: Session, pid: int) -> list[SiteOrder]:
    return db.scalars(select(SiteOrder).where(SiteOrder.patient_id == pid).order_by(SiteOrder.created_at.desc().nullslast())).all()


def kpi(rows: list[SiteOrder]) -> dict:
    ok = [o for o in rows if not is_lost(o)]
    rev = sum(o.total or 0 for o in ok)
    buyers = defaultdict(int)
    for o in ok:
        buyers[o.patient_id or o.phone or o.email or o.id] += 1
    return {"orders": len(rows), "ok": len(ok), "lost": len(rows) - len(ok), "revenue": round(rev, 2),
            "avg": round(rev / len(ok), 2) if ok else 0, "buyers": len(buyers), "repeat": sum(1 for v in buyers.values() if v > 1),
            "paid": sum(1 for o in ok if o.paid)}
