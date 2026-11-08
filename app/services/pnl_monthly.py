"""Звіт «Прибутки й збитки (P&L) по місяцях» (/reports?r=pnlm) — управлінський, «від виручки до чистого прибутку»:

  Виручка (послуги + товари за чеками)
  − Собівартість (матеріали послуг за техкартами / нормами, закупівельна вартість проданих товарів)
  = Валовий прибуток · маржа %
  − Зарплата (ФОП: виплати з журналу за статтями зарплати)
  = Прибуток після ЗП
  − Операційні витрати за групами (оренда й комунальні, маркетинг, сервіси, банк і податки, інше) — без закупівель
    (вони вже в собівартості), без обладнання/капвкладень і без вилучень власника
  − Амортизація обладнання (нарахування з модуля «Амортизація»)
  = Операційний прибуток · рентабельність %
  Довідково нижче: закупівлі (касові), обладнання / капвкладення, вилучення власника, оплати не грошима.

Порівняння з тим самим місяцем минулого року. Касовий P&L за статтями — окремий звіт «Доходи і витрати за рік»."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .pnl import FIRST_YEAR, MONTHS, MONTHS_FULL, group_of
from .patient_money import years  # noqa: F401

OPEX_GROUPS = [("rent", "Оренда й комунальні"), ("marketing", "Маркетинг і реклама"), ("services", "Сервіси й підписки"),
               ("tax", "Податки, банк, бухгалтерія"), ("refund", "Повернення пацієнтам"), ("other", "Інші витрати")]
BELOW = [("goods", "Закупівлі (касові)"), ("equip", "Обладнання, ремонт, капвкладення"), ("owner", "Вилучення власника / дивіденди")]


def _m(d: date | datetime) -> str:
    return d.strftime("%Y-%m")


def _collect(db: Session, year: int) -> dict:
    from . import reports as rp, finops as fo, clinic
    from .payroll_share import is_salary
    from .fin_articles import salary_names as _sn
    from .cb_services_import import service_index
    from .catalog import product_rows
    from ..parsers.observ import norm
    from ..models import AmortPeriod
    d_from, d_to = date(year, 1, 1), date(year, 12, 31)
    today = date.today()
    months = [f"{year}-{i:02d}" for i in range(1, 13) if year < today.year or i <= today.month]
    z = lambda: {m: 0.0 for m in months}  # noqa: E731
    rev_s, rev_p, cogs_s, cogs_p = z(), z(), z(), z()
    # --- виручка й собівартість товарів (чеки) ---
    cat = {norm(pr.product.name): pr for pr in product_rows(db, [])}
    for c in rp._checks(db, d_from, d_to):
        mk = _m(c.date)
        if mk not in rev_s:
            continue
        for kind, name, q, amt, _st in rp._items(c):
            if kind == "service":
                rev_s[mk] += amt
            elif kind == "product":
                rev_p[mk] += amt
                pr = cat.get(norm(name))
                if pr and pr.cost:
                    cogs_p[mk] += float(pr.cost) * float(q or 1)
    # --- собівартість послуг (техкарти / норми / фактичні списання каси CRM) ---
    idx = service_index(db)
    unit_cost = rp._norm_costs(db)
    for ln in rp._service_lines(db, d_from, d_to):
        mk = _m(ln["day"])
        if mk not in cogs_s:
            continue
        if ln.get("cost_actual") is not None:
            cogs_s[mk] += float(ln["cost_actual"])
        else:
            sv = idx.get(norm(ln["name"]))
            cogs_s[mk] += ((unit_cost.get(sv.id, 0.0) or float(sv.cost_fixed or 0)) if sv else 0.0) * float(ln["qty"] or 1)
    # --- витрати з журналу: ЗП, опекс за групами, довідкові ---
    ex = clinic.excluded_income(db)
    sal_names = set(_sn(db))
    salary, amort_paid = z(), z()
    opex = {k: z() for k, _ in OPEX_GROUPS}
    below = {k: z() for k, _ in BELOW}
    non_money = z()
    journal_income = z()
    for r in fo.journal(db, d_from, d_to):
        mk = _m(r.at)
        if mk not in salary or r.kind == "transfer":
            continue
        if r.kind == "income":
            if r.money and (r.article or "").strip().lower() not in ex:
                journal_income[mk] += r.amount
            elif not r.money:
                non_money[mk] += r.amount
            continue
        amt = r.amount if r.kind == "expense" else -r.amount
        if not r.money:
            continue
        if is_salary(r.article, sal_names):
            salary[mk] += amt
            continue
        g, _t = group_of(r.article or "")
        if g == "salary":
            salary[mk] += amt
        elif g in below:
            below[g][mk] += amt
        elif g in opex:
            opex[g][mk] += amt
        else:
            opex["other"][mk] += amt
    # --- амортизація: нарахування ---
    amort = z()
    for p in db.scalars(select(AmortPeriod).where(AmortPeriod.year == year)):
        mk = f"{year}-{p.month:02d}"
        if mk in amort:
            amort[mk] += float(p.amount or 0)
    rows = []
    for mk in months:
        rev = rev_s[mk] + rev_p[mk]
        from_journal = False
        if not rev and journal_income[mk]:            # чеків за місяць немає (історія до синку чеків) — виручка з журналу надходжень
            rev, from_journal = journal_income[mk], True
        cogs = cogs_s[mk] + cogs_p[mk]
        gross = rev - cogs
        after = gross - salary[mk]
        ox = sum(opex[k][mk] for k, _ in OPEX_GROUPS)
        op = after - ox - amort[mk]
        rows.append({"m": mk, "label": MONTHS[int(mk[5:]) - 1], "full": MONTHS_FULL[int(mk[5:]) - 1], "rev": rev, "rev_s": rev_s[mk], "rev_p": rev_p[mk],
                     "cogs": cogs, "cogs_s": cogs_s[mk], "cogs_p": cogs_p[mk], "gross": gross, "gross_pct": _pct(gross, rev),
                     "salary": salary[mk], "salary_pct": _pct(salary[mk], rev), "after": after, "opex": ox, "opex_g": {k: opex[k][mk] for k, _ in OPEX_GROUPS},
                     "amort": amort[mk], "op": op, "op_pct": _pct(op, rev), "below": {k: below[k][mk] for k, _ in BELOW},
                     "non_money": non_money[mk], "j_income": journal_income[mk],
                     "future": mk > _m(today), "current": mk == _m(today), "from_journal": from_journal})
    return {"year": year, "months": months, "rows": rows}


def _pct(a: float, b: float):
    return round(100 * a / b, 1) if b else None


def _sum(rows: list[dict], key: str, sub: str | None = None) -> float:
    return round(sum((r[key][sub] if sub else r[key]) for r in rows), 2)


def build(db: Session, year: int) -> dict:
    cur = _collect(db, year)
    prev = _collect(db, year - 1) if year - 1 >= FIRST_YEAR else None
    rows = cur["rows"]
    done = [r for r in rows if r["rev"] or r["salary"] or r["opex"]]
    t = {k: _sum(rows, k) for k in ("rev", "rev_s", "rev_p", "cogs", "cogs_s", "cogs_p", "gross", "salary", "after", "opex", "amort", "op", "non_money", "j_income")}
    t["opex_g"] = {k: _sum(rows, "opex_g", k) for k, _ in OPEX_GROUPS}
    t["below"] = {k: _sum(rows, "below", k) for k, _ in BELOW}
    t["gross_pct"], t["salary_pct"], t["op_pct"] = _pct(t["gross"], t["rev"]), _pct(t["salary"], t["rev"]), _pct(t["op"], t["rev"])
    n = len(done) or 1
    avg = {k: round(t[k] / n, 2) for k in ("rev", "gross", "salary", "opex", "op")}
    pm = {r["m"][5:]: r for r in prev["rows"]} if prev else {}
    for r in rows:
        p = pm.get(r["m"][5:])
        r["prev"] = p
        r["yoy_rev"] = _pct(r["rev"] - p["rev"], p["rev"]) if p and p["rev"] else None
        r["yoy_op"] = _pct(r["op"] - p["op"], abs(p["op"])) if p and p["op"] else None
    pt = None
    if prev:
        same = [x for x in prev["rows"] if x["m"][5:] in {r["m"][5:] for r in done}]
        pt = {"rev": _sum(same, "rev"), "op": _sum(same, "op"), "gross": _sum(same, "gross"), "salary": _sum(same, "salary"),
              "full_rev": _sum(prev["rows"], "rev"), "full_op": _sum(prev["rows"], "op")}
        pt["yoy_rev"] = _pct(t["rev"] - pt["rev"], pt["rev"]) if pt["rev"] else None
        pt["yoy_op"] = _pct(t["op"] - pt["op"], abs(pt["op"])) if pt["op"] else None
    pos = [r for r in done if r["op"] > 0]
    best = max(done, key=lambda r: r["op"]) if done else None
    worst = min(done, key=lambda r: r["op"]) if done else None
    # покриття даними: собівартість і амортизація є лише з моменту ведення техкарт / складу
    cov = {"cogs_months": sum(1 for r in rows if r["cogs"]), "amort_months": sum(1 for r in rows if r["amort"]), "n": len(done),
           "rev_vs_journal": round(t["rev"] - t["j_income"], 2), "journal_months": sum(1 for r in rows if r["from_journal"])}
    return {"year": year, "rows": rows, "t": t, "avg": avg, "prev": prev, "pt": pt, "best": best, "worst": worst, "pos": len(pos),
            "neg": len(done) - len(pos), "cov": cov, "OPEX": OPEX_GROUPS, "BELOW": BELOW, "MONTHS": MONTHS,
            "max_rev": max([r["rev"] for r in rows] + [1]), "max_abs_op": max([abs(r["op"]) for r in rows] + [1]),
            "has_data": bool(done)}


def to_xlsx(r: dict) -> bytes:
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = f"P&L {r['year']}"
    ws.append([f"Прибутки й збитки по місяцях · {r['year']}"])
    ws["A1"].font = Font(bold=True, size=13)
    hdr = ["Показник"] + [x["label"] for x in r["rows"]] + ["Разом"]
    ws.append(hdr)
    for c in ws[2]:
        c.font = Font(bold=True)

    def line(title, key, sub=None, pct=False, bold=False):
        vals = [(x[key][sub] if sub else x[key]) for x in r["rows"]]
        tot = (r["t"][key][sub] if sub else r["t"][key])
        ws.append([title] + [round(v, 2) if v is not None else None for v in vals] + [round(tot, 2) if tot is not None else None])
        if bold:
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
    line("Виручка", "rev", bold=True)
    line("  послуги", "rev_s")
    line("  товари", "rev_p")
    line("Собівартість", "cogs")
    line("  матеріали послуг", "cogs_s")
    line("  закупівельна вартість товарів", "cogs_p")
    line("Валовий прибуток", "gross", bold=True)
    line("  валова маржа, %", "gross_pct")
    line("Зарплата (ФОП)", "salary")
    line("  % ФОП від виручки", "salary_pct")
    line("Прибуток після ЗП", "after", bold=True)
    for k, title in OPEX_GROUPS:
        line("  " + title, "opex_g", k)
    line("Операційні витрати разом", "opex")
    line("Амортизація обладнання (нарахування)", "amort")
    line("Операційний прибуток", "op", bold=True)
    line("  рентабельність, %", "op_pct")
    ws.append([])
    ws.append(["Довідково (не в прибутку)"])
    for k, title in BELOW:
        line("  " + title, "below", k)
    line("  оплати не грошима (бонуси, сертифікати, баланс)", "non_money")
    ws.column_dimensions["A"].width = 44
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
