"""Звіт «Отримано грошей від пацієнтів» (/reports?r=patient_money): скільки грошей приніс кожен пацієнт за період
(рік / місяць / весь час з 2022), його частка від загальної суми, накопичена частка, ABC-сегмент.

Джерело — журнал фіноперацій (finops.journal): доходи грошима від пацієнтів з CleverBox (з 2022) і каси CRM.
Не рахуються: переміщення, оплати не грошима (бонуси, сертифікати, баланс), статті «не в дохід» (Налаштування → Звіти),
надходження не від пацієнтів (внесення в касу, постачальники). Повернення й сторно зменшують суму пацієнта."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .pnl import FIRST_YEAR, MONTHS_FULL

REFUND_ARTICLES = ("повернення коштів", "повернення")
NOT_PATIENT = ("внесення", "інкасац", "постачальн", "банк", "каса", "фоп ", "тов ", "пп ", "фізична особа-підприємець")


def _key(name: str) -> str:
    return re.sub(r"[^\w]+", " ", (name or "").lower().replace("ʼ", "").replace("'", "")).strip()


def years(db: Session) -> list[int]:
    from .pnl import years as _y
    return _y(db)


def period(year: int | None, month: int | None) -> tuple[date, date, str]:
    today = date.today()
    if not year:
        return date(FIRST_YEAR, 1, 1), today, "весь час"
    if month:
        a = date(year, month, 1)
        b = date(year + (month == 12), month % 12 + 1, 1)
        from datetime import timedelta
        return a, b - timedelta(days=1), f"{MONTHS_FULL[month - 1].lower()} {year}"
    return date(year, 1, 1), min(date(year, 12, 31), today) if year == today.year else date(year, 12, 31), f"{year} рік"


def build(db: Session, year: int | None, month: int | None = None, q: str = "") -> dict:
    from . import finops as fo, clinic
    from ..models import Patient
    d_from, d_to, label = period(year, month)
    ex = clinic.excluded_income(db)
    rows = fo.journal(db, d_from, d_to)
    # пацієнти за іменем — для операцій CleverBox, де є лише ПІБ
    by_name: dict[str, tuple[int, str]] = {}
    name_of: dict[int, str] = {}
    for pid, nm in db.execute(select(Patient.id, Patient.full_name)):
        name_of[pid] = nm
        k = _key(nm)
        if k and k not in by_name:
            by_name[k] = (pid, nm)
    acc: dict[str, dict] = {}
    total = refunds = 0.0
    skipped = {"not_patient": 0.0, "non_money": 0.0, "excluded": 0.0}
    for r in rows:
        if r.kind == "transfer":
            continue
        art = (r.article or "").strip().lower()
        if r.kind == "income" and art in ex:
            skipped["excluded"] += r.amount
            continue
        if not r.money:
            if r.kind == "income":
                skipped["non_money"] += r.amount
            continue
        is_refund = r.kind == "storno" or (r.kind == "expense" and any(art.startswith(x) for x in REFUND_ARTICLES))
        if r.kind != "income" and not is_refund:
            continue
        who = (r.who or "").strip()
        pid = None
        if r.who_link and r.who_link.startswith("/patients/"):
            pid = int(r.who_link.rsplit("/", 1)[1])
        elif who and who != "—":
            hit = by_name.get(_key(who))
            pid = hit[0] if hit else None
        if pid is None and (not who or who == "—" or any(x in who.lower() for x in NOT_PATIENT)):
            if r.kind == "income":
                skipped["not_patient"] += r.amount
            continue
        key = f"p{pid}" if pid else f"n{_key(who)}"
        a = acc.setdefault(key, {"pid": pid, "name": name_of.get(pid, who) if pid else who,
                                 "sum": 0.0, "n": 0, "refund": 0.0, "first": None, "last": None, "months": defaultdict(float)})
        if is_refund:
            a["sum"] -= r.amount
            a["refund"] += r.amount
            refunds += r.amount
            continue
        a["sum"] += r.amount
        a["n"] += 1
        total += r.amount
        d = r.at.date()
        a["first"] = d if not a["first"] or d < a["first"] else a["first"]
        a["last"] = d if not a["last"] or d > a["last"] else a["last"]
        a["months"][d.strftime("%Y-%m")] += r.amount
    net = round(total - refunds, 2)
    lst = sorted((x for x in acc.values() if abs(x["sum"]) >= 0.005), key=lambda x: -x["sum"])
    cum = 0.0
    for i, x in enumerate(lst, 1):
        x["rank"] = i
        x["pct"] = round(100 * x["sum"] / net, 2) if net else 0.0
        cum += x["sum"]
        x["cum_pct"] = round(100 * cum / net, 1) if net else 0.0
        x["abc"] = "A" if x["cum_pct"] <= 80 else ("B" if x["cum_pct"] <= 95 else "C")
        x["avg"] = round(x["sum"] / x["n"], 2) if x["n"] else 0.0
        x["months"] = dict(x["months"])
    n = len(lst)
    top10 = round(sum(x["sum"] for x in lst[:10]), 2)
    abc = {k: {"n": sum(1 for x in lst if x["abc"] == k), "sum": round(sum(x["sum"] for x in lst if x["abc"] == k), 2)} for k in "ABC"}
    shown = lst
    if q:
        qk = _key(q)
        shown = [x for x in lst if qk in _key(x["name"])]
    # помісячно за період — для графіка
    bym: dict[str, float] = defaultdict(float)
    for x in lst:
        for m, v in x["months"].items():
            bym[m] += v
    months = sorted(bym.items())
    return {"year": year, "month": month, "label": label, "d_from": d_from, "d_to": d_to, "rows": shown, "all_n": n,
            "total": net, "gross": round(total, 2), "refunds": round(refunds, 2), "patients": n,
            "avg": round(net / n, 2) if n else 0.0, "median": _median([x["sum"] for x in lst]),
            "top10": top10, "top10_pct": round(100 * top10 / net, 1) if net else 0.0, "abc": abc, "skipped": skipped,
            "months": months, "months_max": max([v for _, v in months] + [1]), "q": q, "n_ops": len(rows)}


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return round(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2, 2)


def to_xlsx(r: dict) -> bytes:
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Отримано від пацієнтів"
    ws.append([f"Отримано грошей від пацієнтів · {r['label']} ({r['d_from']:%d.%m.%Y} — {r['d_to']:%d.%m.%Y})"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["№", "Пацієнт", "Отримано, ₴", "% від загальної суми", "Накопичено, %", "ABC", "Оплат", "Середня оплата", "Повернення", "Перша оплата", "Остання оплата"])
    for c in ws[2]:
        c.font = Font(bold=True)
    for x in r["rows"]:
        ws.append([x["rank"], x["name"], round(x["sum"], 2), x["pct"], x["cum_pct"], x["abc"], x["n"], x["avg"], round(x["refund"], 2) or None,
                   x["first"].strftime("%d.%m.%Y") if x["first"] else "", x["last"].strftime("%d.%m.%Y") if x["last"] else ""])
    ws.append([])
    ws.append(["Разом", f"{r['patients']} пацієнтів", r["total"], 100])
    ws.column_dimensions["B"].width = 40
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
