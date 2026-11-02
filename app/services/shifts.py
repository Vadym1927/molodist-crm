"""Касові зміни (як у CleverBox): відкрити на обрані каси (готівка, термінали) із залишками на початок;
закрити — із залишками на кінець і підсумком продажів каси CRM за зміну."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CashShift, Sale, User


class ShiftError(ValueError):
    pass


def boxes(db: Session) -> list[dict]:
    """Каси, які відкриваються зміною: готівка й термінали (як плитки в касі), з поточним залишком."""
    from . import cashboxes
    return [x for x in cashboxes.overview(db) if x.get("active", True) is not False and x.get("type") in ("cash", "card")]


def open_shifts(db: Session) -> list[CashShift]:
    return db.scalars(select(CashShift).where(CashShift.closed_at.is_(None)).order_by(CashShift.opened_at)).all()


def current(db: Session, user: User | None = None) -> CashShift | None:
    """Відкрита зміна користувача (кожен користувач — своя зміна на свої каси)."""
    q = select(CashShift).where(CashShift.closed_at.is_(None))
    if user is not None:
        q = q.where(CashShift.opened_by == user.id)
    return db.scalar(q.order_by(CashShift.opened_at.desc()).limit(1))


def busy(db: Session, exclude_user: int | None = None) -> dict[int, int]:
    """Каси, відкриті у змінах інших користувачів: id каси → id користувача (одну касу відкриває лише один)."""
    out = {}
    for s in open_shifts(db):
        if exclude_user is not None and s.opened_by == exclude_user:
            continue
        for b in s.boxes or []:
            out[int(b["id"])] = s.opened_by
    return out


def recent(db: Session, limit: int = 8) -> list[CashShift]:
    return db.scalars(select(CashShift).order_by(CashShift.opened_at.desc()).limit(limit)).all()


def open_shift(db: Session, user: User, box_ids: list[int]) -> CashShift:
    if current(db, user):
        raise ShiftError("У вас уже відкрита зміна — спершу закрийте її")
    bx = {int(x["id"]): x for x in boxes(db)}
    sel = [bx[i] for i in box_ids if i in bx]
    if not sel:
        raise ShiftError("Оберіть хоча б одну касу")
    taken = busy(db)
    if any(int(x["id"]) in taken for x in sel):
        names = {u.id: u.name for u in db.scalars(select(User))}
        raise ShiftError("Каса вже відкрита іншим користувачем: " + ", ".join(
            f"{x['name']} ({names.get(taken[int(x['id'])], '—')})" for x in sel if int(x["id"]) in taken))
    sh = CashShift(opened_by=user.id, opened_at=datetime.now(), location_id=sel[0].get("location_id"),
                   boxes=[{"id": int(x["id"]), "name": x["name"], "type": x.get("type"), "open": x.get("balance")} for x in sel])
    db.add(sh)
    db.commit()
    return sh


def sales_summary(db: Session, sh: CashShift, until: datetime | None = None) -> dict:
    until = until or sh.closed_at or datetime.now()
    rows = db.scalars(select(Sale).where(Sale.at >= sh.opened_at, Sale.at <= until, Sale.status == "paid",
                                         Sale.created_by == sh.opened_by)).all()
    by: dict[str, float] = {}
    for s in rows:
        for p in s.payments:
            by[p.method] = round(by.get(p.method, 0) + float(p.amount or 0), 2)
    return {"checks": len(rows), "total": round(sum(float(s.total or 0) for s in rows), 2), "by_method": by,
            "test": sum(1 for s in rows if (s.fiscal or {}).get("test_sale"))}


def close_shift(db: Session, user: User, note: str = "", shift_id: int | None = None) -> CashShift:
    """Закриває свою зміну; власник може закрити й чужу (shift_id)."""
    sh = db.get(CashShift, shift_id) if shift_id and user.role == "owner" else current(db, user)
    if not sh or sh.closed_at:
        raise ShiftError("Немає відкритої зміни")
    now_bal = {int(x["id"]): x.get("balance") for x in boxes(db)}
    sh.boxes = [{**b, "close": now_bal.get(int(b["id"]))} for b in (sh.boxes or [])]
    sh.closed_at, sh.closed_by = datetime.now(), user.id
    sh.summary = sales_summary(db, sh, sh.closed_at)
    sh.note = (note or "").strip()[:500] or None
    db.commit()
    return sh


def online(db: Session, minutes: int = 5) -> list[User]:
    t = datetime.utcnow() - timedelta(minutes=minutes)
    return db.scalars(select(User).where(User.active.is_(True), User.last_seen >= t).order_by(User.name)).all()


def photo_url(u: User) -> str | None:
    """Фото профілю: власне завантажене, інакше — аватар картки лікаря (якщо користувач прив'язаний до спеціаліста)."""
    try:
        if u.photo_path:
            return f"/me/photo/{u.id}?v={u.photo_path.replace(chr(92), '/').rsplit('/', 1)[-1][:24]}"
        if u.doctor and u.doctor.photo_url:
            return u.doctor.photo_url
        return _doc_photo_by_name(u.name)
    except Exception:  # noqa: BLE001 — користувач поза сесією (сторінка помилки) — без фото
        return None


_BY_NAME: dict = {"at": None, "map": {}}


def _doc_photo_by_name(name: str | None) -> str | None:
    """Користувач не прив'язаний до картки спеціаліста — шукаємо картку з тим самим ПІБ (власник, адміністратор)."""
    if not name:
        return None
    now = datetime.utcnow()
    if not _BY_NAME["at"] or (now - _BY_NAME["at"]).total_seconds() > 300:
        from ..models import Doctor, SessionLocal
        with SessionLocal() as db:
            _BY_NAME["map"] = {_key(d.name): d.photo_url for d in db.scalars(select(Doctor).where(Doctor.photo_url.is_not(None)))
                               if d.photo_url}
        _BY_NAME["at"] = now
    return _BY_NAME["map"].get(_key(name))


def _key(n: str) -> str:
    return " ".join(sorted((n or "").lower().replace("ʼ", "'").split()[:2]))
