"""Звіт «Касові зміни» (/reports?r=shifts): усі зміни за період — хто й коли відкрив і закрив, по кожній касі залишок на початок,
надходження (чеки), видатки, переміщення й залишок на кінець; продажі за зміну (чеків, сума, за способами оплати);
розбіжності — операції, внесені в касу заднім числом після закриття зміни (залишок на закритті ≠ перерахованому зараз)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CashShift, User


def _dur(a: datetime, b: datetime | None) -> int:
    return int(((b or datetime.now()) - a).total_seconds() // 60)


def dur_text(minutes: int) -> str:
    h, m = divmod(max(minutes, 0), 60)
    return f"{h} год {m:02d} хв" if h else f"{m} хв"


def build(db: Session, d_from: date, d_to: date, user_id: int | None = None) -> dict:
    from . import cashboxes, shifts as sh
    t0, t1 = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to + timedelta(days=1), datetime.min.time())
    q = select(CashShift).where(((CashShift.opened_at >= t0) & (CashShift.opened_at < t1)) | CashShift.closed_at.is_(None))
    if user_id:
        q = q.where(CashShift.opened_by == user_id)
    all_ = db.scalars(q.order_by(CashShift.opened_at.desc())).all()
    users = {u.id: u.name for u in db.scalars(select(User))}
    boxes = {int(x["id"]): x for x in cashboxes.items(db)}
    mv_cache: dict[int, list] = {}

    def moves_of(bid: int):
        if bid not in mv_cache:
            mv_cache[bid] = cashboxes.moves(db, boxes[bid]) if bid in boxes else []
        return mv_cache[bid]

    rows, open_now = [], []
    for s in all_:
        end = s.closed_at or datetime.now()
        brow, cash_in = [], 0.0
        for b in s.boxes or []:
            bid = int(b["id"])
            mv = [m for m in moves_of(bid) if s.opened_at <= m.at <= end]
            inc = round(sum(m.amount for m in mv if m.kind == "in" and not m.transfer), 2)
            out = round(sum(m.amount for m in mv if m.kind == "out" and not m.transfer), 2)
            tin = round(sum(m.amount for m in mv if m.kind == "in" and m.transfer), 2)
            tout = round(sum(m.amount for m in mv if m.kind == "out" and m.transfer), 2)
            opn = b.get("open")
            expected = round(float(opn) + inc - out + tin - tout, 2) if opn is not None else None
            close = b.get("close")
            now_bal = cashboxes.balance_at(db, boxes[bid], end, moves_of(bid)) if bid in boxes and s.closed_at else None
            late = round(now_bal - close, 2) if (now_bal is not None and close is not None) else None   # внесено після закриття
            if b.get("type") == "cash":
                cash_in += inc
            brow.append({"id": bid, "name": b.get("name"), "type": b.get("type"), "open": opn, "close": close, "in": inc, "out": out,
                         "tin": tin, "tout": tout, "expected": expected, "late": late if late else 0.0,
                         "moves": sorted(mv, key=lambda m: m.at)})
        summ = s.summary if s.closed_at and s.summary else sh.sales_summary(db, s, end)
        r = {"id": s.id, "opened_at": s.opened_at, "closed_at": s.closed_at, "opened_by": users.get(s.opened_by, "—"),
             "closed_by": users.get(s.closed_by) if s.closed_by else None, "minutes": _dur(s.opened_at, s.closed_at),
             "boxes": brow, "checks": summ.get("checks", 0), "total": float(summ.get("total") or 0), "by_method": summ.get("by_method") or {},
             "test": summ.get("test", 0), "note": s.note, "cash_in": round(cash_in, 2),
             "late": round(sum(abs(x["late"]) for x in brow), 2), "open": s.closed_at is None}
        (open_now if r["open"] else rows).append(r)
    closed = rows
    n = len(closed)
    by_user: dict[str, dict] = defaultdict(lambda: {"n": 0, "total": 0.0, "checks": 0, "minutes": 0})
    for r in closed:
        d = by_user[r["opened_by"]]
        d["n"] += 1
        d["total"] += r["total"]
        d["checks"] += r["checks"]
        d["minutes"] += r["minutes"]
    bu = sorted(({"name": k, **v, "avg": round(v["total"] / v["n"], 2) if v["n"] else 0} for k, v in by_user.items()), key=lambda x: -x["total"])
    meth: dict[str, float] = defaultdict(float)
    for r in closed:
        for k, v in r["by_method"].items():
            meth[k] += float(v or 0)
    tot = round(sum(r["total"] for r in closed), 2)
    return {"d_from": d_from, "d_to": d_to, "rows": closed, "open": open_now, "n": n, "n_open": len(open_now),
            "total": tot, "checks": sum(r["checks"] for r in closed), "avg": round(tot / n, 2) if n else 0,
            "avg_min": int(sum(r["minutes"] for r in closed) / n) if n else 0,
            "late_n": sum(1 for r in closed if r["late"]), "late_sum": round(sum(r["late"] for r in closed), 2),
            "by_user": bu, "by_method": sorted(meth.items(), key=lambda x: -x[1]),
            "users": sorted(({"id": k, "name": v} for k, v in users.items()), key=lambda x: x["name"]), "user_id": user_id,
            "days": {r["opened_at"].date() for r in closed}}


def to_xlsx(r: dict) -> bytes:
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Касові зміни"
    ws.append([f"Касові зміни · {r['d_from']:%d.%m.%Y} — {r['d_to']:%d.%m.%Y}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["№", "Відкрито", "Ким", "Закрито", "Ким", "Тривалість", "Каса", "На початок", "Надходження", "Видатки", "Переміщення +",
               "Переміщення −", "На кінець", "Внесено після закриття", "Чеків", "Продажі за зміну", "Примітка"])
    for c in ws[2]:
        c.font = Font(bold=True)
    for s in r["rows"]:
        first = True
        for b in s["boxes"] or [{"name": "—"}]:
            ws.append([s["id"] if first else "", s["opened_at"].strftime("%d.%m.%Y %H:%M") if first else "", s["opened_by"] if first else "",
                       s["closed_at"].strftime("%d.%m.%Y %H:%M") if first and s["closed_at"] else "", (s["closed_by"] or "") if first else "",
                       dur_text(s["minutes"]) if first else "", b.get("name"), b.get("open"), b.get("in"), b.get("out"), b.get("tin"), b.get("tout"),
                       b.get("close"), b.get("late") or None, s["checks"] if first else "", s["total"] if first else "", (s["note"] or "") if first else ""])
            first = False
    ws.append([])
    ws.append(["Разом змін", r["n"], "", "", "", "", "", "", "", "", "", "", "", r["late_sum"], r["checks"], r["total"]])
    ws.column_dimensions["G"].width = 28
    ws.column_dimensions["Q"].width = 40
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
