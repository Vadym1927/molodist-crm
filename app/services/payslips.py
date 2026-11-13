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


def build(db: Session, month: str) -> dict:
    from . import accrual_articles as aa, finops as fo
    from .fin_articles import salary_names
    kinds = {x["name"].strip().lower(): x["kind"] for x in aa.items(db)}
    rows_in = fo.settlements(db, month)
    a, b = fo.month_bounds(month)
    pays = defaultdict(list)
    for x in db.scalars(select(FinOp).where(FinOp.deleted_at.is_(None), FinOp.category.in_(salary_names(db)), FinOp.doctor_id.is_not(None))
                        .order_by(FinOp.day, FinOp.id)):
        if (x.salary_month or fo.month_key(x.day)) == month:
            pays[x.doctor_id].append(x)
    rows = []
    for r in rows_in:
        m = r["m"]
        parts = {"bonus": [], "tax": [], "hold": []}
        for adj in m.get("adjs") or []:
            parts[_kind(adj, kinds)].append(adj)
        s = {k: round(sum(x.amount for x in v), 2) for k, v in parts.items()}
        base = round(float(m.get("base") or 0), 2)
        comm = round(float(m.get("from_services") or 0) + float(m.get("from_products") or 0), 2)
        gross = round(base + comm + s["bonus"], 2)
        net = round(gross + s["tax"] + s["hold"], 2)              # = нараховано до виплати (m["accrued"])
        ps = pays.get(r["doctor"].id, [])
        paid = round(sum(x.amount if x.kind == "expense" else -x.amount for x in ps), 2)
        row = {"doctor": r["doctor"], "plan": (r.get("plan") or {}).get("name") if isinstance(r.get("plan"), dict) else None,
               "days": m.get("days"), "services": m.get("services") or 0, "products": m.get("products") or 0,
               "base": base, "comm": comm, "from_services": round(float(m.get("from_services") or 0), 2),
               "from_products": round(float(m.get("from_products") or 0), 2),
               "bonus": s["bonus"], "gross": gross, "tax": s["tax"], "hold": s["hold"], "net": net,
               "paid": paid, "opening": r["opening"], "closing": round(r["opening"] + net - paid, 2),
               "adjs": parts, "pays": ps}
        row["empty"] = not any(abs(row[k]) > 0.004 for k in ("gross", "net", "paid", "opening", "closing"))
        rows.append(row)
    rows.sort(key=lambda x: (x["empty"], -x["gross"], x["doctor"].name))
    keys = ("base", "comm", "bonus", "gross", "tax", "hold", "net", "paid", "opening", "closing")
    return {"month": month, "d_from": a, "d_to": b, "rows": rows, "total": {k: round(sum(x[k] for x in rows), 2) for k in keys}}


def to_xlsx(v: dict) -> bytes:
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = f"ЗП {v['month']}"
    head = ["Співробітник", "Посада", "Ставка / оклад", "Комісійні", "Премії й доплати", "Нараховано (брутто)", "Податки", "Утримання",
            "До виплати", "Виплачено на руки", "Борг на початок", "Залишок до виплати"]
    ws.append([f"Розрахункові листи за {v['month']}"])
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
