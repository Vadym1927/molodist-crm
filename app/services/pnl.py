"""Звіт «Фінансовий аналіз доходів і витрат за рік» (P&L за статтями).

Джерело — журнал фіноперацій CRM (finops.journal): операції CleverBox з 2022 року (CbFinOp), чеки й каса CRM, операції,
внесені в CRM. Не рахуються: переміщення між рахунками, оплати не грошима (бонуси, сертифікати, баланс),
статті «не в дохід» (Налаштування → Звіти). Сторно зменшує свою статтю.

Результат: помісячно за статтями доходів і витрат, підсумки, порівняння з попереднім роком, структура витрат,
групи витрат (зарплата, закупівлі, оренда й комуналка, маркетинг, податки, вилучення власника, інше)."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy.orm import Session

MONTHS = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"]
MONTHS_FULL = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень", "Жовтень",
               "Листопад", "Грудень"]
FIRST_YEAR = 2022

# групи витрат за ключовими словами в назві статті (порядок важливий — перша, що збіглась)
GROUPS = [
    ("salary", "Зарплата й персонал", r"зарплат|з/п|зп\b|оклад|премі|аванс|персонал|бонус співроб|відпуск|лікарнян|навчанн"),
    ("goods", "Закупівлі: препарати, товари, розхідники", r"закуп|постач|товар|препарат|розхідн|одноразов|матеріал|косметик|накладн"),
    ("rent", "Оренда й комунальні", r"оренд|комунал|електро|світло|вода|опален|газ\b|інтернет|прибиранн|охорон"),
    ("marketing", "Маркетинг і реклама", r"реклам|маркет|таргет|google|instagram|facebook|smm|блогер|контент|сайт|лендінг|tilda"),
    ("tax", "Податки, банк, бухгалтерія", r"податк|єсв|єдиний|комісі|банк|еквайр|бухгалт|юрид|штраф|держ"),
    ("owner", "Вилучення власника / дивіденди", r"дивіденд|девіденд|власник|вилученн|особист"),
    ("equip", "Обладнання, ремонт, амортизація", r"обладн|апарат|ремонт|амортиз|меблі|техніка|інвест"),
    ("services", "Сервіси й підписки", r"сервіс|підписк|crm|програм|софт|телефон|зв'язок|звʼязок|хостинг"),
    ("refund", "Повернення пацієнтам", r"поверненн"),
]
OTHER = ("other", "Інші витрати")


def group_of(article: str) -> tuple[str, str]:
    a = (article or "").lower()
    for key, title, rx in GROUPS:
        if re.search(rx, a):
            return key, title
    return OTHER


def years(db: Session) -> list[int]:
    from sqlalchemy import select, func
    from ..models import CbFinOp, FinOp, Sale
    ys = set()
    for v in (db.scalar(select(func.min(CbFinOp.at))), db.scalar(select(func.min(FinOp.day))), db.scalar(select(func.min(Sale.at)))):
        if v:
            ys.add(v.year if isinstance(v, (date, datetime)) else int(str(v)[:4]))
    y0 = min(ys) if ys else date.today().year
    return list(range(max(y0, FIRST_YEAR), date.today().year + 1))


class Line:
    __slots__ = ("article", "m", "total", "kind", "group", "gtitle", "pct")

    def __init__(self, article: str, kind: str):
        self.article, self.kind = article, kind
        self.m = [0.0] * 12
        self.total = 0.0
        self.pct = 0.0
        self.group, self.gtitle = group_of(article) if kind == "expense" else ("income", "Доходи")

    def add(self, month: int, v: float):
        self.m[month - 1] += v
        self.total += v


def _collect(db: Session, year: int) -> dict:
    from . import finops as fo
    from . import clinic
    ex = clinic.excluded_income(db)
    rows = fo.journal(db, date(year, 1, 1), date(year, 12, 31))
    inc: dict[str, Line] = {}
    exp: dict[str, Line] = {}
    skipped = {"transfer": 0.0, "non_money": 0.0, "excluded": 0.0}
    for r in rows:
        if r.kind == "transfer":
            skipped["transfer"] += r.amount
            continue
        if not r.money:
            skipped["non_money"] += r.amount
            continue
        art = (r.article or "").strip() or "Без статті"
        if r.kind == "income" and art.lower() in ex:
            skipped["excluded"] += r.amount
            continue
        mo = r.at.month
        if r.kind == "income":
            inc.setdefault(art, Line(art, "income")).add(mo, r.amount)
        elif r.kind == "storno":                           # сторно: якщо є така стаття в доходах — мінус з доходу, інакше витрата
            if art in inc or art.lower() == "оплата послуг, товарів":
                inc.setdefault(art, Line(art, "income")).add(mo, -r.amount)
            else:
                exp.setdefault(art, Line(art, "expense")).add(mo, -r.amount)
        else:
            exp.setdefault(art, Line(art, "expense")).add(mo, r.amount)
    inc_l = sorted(inc.values(), key=lambda x: -x.total)
    exp_l = sorted(exp.values(), key=lambda x: -x.total)
    ti = [round(sum(l.m[i] for l in inc_l), 2) for i in range(12)]
    te = [round(sum(l.m[i] for l in exp_l), 2) for i in range(12)]
    last_m = 0
    for r in rows:
        last_m = max(last_m, r.at.month)
    return {"year": year, "income": inc_l, "expense": exp_l, "ti": ti, "te": te, "net": [round(a - b, 2) for a, b in zip(ti, te)],
            "sum_i": round(sum(ti), 2), "sum_e": round(sum(te), 2), "skipped": skipped, "n": len(rows),
            "months_with_data": [i + 1 for i in range(12) if ti[i] or te[i]], "last_month": last_m}


def build(db: Session, year: int) -> dict:
    cur = _collect(db, year)
    prev = _collect(db, year - 1) if year - 1 >= FIRST_YEAR else None
    cur["sum_net"] = round(cur["sum_i"] - cur["sum_e"], 2)
    cur["margin"] = round(100 * cur["sum_net"] / cur["sum_i"], 1) if cur["sum_i"] else None
    mwd = cur["months_with_data"] or [1]
    cur["avg_i"] = round(cur["sum_i"] / len(mwd), 2)
    cur["avg_e"] = round(cur["sum_e"] / len(mwd), 2)
    cur["avg_net"] = round(cur["sum_net"] / len(mwd), 2)
    full = [i for i in mwd if i < (date.today().month if year == date.today().year else 13)] or mwd
    cur["best"] = max(full, key=lambda i: cur["net"][i - 1]) if full else None
    cur["worst"] = min(full, key=lambda i: cur["net"][i - 1]) if full else None
    cur["neg_months"] = sum(1 for i in full if cur["net"][i - 1] < 0)
    # групи витрат
    g: dict[str, dict] = {}
    for l in cur["expense"]:
        d = g.setdefault(l.group, {"key": l.group, "title": l.gtitle, "total": 0.0, "m": [0.0] * 12, "lines": []})
        d["total"] += l.total
        d["lines"].append(l)
        for i in range(12):
            d["m"][i] += l.m[i]
    cur["groups"] = sorted(g.values(), key=lambda x: -x["total"])
    for d in cur["groups"]:
        d["pct"] = round(100 * d["total"] / cur["sum_e"], 1) if cur["sum_e"] else 0
    for l in cur["expense"]:
        l.pct = round(100 * l.total / cur["sum_e"], 1) if cur["sum_e"] else 0
    for l in cur["income"]:
        l.pct = round(100 * l.total / cur["sum_i"], 1) if cur["sum_i"] else 0
    # порівняння з минулим роком
    cur["prev"] = prev
    if prev:
        cur["yoy"] = {"i": _pct(cur["sum_i"], prev["sum_i"]), "e": _pct(cur["sum_e"], prev["sum_e"]),
                      "net": _pct(cur["sum_net"], prev["sum_i"] - prev["sum_e"]),
                      "same_i": round(sum(prev["ti"][i - 1] for i in mwd), 2), "same_e": round(sum(prev["te"][i - 1] for i in mwd), 2)}
        cur["yoy"]["same_net"] = round(cur["yoy"]["same_i"] - cur["yoy"]["same_e"], 2)
        cur["yoy"]["same_i_pct"] = _pct(cur["sum_i"], cur["yoy"]["same_i"])
        cur["yoy"]["same_e_pct"] = _pct(cur["sum_e"], cur["yoy"]["same_e"])
    cur["max"] = max(cur["ti"] + cur["te"] + ([1])) or 1
    cur["net_max"] = max([abs(x) for x in cur["net"]] + [1])
    return cur


def _pct(a: float, b: float):
    return round(100 * (a - b) / abs(b), 1) if b else None


def to_xlsx(r: dict) -> bytes:
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = f"P&L {r['year']}"
    ws.append([f"Фінансовий аналіз доходів і витрат · {r['year']}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["Стаття"] + MONTHS + ["Разом", "%"])
    ws.append(["ДОХОДИ"])
    for l in r["income"]:
        ws.append([l.article] + [round(x, 2) for x in l.m] + [round(l.total, 2), l.pct])
    ws.append(["Разом доходи"] + r["ti"] + [r["sum_i"], 100])
    ws.append([])
    ws.append(["ВИТРАТИ"])
    for gd in r["groups"]:
        ws.append([gd["title"]] + [round(x, 2) for x in gd["m"]] + [round(gd["total"], 2), gd["pct"]])
        for l in gd["lines"]:
            ws.append(["   " + l.article] + [round(x, 2) for x in l.m] + [round(l.total, 2), l.pct])
    ws.append(["Разом витрати"] + r["te"] + [r["sum_e"], 100])
    ws.append([])
    ws.append(["Чистий результат"] + r["net"] + [r["sum_net"], r["margin"]])
    for row in ws.iter_rows(min_row=2, max_row=2):
        for c in row:
            c.font = Font(bold=True)
    ws.column_dimensions["A"].width = 44
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
