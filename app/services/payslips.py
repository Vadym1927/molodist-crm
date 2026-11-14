"""Розрахункові листи (зведена відомість ЗП за місяць): по кожному співробітнику —
ставка, комісійні, премії й доплати → нараховано (брутто) → податки → утримання → до виплати → виплачено на руки → залишок.

Джерела: розрахунок за окладами (finops.payroll), коригування SalaryAdj (статті «Налаштування → Статті для нарахувань»:
дохід — премії й доплати, витрата — податки чи утримання), виплати — фіноперації зі статтею зарплати (finops.paid_salary).
Податки — коригування зі статтею, у назві якої «податк», «ПДФО», «ЄСВ» чи «військов»; решта мінусових — утримання."""
from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FinOp

TAX_RE = re.compile(r"податк|пдфо|єсв|есв|військов|збір", re.I)


def _kind(a, kinds: dict[str, str]) -> str:
    """bonus | tax | hold — для одного коригування."""
    art = (a.article or "").strip()
    if art and TAX_RE.search(art):
        return "tax"
    k = kinds.get(art.lower())
    if k == "income" or (not k and a.amount > 0):
        return "bonus"
    return "hold"


HALVES = {0: "весь місяць", 1: "1–15", 2: "16–кінець місяця"}


def _first_half(db: Session, month: str, rows_in: list[dict]) -> dict[int, dict]:
    """Нарахування за 1–15 число тими самими правилами, що й за місяць (оклад «за місяць» — навпіл).
    Друга половина = місяць − перша, тож дві половини завжди сходяться з місячним листом."""
    from datetime import timedelta
    from . import finops as fo
    from .schedule import shifts as _shifts
    from . import salary_plans as spl
    a, _ = fo.month_bounds(month)
    b = a + timedelta(days=14)
    doctors = [r["doctor"] for r in rows_in]
    sales = fo._staff_sales(db, a, b, doctors)
    lines, clinic, sh = ({}, {}, {})
    if any(r.get("plan") for r in rows_in):
        lines, clinic = fo._staff_lines(db, a, b, doctors)
        sh = _shifts(db, a, b)
    out = {}
    for r in rows_in:
        d, pl, rule = r["doctor"], r.get("plan"), r.get("rule")
        s = sales.get((month, d.id), {"services": 0.0, "products": 0.0})
        if pl:
            days = {dd for (did, dd) in sh if did == d.id}
            hours = sum(x.minutes for (did, dd), x in sh.items() if did == d.id) / 60
            res = spl.accrue(pl, lines.get((month, d.id), []), days, hours, clinic.get(month, []))
            fx = pl.get("fixed") or {}
            base = res["fixed"] / 2 if fx.get("on") and fx.get("period") == "month" else res["fixed"]
            out[d.id] = {"base": base, "from_services": res["comm_services"],
                         "from_products": res["comm"] - res["comm_services"] + res["turn"], "days": len(days)}
        else:
            out[d.id] = {"base": ((rule.base or 0) if rule else 0) / 2,
                         "from_services": s["services"] * ((rule.pct_services or 0) if rule else 0) / 100,
                         "from_products": s["products"] * ((rule.pct_products or 0) if rule else 0) / 100, "days": None}
        out[d.id].update(services=s["services"], products=s["products"])
    return out


def build(db: Session, month: str, half: int = 0) -> dict:
    """half: 0 — місяць, 1 — 1–15 число, 2 — 16–кінець. Коригування потрапляють у половину за датою внесення, виплати — за датою."""
    from datetime import timedelta, timezone
    from zoneinfo import ZoneInfo
    from ..config import settings
    from . import accrual_articles as aa, finops as fo
    from .fin_articles import salary_names
    half = half if half in (1, 2) else 0
    kinds = {x["name"].strip().lower(): x["kind"] for x in aa.items(db)}
    rows_in = fo.settlements(db, month)
    a, b = fo.month_bounds(month)
    mid = a + timedelta(days=14)
    h1 = _first_half(db, month, rows_in) if half else {}
    z = ZoneInfo(settings.timezone)

    def adj_half(x) -> int:
        d = x.created_at.replace(tzinfo=timezone.utc).astimezone(z).date() if x.created_at else b
        return 1 if d <= mid else 2

    pays = defaultdict(list)
    for x in db.scalars(select(FinOp).where(FinOp.deleted_at.is_(None), FinOp.category.in_(salary_names(db)), FinOp.doctor_id.is_not(None))
                        .order_by(FinOp.day, FinOp.id)):
        if (x.salary_month or fo.month_key(x.day)) == month:
            pays[x.doctor_id].append(x)

    def signed(ps):
        return round(sum(x.amount if x.kind == "expense" else -x.amount for x in ps), 2)

    rows = []
    for r in rows_in:
        m = r["m"]
        did = r["doctor"].id
        vals = {k: float(m.get(k) or 0) for k in ("base", "from_services", "from_products", "services", "products")}
        days, adjs, ps, opening = m.get("days"), list(m.get("adjs") or []), pays.get(did, []), r["opening"]
        if half:
            f = h1[did]
            ps1 = [x for x in ps if x.day <= mid]
            adj1 = [x for x in adjs if adj_half(x) == 1]
            if half == 1:
                vals = {k: float(f[k]) for k in vals}
                days, adjs, ps = f["days"], adj1, ps1
            else:
                net1 = f["base"] + f["from_services"] + f["from_products"] + sum(x.amount for x in adj1)
                opening = round(opening + net1 - signed(ps1), 2)             # залишок після першої половини
                vals = {k: vals[k] - float(f[k]) for k in vals}
                days = (days - f["days"]) if days is not None and f["days"] is not None else days
                adjs, ps = [x for x in adjs if adj_half(x) == 2], [x for x in ps if x.day > mid]
        parts = {"bonus": [], "tax": [], "hold": []}
        for adj in adjs:
            parts[_kind(adj, kinds)].append(adj)
        s = {k: round(sum(x.amount for x in v), 2) for k, v in parts.items()}
        base = round(vals["base"], 2)
        comm = round(vals["from_services"] + vals["from_products"], 2)
        gross = round(base + comm + s["bonus"], 2)
        net = round(gross + s["tax"] + s["hold"], 2)              # = нараховано до виплати (m["accrued"])
        paid = signed(ps)
        row = {"doctor": r["doctor"], "plan": (r.get("plan") or {}).get("name") if isinstance(r.get("plan"), dict) else None,
               "days": days, "services": round(vals["services"], 2), "products": round(vals["products"], 2),
               "base": base, "comm": comm, "from_services": round(vals["from_services"], 2),
               "from_products": round(vals["from_products"], 2),
               "bonus": s["bonus"], "gross": gross, "tax": s["tax"], "hold": s["hold"], "net": net,
               "paid": paid, "opening": opening, "closing": round(opening + net - paid, 2),
               "adjs": parts, "pays": ps}
        row["empty"] = not any(abs(row[k]) > 0.004 for k in ("gross", "net", "paid", "opening", "closing"))
        rows.append(row)
    rows.sort(key=lambda x: (x["empty"], -x["gross"], x["doctor"].name))
    keys = ("base", "comm", "bonus", "gross", "tax", "hold", "net", "paid", "opening", "closing")
    d_from, d_to = (a, mid) if half == 1 else ((mid + timedelta(days=1), b) if half == 2 else (a, b))
    return {"month": month, "half": half, "d_from": d_from, "d_to": d_to,
            "label": f"{d_from:%d.%m.%Y} – {d_to:%d.%m.%Y}", "rows": rows, "total": {k: round(sum(x[k] for x in rows), 2) for k in keys}}


def to_xlsx(v: dict) -> bytes:
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = f"ЗП {v['month']}"
    head = ["Співробітник", "Посада", "Ставка / оклад", "Комісійні", "Премії й доплати", "Нараховано (брутто)", "Податки", "Утримання",
            "До виплати", "Виплачено на руки", "Борг на початок", "Залишок до виплати"]
    ws.append([f"Розрахункові листи за {v.get('label') or v['month']}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(head)
    for c in ws[2]:
        c.font, c.fill, c.alignment = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="3B3050"), Alignment(wrap_text=True, vertical="center")
    for r in v["rows"]:
        if r["empty"]:
            continue
        ws.append([r["doctor"].name, r["doctor"].position or "", r["base"], r["comm"], r["bonus"], r["gross"], r["tax"], r["hold"], r["net"],
                   r["paid"], r["opening"], r["closing"]])
    t = v["total"]
    ws.append(["Разом", "", t["base"], t["comm"], t["bonus"], t["gross"], t["tax"], t["hold"], t["net"], t["paid"], t["opening"], t["closing"]])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=3, min_col=3):
        for c in row:
            c.number_format = '# ##0.00'
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 30, 26
    for col in "CDEFGHIJKL":
        ws.column_dimensions[col].width = 15
    ws.row_dimensions[2].height = 32
    ws.freeze_panes = "C3"
    d = wb.create_sheet("Деталі")
    d.append(["Співробітник", "Що", "Стаття / каса", "Коментар", "Сума"])
    for c in d[1]:
        c.font = Font(bold=True)
    names = {"bonus": "Премія / доплата", "tax": "Податок", "hold": "Утримання"}
    for r in v["rows"]:
        for k in ("bonus", "tax", "hold"):
            for x in r["adjs"][k]:
                d.append([r["doctor"].name, names[k], x.article or "", x.comment or "", x.amount])
        for p in r["pays"]:
            d.append([r["doctor"].name, f"Виплата {p.day:%d.%m.%Y}", p.cashbox or "", p.comment or "", p.amount if p.kind == "expense" else -p.amount])
    d.column_dimensions["A"].width, d.column_dimensions["B"].width, d.column_dimensions["C"].width, d.column_dimensions["D"].width = 30, 20, 22, 40
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
