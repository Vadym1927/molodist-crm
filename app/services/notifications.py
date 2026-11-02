"""Дзвіночок — системні сповіщення: онлайн-записи, терміни придатності, критичні залишки, сертифікати й абонементи,
що закінчуються. Важкі підрахунки (склад) кешуються на 10 хв у процесі."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, User
from ..security import can

_CACHE: dict = {"at": None, "data": None}
TTL = 600


def _local(dt: datetime | None) -> datetime | None:
    from .day_stats import _tz_shift
    return dt + timedelta(hours=_tz_shift()) if dt else None


def online(db: Session, since: datetime) -> list[dict]:
    out = []
    for a in db.scalars(select(Appointment).where(Appointment.source.in_(("online", "bot")), Appointment.created_at >= since)
                        .order_by(Appointment.created_at.desc()).limit(50)):
        svc = ", ".join(str(x.get("name") or "") for x in a.services or []) or "без послуги"
        out.append({"kind": "online", "id": a.id, "at": a.created_at, "level": "info",
                    "title": "Онлайн-запис" + (" (скасовано)" if a.status == "cancelled" else ""),
                    "text": f"{a.patient.full_name if a.patient else '—'} → {svc} · {a.start:%d.%m %H:%M}",
                    "url": f"/calendar?start={a.start:%Y-%m-%d}&open={a.id}"})
    return out


def _summary(db: Session) -> list[dict]:
    now = datetime.utcnow()
    if _CACHE["at"] and (now - _CACHE["at"]).total_seconds() < TTL:
        return _CACHE["data"]
    day = datetime.combine(date.today(), datetime.min.time()) - timedelta(hours=3)
    out = []
    try:
        from . import expiry
        rs = [r for r in expiry.rows(db) if r["days"] is not None and r["days"] <= 30]
        exp = [r for r in rs if r["days"] < 0]
        if exp:
            out.append({"kind": "expired", "at": day, "level": "bad", "title": "Прострочений товар на складі",
                        "text": f"{len(exp)} позицій — термін минув", "url": "/reports?r=expiry", "perm": "stock"})
        soon = [r for r in rs if r["days"] >= 0]
        if soon:
            out.append({"kind": "expiry", "at": day, "level": "warn", "title": "Закінчується термін придатності",
                        "text": f"{len(soon)} позицій — до 30 днів: " + ", ".join(r["product"].name for r in soon[:3]) + ("…" if len(soon) > 3 else ""),
                        "url": "/reports?r=expiry", "perm": "stock"})
    except Exception:  # noqa: BLE001
        pass
    try:
        from .catalog import product_rows
        from .stock import is_critical
        crit = [r for r in product_rows(db, []) if r.product.active is not False and is_critical(r)]
        if crit:
            out.append({"kind": "critical", "at": day, "level": "warn", "title": "Критичні залишки",
                        "text": f"{len(crit)} товарів: " + ", ".join(r.product.name for r in crit[:3]) + ("…" if len(crit) > 3 else ""),
                        "url": "/stock?tab=critical", "perm": "stock"})
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import certificates as cert
        cs = cert.expiring(db)
        if cs:
            out.append({"kind": "cert", "at": day, "level": "warn", "title": "Сертифікати скоро згорять",
                        "text": f"{len(cs)} сертифікатів із залишком — до 14 днів", "url": "/gifts", "perm": "patients_all"})
        ss = cert.subs_expiring(db)
        if ss:
            out.append({"kind": "sub", "at": day, "level": "warn", "title": "Абонементи закінчуються",
                        "text": f"{len(ss)} абонементів — скоро закінчиться термін", "url": "/gifts?tab=subs", "perm": "patients_all"})
    except Exception:  # noqa: BLE001
        pass
    _CACHE.update(at=now, data=out)
    return out


def items(db: Session, user: User) -> list[dict]:
    res = online(db, datetime.utcnow() - timedelta(days=7)) if can(user, "book") else []
    res += [x for x in _summary(db) if not x.get("perm") or can(user, x["perm"])]
    seen = user.notif_seen_at
    for x in res:
        x["new"] = not seen or (x["at"] and x["at"] > seen)
        x["at_local"] = _local(x["at"])
    res.sort(key=lambda x: (not x["new"], -(x["at"].timestamp() if x["at"] else 0)))
    return res


def unseen(db: Session, user: User) -> int:
    return sum(1 for x in items(db, user) if x["new"])
