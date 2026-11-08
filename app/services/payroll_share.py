"""Звіт «Фонд оплати праці» (/reports?r=payroll_share): зарплата як частка виручки.

По місяцях року: виручка (чеки CleverBox + каса CRM), виплачена зарплата з журналу фіноперацій (статті зарплати CRM +
статті CleverBox «зарплата / аванс / премія» з 2022), % ФОП; окремо лікарі / адміністратори / інші.
По співробітниках: скільки виплачено (і нараховано в CRM, якщо є), скільки виручки він приніс (послуги + товари за
ініціатором у чеках), % ЗП від його виручки, виручка на 1 ₴ зарплати."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .pnl import FIRST_YEAR, MONTHS, MONTHS_FULL
from .patient_money import period, years  # noqa: F401

SALARY_RE = re.compile(r"зарплат|з/п|\bзп\b|оклад|аванс|премі|відпускн|лікарнян|бонус (співроб|лікар|адмін)|персонал", re.I)
NOT_SALARY_RE = re.compile(r"податк|єсв|податок|дивіденд|власник", re.I)
ADMIN_RE = re.compile(r"адмін|administr|менеджер|рецепц|reception|касир|маркет|smm|бухгалт|прибир|клінінг", re.I)


def is_salary(article: str, salary_names: set[str]) -> bool:
    a = (article or "").strip()
    if a in salary_names:
        return True
    return bool(SALARY_RE.search(a)) and not NOT_SALARY_RE.search(a)


def role_of(d) -> str:
    pos = (getattr(d, "position", "") or "")
    return "admin" if ADMIN_RE.search(pos) else "doctor"


ROLES = {"doctor": "Лікарі", "admin": "Адміністратори та інші співробітники", "other": "Не зіставлено зі співробітником"}


def build(db: Session, year: int | None, month: int | None = None) -> dict:
    from . import finops as fo, reports as rp
    from .fin_articles import salary_names as _sn
    from ..integrations.cleverbox import match_doctor
    from ..models import Doctor
    d_from, d_to, label = period(year, month)
    doctors = db.scalars(select(Doctor).order_by(Doctor.name)).all()
    dmap = {d.id: d for d in doctors}
    sal_names = set(_sn(db))
    months = [f"{year}-{i:02d}" for i in range(1, 13) if year and (year < date.today().year or i <= date.today().month)] if year and not month \
        else ([f"{year}-{month:02d}"] if year else [])
    # --- виручка по місяцях і співробітниках (чеки) ---
    rev_m: dict[str, float] = defaultdict(float)
    rev_e: dict[int, dict] = defaultdict(lambda: {"services": 0.0, "products": 0.0, "m": defaultdict(float)})
    rev_unassigned = 0.0
    cache: dict[str, int | None] = {}
    for c in rp._checks(db, d_from, d_to):
        mk = c.date.strftime("%Y-%m")
        for kind, _n, _q, amt, staff in rp._items(c):
            if kind not in ("service", "product"):
                continue
            rev_m[mk] += amt
            if staff not in cache:
                d = match_doctor(staff, doctors)
                cache[staff] = d.id if d else None
            did = cache[staff]
            if did is None:
                rev_unassigned += amt
                continue
            e = rev_e[did]
            e["services" if kind == "service" else "products"] += amt
            e["m"][mk] += amt
    # --- зарплата з журналу (виплачено) ---
    pay_m: dict[str, dict] = defaultdict(lambda: {"doctor": 0.0, "admin": 0.0, "other": 0.0})
    pay_e: dict[int, dict] = defaultdict(lambda: {"paid": 0.0, "m": defaultdict(float), "n": 0})
    other_names: dict[str, float] = defaultdict(float)
    for r in fo.journal(db, d_from, d_to):
        if r.kind not in ("expense", "storno") or not r.money or not is_salary(r.article, sal_names):
            continue
        amt = r.amount if r.kind == "expense" else -r.amount
        mk = r.at.strftime("%Y-%m")
        did = None
        if r.src == "crm" and r.id:
            from ..models import FinOp
            op = db.get(FinOp, r.id)
            did = op.doctor_id if op else None
        if did is None and r.who:
            if r.who not in cache:
                d = match_doctor(r.who, doctors)
                cache[r.who] = d.id if d else None
            did = cache[r.who]
        if did is None:
            pay_m[mk]["other"] += amt
            other_names[r.who or r.article or "—"] += amt
            continue
        pay_m[mk][role_of(dmap[did])] += amt
        e = pay_e[did]
        e["paid"] += amt
        e["m"][mk] += amt
        e["n"] += 1
    # --- нараховано в CRM (якщо є правила / оклади) ---
    acc_e: dict[int, float] = defaultdict(float)
    acc_m: dict[str, float] = defaultdict(float)
    try:
        start = fo.payroll_start(db)
        for mk in months or sorted(set(rev_m) | set(pay_m)):
            if mk < start or mk > date.today().strftime("%Y-%m"):
                continue
            for row in fo.payroll(db, mk):
                v = float(row["m"]["accrued"] or 0)
                if v:
                    acc_e[row["doctor"].id] += v
                    acc_m[mk] += v
    except Exception:  # noqa: BLE001 — нарахування необовʼязкові для звіту
        pass
    all_m = months or sorted(set(rev_m) | set(pay_m))
    mrows = []
    for mk in all_m:
        p = pay_m.get(mk, {"doctor": 0.0, "admin": 0.0, "other": 0.0})
        fot = p["doctor"] + p["admin"] + p["other"]
        rev = rev_m.get(mk, 0.0)
        mrows.append({"m": mk, "label": MONTHS[int(mk[5:]) - 1], "rev": round(rev, 2), "fot": round(fot, 2), "doctor": round(p["doctor"], 2),
                      "admin": round(p["admin"], 2), "other": round(p["other"], 2), "accrued": round(acc_m.get(mk, 0.0), 2),
                      "pct": round(100 * fot / rev, 1) if rev else None, "pct_doc": round(100 * p["doctor"] / rev, 1) if rev else None,
                      "pct_adm": round(100 * p["admin"] / rev, 1) if rev else None})
    rev_t = round(sum(x["rev"] for x in mrows), 2)
    fot_t = round(sum(x["fot"] for x in mrows), 2)
    emps = []
    for did in set(rev_e) | set(pay_e) | set(acc_e):
        d = dmap.get(did)
        if not d:
            continue
        rv, pe = rev_e.get(did), pay_e.get(did)
        rev = round((rv["services"] + rv["products"]) if rv else 0.0, 2)
        paid = round(pe["paid"] if pe else 0.0, 2)
        acc = round(acc_e.get(did, 0.0), 2)
        if not rev and not paid and not acc:
            continue
        base = paid or acc
        emps.append({"id": did, "name": d.name, "role": role_of(d), "active": d.active, "position": d.position or "",
                     "rev": rev, "services": round(rv["services"], 2) if rv else 0.0, "products": round(rv["products"], 2) if rv else 0.0,
                     "paid": paid, "accrued": acc, "n": pe["n"] if pe else 0,
                     "pct": round(100 * base / rev, 1) if rev and base else None,
                     "per_uah": round(rev / base, 2) if base else None,
                     "rev_share": round(100 * rev / rev_t, 1) if rev_t else 0.0, "fot_share": round(100 * paid / fot_t, 1) if fot_t else 0.0,
                     "m_rev": dict(rv["m"]) if rv else {}, "m_paid": dict(pe["m"]) if pe else {}})
    emps.sort(key=lambda e: (-(e["paid"] or e["accrued"]), -e["rev"]))
    groups = []
    for key in ("doctor", "admin"):
        xs = [e for e in emps if e["role"] == key]
        if not xs:
            continue
        rv = round(sum(e["rev"] for e in xs), 2)
        pd = round(sum(e["paid"] for e in xs), 2)
        groups.append({"key": key, "title": ROLES[key], "rows": xs, "rev": rv, "paid": pd, "accrued": round(sum(e["accrued"] for e in xs), 2),
                       "pct": round(100 * pd / rv, 1) if rv else None, "fot_pct": round(100 * pd / fot_t, 1) if fot_t else 0.0})
    other_t = round(sum(x["other"] for x in mrows), 2)
    best = [x for x in mrows if x["pct"] is not None]
    return {"year": year, "month": month, "label": label, "d_from": d_from, "d_to": d_to, "months": mrows, "rev": rev_t, "fot": fot_t,
            "pct": round(100 * fot_t / rev_t, 1) if rev_t else None, "doctor": round(sum(x["doctor"] for x in mrows), 2),
            "admin": round(sum(x["admin"] for x in mrows), 2), "other": other_t, "accrued": round(sum(x["accrued"] for x in mrows), 2),
            "other_names": sorted(other_names.items(), key=lambda x: -x[1])[:15], "emps": emps, "groups": groups,
            "rev_unassigned": round(rev_unassigned, 2), "best": min(best, key=lambda x: x["pct"]) if best else None,
            "worst": max(best, key=lambda x: x["pct"]) if best else None, "max_rev": max([x["rev"] for x in mrows] + [1]),
            "avg_pct": round(sum(x["pct"] for x in best) / len(best), 1) if best else None, "MONTHS": MONTHS, "MONTHS_FULL": MONTHS_FULL,
            "first_year": FIRST_YEAR, "n_emps": len(emps)}


def to_xlsx(r: dict) -> bytes:
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "ФОП по місяцях"
    ws.append([f"Фонд оплати праці · {r['label']} ({r['d_from']:%d.%m.%Y} — {r['d_to']:%d.%m.%Y})"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["Місяць", "Виручка", "ФОП (виплачено)", "% ФОП", "Лікарі", "% лікарі", "Адміністратори та інші", "% адмін", "Не зіставлено", "Нараховано в CRM"])
    for c in ws[2]:
        c.font = Font(bold=True)
    for x in r["months"]:
        ws.append([x["m"], x["rev"], x["fot"], x["pct"], x["doctor"], x["pct_doc"], x["admin"], x["pct_adm"], x["other"], x["accrued"] or None])
    ws.append(["Разом", r["rev"], r["fot"], r["pct"], r["doctor"], None, r["admin"], None, r["other"], r["accrued"] or None])
    ws2 = wb.create_sheet("По співробітниках")
    ws2.append(["Співробітник", "Роль", "Посада", "Виручка (послуги+товари)", "Послуги", "Товари", "Виплачено ЗП", "Нараховано (CRM)", "ЗП % від виручки",
                "Виручка на 1 ₴ ЗП", "Частка виручки, %", "Частка ФОП, %", "Виплат"])
    for c in ws2[1]:
        c.font = Font(bold=True)
    for e in r["emps"]:
        ws2.append([e["name"], ROLES[e["role"]], e["position"], e["rev"], e["services"], e["products"], e["paid"], e["accrued"] or None, e["pct"], e["per_uah"],
                    e["rev_share"], e["fot_share"], e["n"]])
    ws.column_dimensions["A"].width = 14
    ws2.column_dimensions["A"].width = 34
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
