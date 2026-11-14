"""Звіти: виручка, лікарі, послуги, товари, пацієнти, бюті-плани, поставки.

Кожен звіт повертає Report — однакову структуру (плитки з порівнянням з попереднім періодом, колонки, рядки,
підсумок, міні-графік), яку малює один шаблон reports.html і вивантажує один Excel-експорт.
Джерела: чеки CleverBox (Check — фактична виручка), записи (Visit), бюті-плани, поставки (SupplierInvoice)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import re

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import Check, Visit, BeautyPlan, Patient, Product, SupplierInvoice
from ..parsers.observ import norm

REPORTS = {"cash": "Стан рахунків", "cashflow": "Cashflow", "pnl": "Доходи і витрати за рік", "pnlm": "P&L по місяцях", "shifts": "Касові зміни", "patient_money": "Отримано від пацієнтів", "fees": "Комісії банків", "payroll_share": "Фонд оплати праці", "fop": "Ліміти ФОП", "expiry": "Терміни придатності", "revenue": "Виручка", "doctors": "Лікарі", "services": "Звіт з продажу послуг", "products": "Товари",
           "patients": "Пацієнти", "plans": "Бюті-плани", "certificates": "Сертифікати й абонементи", "supply": "Поставки", "cosmetics": "Бонус за продаж косметики", "sources": "Джерела запису",
           "discounts": "Використання знижок", "cross": "Крос-продажі догляду", "ltv": "LTV пацієнтів", "src_sales": "Продажі за джерелами", "abc": "ABC-аналіз",
           "lost": "Утримання: хто не повернувся", "cohorts": "Когорти нових", "rfm": "Сегменти (RFM)", "bdays": "Дні народження", "mat_use": "Витрата матеріалів", "inventory": "Інвентаризації", "dead": "Неліквіди", "stock_value": "Вартість складу", "turnover": "Оборотність складу", "load": "Завантаження спеціалістів", "admin_kpi": "KPI адміністраторів", "tasks": "Звіт по задачах", "cancels": "Скасування й неявки"}


@dataclass
class Col:
    key: str
    label: str
    kind: str = "text"          # text | uah | int | num | pct | date
    total: bool = False         # підсумовувати в рядку «Разом»


@dataclass
class Report:
    key: str
    title: str
    hint: str = ""
    tiles: list = field(default_factory=list)       # (назва, значення, попереднє, формат, підпис)
    cols: list[Col] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    total: dict | None = None
    chart: list = field(default_factory=list)       # (підпис, значення)
    chart_title: str = ""
    empty: str = "За період немає даних."
    filters: list = field(default_factory=list)     # [{name, label, options [(value, label)], value}]
    grouped: bool = False                           # рядки-групи (_group) — без сортування кліком
    extra: dict = field(default_factory=dict)       # дані для окремого вигляду звіту (напр. бонус за косметику)

    def finish(self):
        rows = [r for r in self.rows if not r.get("_group")]
        if any(c.total for c in self.cols) and rows:
            self.total = {c.key: sum(r.get(c.key) or 0 for r in rows) for c in self.cols if c.total}
        return self


def _rng(d_from: date, d_to: date):
    return datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())


def prev_period(d_from: date, d_to: date) -> tuple[date, date]:
    """Попередній період тієї ж довжини (для місяця — попередній календарний місяць)."""
    if d_from.day == 1 and (d_to + timedelta(days=1)).day == 1:
        p_to = d_from - timedelta(days=1)
        months = (d_to.year - d_from.year) * 12 + d_to.month - d_from.month + 1
        y, m = p_to.year, p_to.month - months + 1
        while m < 1:
            y, m = y - 1, m + 12
        return date(y, m, 1), p_to
    n = (d_to - d_from).days + 1
    return d_from - timedelta(days=n), d_from - timedelta(days=1)


def _checks(db: Session, d_from: date, d_to: date):
    from .finops import revenue_checks
    a, b = _rng(d_from, d_to)
    return revenue_checks(db, a, b)


def _items(c: Check):
    for it in c.items or []:
        amt = float(it.get("payd_amount") or it.get("total") or 0)
        yield (it.get("item_type") or "other", (it.get("item_name") or it.get("name") or "—").strip(),
               float(it.get("quantity") or 1), amt, (it.get("initiator_name") or c.author or "—").strip())


def _money(checks):
    t = {"revenue": 0.0, "checks": 0, "services": 0.0, "products": 0.0, "other": 0.0, "discount": 0.0, "patients": set()}
    for c in checks:
        t["revenue"] += c.amount or 0
        t["discount"] += c.discount or 0
        t["checks"] += 1
        if c.patient_id:
            t["patients"].add(c.patient_id)
        for kind, _, _, amt, _ in _items(c):
            t["services" if kind == "service" else "products" if kind == "product" else "other"] += amt
    t["patients"] = len(t["patients"])
    t["avg"] = round(t["revenue"] / t["checks"]) if t["checks"] else 0
    return t


# ---------------- Виручка ----------------

def revenue(db: Session, d_from: date, d_to: date) -> Report:
    cur, prev = _checks(db, d_from, d_to), _checks(db, *prev_period(d_from, d_to))
    t, p = _money(cur), _money(prev)
    r = Report("revenue", "Виручка", "За чеками CleverBox. Порівняння — з попереднім періодом такої ж довжини.")
    r.tiles = [("Виручка", t["revenue"], p["revenue"], "uah", f"{t['checks']} чеків"),
               ("Середній чек", t["avg"], p["avg"], "uah", ""),
               ("Послуги", t["services"], p["services"], "uah", ""),
               ("Товари", t["products"], p["products"], "uah", ""),
               ("Пацієнтів", t["patients"], p["patients"], "int", "з оплатою")]
    by_month = (d_to - d_from).days > 62
    buckets: dict = defaultdict(list)
    for c in cur:
        buckets[(c.date.year, c.date.month) if by_month else c.date.date()].append(c)
    keys = []
    if by_month:
        y, m = d_from.year, d_from.month
        while (y, m) <= (d_to.year, d_to.month):
            keys.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        d = d_from
        while d <= d_to:
            keys.append(d)
            d += timedelta(days=1)
    r.cols = [Col("period", "Місяць" if by_month else "Дата"), Col("checks", "Чеків", "int", True),
              Col("patients", "Пацієнтів", "int"), Col("services", "Послуги", "uah", True),
              Col("products", "Товари", "uah", True), Col("other", "Інше", "uah", True),
              Col("discount", "Знижки", "uah", True), Col("revenue", "Виручка", "uah", True), Col("avg", "Сер. чек", "uah")]
    for k in keys:
        m = _money(buckets.get(k, []))
        label = f"{k[1]:02d}.{k[0]}" if by_month else k.strftime("%d.%m %a").replace("Mon", "пн").replace("Tue", "вт") \
            .replace("Wed", "ср").replace("Thu", "чт").replace("Fri", "пт").replace("Sat", "сб").replace("Sun", "нд")
        if m["checks"] or by_month:
            r.rows.append({"period": label, **{x: m[x] for x in ("checks", "patients", "services", "products", "other",
                                                                 "discount", "revenue", "avg")}})
        r.chart.append((f"{k[1]:02d}.{str(k[0])[2:]}" if by_month else k.strftime("%d"), m["revenue"]))
    r.chart_title = "Виручка по місяцях" if by_month else "Виручка по днях"
    r.finish()
    if r.total:
        r.total["avg"] = round(r.total["revenue"] / r.total["checks"]) if r.total.get("checks") else 0
        r.total["patients"] = t["patients"]
    return r


# ---------------- Лікарі ----------------

def doctors(db: Session, d_from: date, d_to: date) -> Report:
    a, b = _rng(d_from, d_to)
    agg = defaultdict(lambda: {"services": 0.0, "proc": 0.0, "products": 0.0, "patients": set(), "done": 0,
                               "booked": 0, "cancelled": 0, "plans": 0, "plan_sum": 0.0})
    for c in _checks(db, d_from, d_to):
        for kind, _, q, amt, staff in _items(c):
            x = agg[staff]
            if kind == "service":
                x["services"] += amt
                x["proc"] += q
            elif kind == "product":
                x["products"] += amt
            if c.patient_id and kind in ("service", "product"):
                x["patients"].add(c.patient_id)
    seen = defaultdict(set)          # візит = пацієнт × день у лікаря (надані послуги й записи CleverBox)
    from . import autonomy
    from ..models import Appointment, Doctor
    for v in db.scalars(select(Visit).where(Visit.starts_at >= a, Visit.starts_at <= autonomy.cb_until(db, b))):
        m = (v.master_name or "—").strip()
        if v.status == "cancelled":
            agg[m]["cancelled"] += 1
        elif v.status == "done" or (v.source != "cb_service_sale" and v.starts_at < datetime.now()):
            seen[m].add((v.patient_id, v.starts_at.date()))
        else:
            agg[m]["booked"] += 1
    s_ = autonomy.since(db)
    if s_:                                       # автономний режим: візити з дати переходу — записи CRM
        dn = {d.id: d.name for d in db.scalars(select(Doctor))}
        for ap in db.scalars(select(Appointment).where(Appointment.start >= max(a, datetime.combine(s_, datetime.min.time())),
                                                       Appointment.start <= b)):
            m = dn.get(ap.doctor_id, "—")
            if ap.status in ("cancelled", "no_show"):
                agg[m]["cancelled"] += 1
            elif ap.status in ("done", "paid") or ap.start < datetime.now():
                seen[m].add((ap.patient_id, ap.start.date()))
            else:
                agg[m]["booked"] += 1
    for m, days in seen.items():
        agg[m]["done"] = len(days)
    for pl in db.scalars(select(BeautyPlan).where(BeautyPlan.session_at >= a, BeautyPlan.session_at <= b,
                                                  BeautyPlan.status != "cancelled")):
        x = agg[pl.doctor.name if pl.doctor else "Без лікаря"]
        x["plans"] += 1
        x["plan_sum"] += pl.total_planned or sum(i.total for i in pl.items)
    r = Report("doctors", "Лікарі", "Послуги й товари — за ініціатором у чеках CleverBox; записи — за майстром у записі; "
               "бюті-плани — за датою консультації.")
    r.cols = [Col("name", "Лікар / спеціаліст"), Col("services", "Послуги", "uah", True), Col("proc", "Процедур", "num", True),
              Col("products", "Догляд", "uah", True), Col("total", "Разом", "uah", True), Col("patients", "Пацієнтів", "int"),
              Col("per_patient", "На пацієнта", "uah"), Col("done", "Візитів відбулось", "int", True),
              Col("cancelled", "Скасовано", "int", True), Col("cancel_pct", "% скасувань", "pct"),
              Col("plans", "Бюті-планів", "int", True), Col("plan_sum", "Сума планів", "uah", True)]
    for name, x in agg.items():
        tot = x["services"] + x["products"]
        if not (tot or x["done"] or x["booked"] or x["plans"]):
            continue
        n_p = len(x["patients"])
        vis = x["done"] + x["cancelled"]
        r.rows.append({"name": name, "services": x["services"], "proc": x["proc"], "products": x["products"], "total": tot,
                       "patients": n_p, "per_patient": round(tot / n_p) if n_p else 0, "done": x["done"],
                       "cancelled": x["cancelled"], "cancel_pct": round(100 * x["cancelled"] / vis, 1) if vis else 0,
                       "plans": x["plans"], "plan_sum": x["plan_sum"]})
    r.rows.sort(key=lambda z: -z["total"])
    r.chart = [(z["name"][:18], z["total"]) for z in r.rows[:12] if z["total"]]
    r.chart_title = "Виручка за лікарями"
    top = r.rows[0] if r.rows else None
    r.finish()
    r.tiles = [("Лікарів з виручкою", sum(1 for z in r.rows if z["total"]), None, "int", ""),
               ("Найбільша виручка", top["total"] if top else 0, None, "uah", top["name"] if top else ""),
               ("Бюті-планів", r.total["plans"] if r.total else 0, None, "int", ""),
               ("Візитів відбулось", r.total["done"] if r.total else 0, None, "int", "")]
    return r


# ---------------- Послуги / Товари (позиції чеків) ----------------

def _positions(db, d_from, d_to, kind):
    agg = defaultdict(lambda: {"qty": 0.0, "sum": 0.0, "patients": set(), "staff": defaultdict(float)})
    for c in _checks(db, d_from, d_to):
        for k, name, q, amt, staff in _items(c):
            if k != kind:
                continue
            x = agg[name]
            x["qty"] += q
            x["sum"] += amt
            x["staff"][staff] += amt
            if c.patient_id:
                x["patients"].add(c.patient_id)
    return agg


def _service_lines(db: Session, d_from: date, d_to: date) -> list[dict]:
    """Послуги з чеків CleverBox і продажів каси CRM: назва, категорія, к-сть, оплачено, знижка, хто виконував."""
    from ..models import Sale, Doctor
    out = []

    def f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    for c in _checks(db, d_from, d_to):
        if getattr(c, "crm", False):                 # продажі каси CRM — нижче, окремо (з собівартістю)
            continue
        for it in c.items or []:
            if (it.get("item_type") or "") != "service":
                continue
            qty = f(it.get("quantity")) or 1
            paid = f(it.get("payd_amount") or it.get("total"))
            price = f(it.get("price"))
            disc = max(0.0, round(price * qty - paid, 2)) if price else f(it.get("discount"))
            out.append({"name": (it.get("item_name") or it.get("name") or "—").strip(), "category": (it.get("category_name") or "").strip(),
                        "qty": qty, "paid": paid, "disc": disc, "price": price, "staff": (it.get("initiator_name") or "").strip(),
                        "doctor_id": None, "day": c.date.date()})
    a, b = _rng(d_from, d_to)
    docs = {d.id: d.name for d in db.scalars(select(Doctor))}
    from .techcards import cost_by_service
    from ..models import Appointment as _Ap
    for sl in db.scalars(select(Sale).where(Sale.status == "paid", Sale.at >= a, Sale.at <= b)):
        from .techcards import sale_service_costs
        vcost = sale_service_costs(db, sl)
        for it in sl.items:
            if it.kind != "service":
                continue
            qty = it.qty or 1
            out.append({"name": it.name, "category": it.category or "", "qty": qty, "paid": float(it.total or 0),
                        "disc": max(0.0, round((it.price or 0) * qty - float(it.total or 0), 2)), "price": float(it.price or 0),
                        "staff": docs.get(it.doctor_id, ""), "doctor_id": it.doctor_id, "day": sl.at.date(),
                        "cost_actual": vcost[it.ref_id].pop(0) if vcost.get(it.ref_id) else None})
    return out


def _norm_costs(db: Session) -> dict[int, float]:
    from .service_costs import techcard_costs
    return techcard_costs(db)


def services(db: Session, d_from: date, d_to: date, cat: str = "", staff: str = "", zero: str = "hide") -> Report:
    """Звіт з продажу послуг (як у CleverBox): категорія → послуги; к-сть, сума, % від усіх послуг, знижка,
    собівартість (норми списання), з/п спеціаліста (комісійні з окладу), прибуток."""
    from ..models import Doctor
    from ..integrations.cleverbox import match_doctor
    from . import salary_plans as spl
    from .cb_services_import import service_index
    idx = service_index(db)
    unit_cost = _norm_costs(db)
    doctors = db.scalars(select(Doctor)).all()
    plans = {d.id: spl.by_name(db, (d.extra or {}).get("salary")) for d in doctors}
    dcache: dict = {}
    lines = _service_lines(db, d_from, d_to)
    def cat_of(ln):
        sv_ = idx.get(norm(ln["name"]))
        return ln["category"] or (sv_.category if sv_ else None) or "Без категорії"
    cats_all = sorted({cat_of(ln) for ln in lines} | {(sv.category or "Без категорії") for sv in idx.values()})
    staff_all = sorted({ln["staff"] for ln in lines if ln["staff"]})
    agg: dict = {}
    for ln in lines:
        sv = idx.get(norm(ln["name"]))
        category = cat_of(ln)
        if (cat and category != cat) or (staff and ln["staff"] != staff):
            continue
        doc = None
        if ln["doctor_id"]:
            doc = next((d for d in doctors if d.id == ln["doctor_id"]), None)
        elif ln["staff"]:
            if ln["staff"] not in dcache:
                dcache[ln["staff"]] = match_doctor(ln["staff"], doctors)
            doc = dcache[ln["staff"]]
        sal = 0.0
        pl = plans.get(doc.id) if doc else None
        if pl and pl["comm"]["on"]:
            l2 = {"type": "service", "name": ln["name"], "category": category, "brand": "", "qty": ln["qty"],
                  "paid": ln["paid"], "cost": (ln["price"] * ln["qty"]) or ln["paid"], "disc": ln["disc"] > 0,
                  "cost_tc": ln["cost_actual"] if ln.get("cost_actual") is not None else (unit_cost.get(sv.id, 0.0) if sv else 0.0) * ln["qty"],
                  "cost_fx": float((sv.cost_fixed if sv else 0) or 0) * ln["qty"]}
            sal, _, _ = spl.line_amount(pl, l2)
        x = agg.setdefault((category, ln["name"]), {"qty": 0.0, "sum": 0.0, "disc": 0.0, "cost": 0.0, "salary": 0.0})
        x["qty"] += ln["qty"]
        x["sum"] += ln["paid"]
        x["disc"] += ln["disc"]
        x["cost"] += ln["cost_actual"] if ln.get("cost_actual") is not None else \
            ((unit_cost.get(sv.id, 0.0) or float(sv.cost_fixed or 0)) if sv else 0.0) * ln["qty"]
        x["salary"] += sal
    if zero == "all":                                    # «Показати всі» — і послуги без продажів
        for sv in {id(v): v for v in idx.values()}.values():
            c_ = sv.category or "Без категорії"
            if (not cat or c_ == cat) and not staff and not any(k[1] == sv.name for k in agg):
                agg.setdefault((c_, sv.name), {"qty": 0.0, "sum": 0.0, "disc": 0.0, "cost": 0.0, "salary": 0.0})
    total = sum(x["sum"] for x in agg.values()) or 1
    groups: dict = defaultdict(list)
    for (c_, name), x in agg.items():
        groups[c_].append({"name": name, "qty": x["qty"], "sum": round(x["sum"], 2), "share": round(100 * x["sum"] / total, 2),
                           "disc": round(x["disc"], 2), "cost": round(x["cost"], 2), "salary": round(x["salary"], 2),
                           "profit": round(x["sum"] - x["cost"] - x["salary"], 2)})
    r = Report("services", "Звіт з продажу послуг",
               "Послуги з чеків CleverBox і каси CRM. Категорія — з чека / довідника послуг. Собівартість — матеріали за нормами "
               "списання (Склад → Норми списання) × ціна закупівлі. З/П спеціаліста — комісійні за окладом співробітника "
               "(Налаштування → Оклади); фіксована ставка сюди не входить. Прибуток = сума − собівартість − з/п.")
    r.grouped = True
    r.cols = [Col("name", "Послуга"), Col("qty", "Кількість", "num", True), Col("sum", "Сума", "uah", True),
              Col("share", "% від усіх послуг", "pct"), Col("disc", "Знижка", "uah", True), Col("cost", "Собівартість", "uah", True),
              Col("salary", "З/П спеціаліста", "uah", True), Col("profit", "Прибуток", "uah", True)]
    order = sorted(groups.items(), key=lambda kv: (-sum(z["sum"] for z in kv[1]), kv[0]))
    for c_, items in order:
        items.sort(key=lambda z: (-z["qty"], -z["sum"], z["name"]))
        g = {k: round(sum(z[k] for z in items), 2) for k in ("qty", "sum", "disc", "cost", "salary", "profit")}
        r.rows.append({"name": c_, **g, "share": round(100 * g["sum"] / total, 2), "_group": True})
        r.rows.extend(items)
    r.filters = [{"name": "zero", "label": "Режим відображення", "value": zero, "options": [("hide", "Скрити з 0"), ("all", "Показати всі")]},
                 {"name": "cat", "label": "Категорія", "value": cat, "options": [("", "Усі категорії")] + [(c_, c_) for c_ in cats_all]},
                 {"name": "staff", "label": "Співробітник", "value": staff, "options": [("", "Усі співробітники")] + [(x, x) for x in staff_all]}]
    r.chart = [(c_[:18], sum(z["sum"] for z in items)) for c_, items in order[:12]]
    r.chart_title = "Сума за категоріями"
    r.finish()
    t = r.total or {}
    r.tiles = [("Сума послуг", t.get("sum", 0), None, "uah", f"{t.get('qty', 0):g} послуг"),
               ("Знижки", t.get("disc", 0), None, "uah", ""),
               ("Собівартість", t.get("cost", 0), None, "uah", ""),
               ("З/П спеціалістів", t.get("salary", 0), None, "uah", ""),
               ("Прибуток", t.get("profit", 0), None, "uah",
                f"{round(100 * t.get('profit', 0) / t['sum'], 1)}% від суми" if t.get("sum") else "")]
    return r


def products(db: Session, d_from: date, d_to: date) -> Report:
    from .catalog import product_rows
    agg = _positions(db, d_from, d_to, "product")
    prev = _positions(db, *prev_period(d_from, d_to), "product")
    cat = {norm(pr.product.name): pr for pr in product_rows(db, [])}
    r = Report("products", "Товари", "Продажі товарів за чеками CleverBox. Собівартість — остання ціна закупівлі "
               "(або собівартість CleverBox), прибуток = виручка − собівартість.")
    r.cols = [Col("name", "Товар"), Col("brand", "Бренд"), Col("qty", "К-сть", "num", True), Col("sum", "Виручка", "uah", True),
              Col("cost", "Собівартість", "uah", True), Col("profit", "Валовий прибуток", "uah", True),
              Col("margin", "Маржа", "pct"), Col("patients", "Пацієнтів", "int"), Col("prev", "Попередній період", "uah", True)]
    for name, x in agg.items():
        pr = cat.get(norm(name))
        unit = pr.cost if pr else None
        cost = round(unit * x["qty"], 2) if unit else 0
        profit = x["sum"] - cost if unit else 0
        r.rows.append({"name": name, "brand": (pr.product.brand if pr else "") or "", "qty": x["qty"], "sum": x["sum"],
                       "cost": cost, "profit": profit, "margin": round(100 * profit / x["sum"], 1) if unit and x["sum"] else None,
                       "patients": len(x["patients"]), "prev": prev[name]["sum"] if name in prev else 0,
                       "_href": f"/products/{pr.product.id}" if pr else None})
    r.rows.sort(key=lambda z: -z["sum"])
    by_brand = defaultdict(float)
    for z in r.rows:
        by_brand[z["brand"] or "Без бренду"] += z["sum"]
    r.chart = sorted(by_brand.items(), key=lambda kv: -kv[1])[:10]
    r.chart_title = "Виручка за брендами"
    r.finish()
    t = r.total or {}
    known = [z for z in r.rows if z["margin"] is not None]
    ks = sum(z["sum"] for z in known)
    r.tiles = [("Виручка товарів", t.get("sum", 0), sum(x["sum"] for x in prev.values()), "uah", ""),
               ("Продано, шт", t.get("qty", 0), sum(x["qty"] for x in prev.values()), "num", ""),
               ("Валовий прибуток", t.get("profit", 0), None, "uah",
                f"маржа {round(100 * sum(z['profit'] for z in known) / ks, 1)}%" if ks else ""),
               ("Без собівартості", len(r.rows) - len(known), None, "int", "позицій — внесіть закупівлі")]
    return r


# ---------------- Пацієнти ----------------

def patients(db: Session, d_from: date, d_to: date) -> Report:
    a, b = _rng(d_from, d_to)
    first = dict(db.execute(select(Check.patient_id, func.min(Check.date)).where(
        Check.deleted.is_(False), Check.patient_id.is_not(None)).group_by(Check.patient_id)).all())
    from ..models import Sale as _Sale                      # перша оплата може бути вже в касі CRM (після переходу з CleverBox)
    for pid, at in db.execute(select(_Sale.patient_id, func.min(_Sale.at)).where(_Sale.status == "paid", _Sale.patient_id.is_not(None))
                              .group_by(_Sale.patient_id)):
        if pid not in first or at < first[pid]:
            first[pid] = at
    with_plan = {pid for (pid,) in db.execute(select(BeautyPlan.patient_id).where(BeautyPlan.status != "cancelled"))}
    agg = defaultdict(lambda: {"sum": 0.0, "checks": 0, "services": 0.0, "products": 0.0, "last": None})
    for c in _checks(db, d_from, d_to):
        if not c.patient_id:
            continue
        x = agg[c.patient_id]
        x["sum"] += c.amount or 0
        x["checks"] += 1
        x["last"] = max(x["last"] or c.date, c.date)
        for kind, _, _, amt, _ in _items(c):
            if kind == "service":
                x["services"] += amt
            elif kind == "product":
                x["products"] += amt
    new = {pid for pid in agg if first.get(pid) and first[pid] >= a}
    pa, pb = prev_period(d_from, d_to)
    prev_ids = {c.patient_id for c in _checks(db, pa, pb) if c.patient_id}
    names = {p.id: p.full_name for p in db.scalars(select(Patient).where(Patient.id.in_(list(agg) or [0])))}
    r = Report("patients", "Пацієнти", "Пацієнти з оплатою за період. Нова — найперша оплата (CleverBox або каса CRM) припала на цей період.")
    r.cols = [Col("name", "Пацієнт"), Col("type", "Тип"), Col("plan", "Бюті-план"), Col("checks", "Чеків", "int", True),
              Col("services", "Послуги", "uah", True), Col("products", "Догляд", "uah", True), Col("sum", "Разом", "uah", True),
              Col("last", "Остання оплата", "date")]
    for pid, x in agg.items():
        r.rows.append({"name": names.get(pid, f"#{pid}"), "type": "нова" if pid in new else "повторна",
                       "plan": "так" if pid in with_plan else "—", "checks": x["checks"], "services": x["services"],
                       "products": x["products"], "sum": x["sum"], "last": x["last"], "_href": f"/patients/{pid}"})
    r.rows.sort(key=lambda z: -z["sum"])
    n = len(agg)
    tot = sum(x["sum"] for x in agg.values())
    plan_sum = sum(x["sum"] for pid, x in agg.items() if pid in with_plan)
    r.tiles = [("Пацієнтів з оплатою", n, len(prev_ids), "int", ""),
               ("Нових", len(new), None, "int", f"{round(100 * len(new) / n)}%" if n else ""),
               ("Повторних", n - len(new), None, "int", f"повернулись з минулого періоду: {len(prev_ids & set(agg))}"),
               ("Виручка на пацієнта", round(tot / n) if n else 0, None, "uah", ""),
               ("Виручка від пацієнтів з бюті-планом", plan_sum, None, "uah", f"{round(100 * plan_sum / tot)}% виручки" if tot else "")]
    r.chart = [("нові", sum(agg[p]["sum"] for p in new)), ("повторні", tot - sum(agg[p]["sum"] for p in new))]
    r.chart_title = "Виручка: нові / повторні"
    return r.finish()


# ---------------- Бюті-плани ----------------

def plans(db: Session, d_from: date, d_to: date) -> Report:
    from .kpi import all_statuses, kpi_summary
    total, by_doc = kpi_summary(all_statuses(db, d_from, d_to))
    pt, _ = kpi_summary(all_statuses(db, *prev_period(d_from, d_to)))
    r = Report("plans", "Бюті-плани", "Плани з датою консультації в періоді. Реалізація — виконані процедури й куплений "
               "догляд за даними CleverBox (на сьогодні).")
    r.tiles = [("Бюті-планів", total.plans, pt.plans, "int", ""),
               ("Сума планів", total.planned_sum, pt.planned_sum, "uah", f"сер. {total.avg_plan:,.2f} ₴".replace(",", " ")),
               ("Реалізовано", total.realized_sum, pt.realized_sum, "uah", f"{total.realization_pct}%"),
               ("Почали виконувати", total.started_plans, pt.started_plans, "int", f"конверсія {total.conversion_pct}%"),
               ("Записані далі", total.booked_plans, None, "int", f"{total.booked_pct}% активних")]
    r.cols = [Col("name", "Лікар"), Col("plans", "Планів", "int", True), Col("planned", "Сума планів", "uah", True),
              Col("avg", "Сер. план", "uah"), Col("realized", "Реалізовано", "uah", True), Col("real_pct", "% реалізації", "pct"),
              Col("conv", "Конверсія", "pct"), Col("booked", "Записані", "int", True), Col("red", "Без руху", "int", True),
              Col("proc", "Процедур виконано", "text")]
    for k in by_doc:
        r.rows.append({"name": k.label, "plans": k.plans, "planned": k.planned_sum, "avg": k.avg_plan,
                       "realized": k.realized_sum, "real_pct": k.realization_pct, "conv": k.conversion_pct,
                       "booked": k.booked_plans, "red": k.red, "proc": f"{k.proc_done} з {k.proc_planned}"})
    r.chart = [(z["name"][:18], z["planned"]) for z in r.rows]
    r.chart_title = "Сума планів за лікарями"
    return r.finish()


# ---------------- Поставки ----------------

def supply(db: Session, d_from: date, d_to: date) -> Report:
    a, b = _rng(d_from, d_to)
    inv = db.scalars(select(SupplierInvoice).where(SupplierInvoice.inv_date >= a, SupplierInvoice.inv_date <= b)).all()
    pa, pb = _rng(*prev_period(d_from, d_to))
    prev = db.scalars(select(SupplierInvoice).where(SupplierInvoice.inv_date >= pa, SupplierInvoice.inv_date <= pb)).all()
    agg = defaultdict(lambda: {"n": 0, "sum": 0.0, "paid": 0.0, "last": None, "wh": set()})
    for i in inv:
        x = agg[i.supplier or "—"]
        x["n"] += 1
        x["sum"] += i.amount or 0
        x["paid"] += i.paid or 0
        x["last"] = max(x["last"] or i.inv_date, i.inv_date)
        if i.warehouse_name:
            x["wh"].add(i.warehouse_name.strip())
    r = Report("supply", "Поставки", "З імпорту «Поставки» CleverBox (Склад → Постачальники). Борг — за поставками періоду.",
               empty="Поставок за період немає — завантажте вивантаження «Поставки» в Склад → Постачальники.")
    r.cols = [Col("name", "Постачальник"), Col("n", "Поставок", "int", True), Col("sum", "Сума", "uah", True),
              Col("paid", "Оплачено", "uah", True), Col("debt", "Борг", "uah", True), Col("share", "Частка", "pct"),
              Col("last", "Остання", "date"), Col("wh", "Склади")]
    tot = sum(x["sum"] for x in agg.values()) or 1
    for name, x in agg.items():
        r.rows.append({"name": name, "n": x["n"], "sum": x["sum"], "paid": x["paid"], "debt": max(x["sum"] - x["paid"], 0),
                       "share": round(100 * x["sum"] / tot, 1), "last": x["last"], "wh": ", ".join(sorted(x["wh"]))})
    r.rows.sort(key=lambda z: -z["sum"])
    r.chart = [(z["name"][:14], z["sum"]) for z in r.rows[:10]]
    r.chart_title = "Закупівлі за постачальниками"
    r.finish()
    t = r.total or {}
    r.tiles = [("Закуплено", t.get("sum", 0), sum(i.amount or 0 for i in prev), "uah", f"{len(inv)} поставок"),
               ("Оплачено", t.get("paid", 0), None, "uah", ""),
               ("Борг за період", t.get("debt", 0), None, "uah", ""),
               ("Постачальників", len(agg), len({i.supplier for i in prev}), "int", "")]
    return r


# ---------------- Сертифікати ----------------

def certificates(db: Session, d_from: date, d_to: date) -> Report:
    from ..models import GiftCertificate
    from .certificates import STATES
    today = date.today()
    allc = db.scalars(select(GiftCertificate)).all()
    a, b = _rng(d_from, d_to)
    pa, pb = _rng(*prev_period(d_from, d_to))
    sold = [c for c in allc if c.sold_at and a <= c.sold_at <= b and c.state(today) != "unpaid"]
    sold_prev = [c for c in allc if c.sold_at and pa <= c.sold_at <= pb and c.state(today) != "unpaid"]
    live = [c for c in allc if c.state(today) in ("active", "expiring")]
    burned = [c for c in allc if c.state(today) == "expired" and c.valid_to and d_from <= c.valid_to <= d_to]
    r = Report("certificates", "Сертифікати", "З вивантаження CleverBox «Сертифікати» (Дані → Імпорт). Таблиця — "
               "сертифікати, продані за період, плюс усі активні зараз. «Згоріло» — залишок на сертифікатах, "
               "термін яких минув у періоді.", empty="Сертифікатів ще немає — завантажте вивантаження «Сертифікати» з CleverBox.")
    r.tiles = [("Продано", sum(c.paid for c in sold), sum(c.paid for c in sold_prev), "uah", f"{len(sold)} шт"),
               ("Активні зараз", sum(c.balance for c in live), None, "uah",
                f"{len(live)} шт — зобов'язання клініки"),
               ("Згорають за 14 дн", sum(c.balance for c in live if c.state(today) == "expiring"), None, "uah",
                f"{sum(1 for c in live if c.state(today) == 'expiring')} шт — задачі адмінам створено"),
               ("Згоріло з залишком", sum(c.balance for c in burned), None, "uah", f"{len(burned)} шт у періоді")]
    from ..models import Subscription
    subs = [x for x in db.scalars(select(Subscription)) if x.state(today) in ("active", "expiring")]
    if subs:
        r.tiles.append(("Абонементи: невикористано", sum(x.value_left for x in subs), None, "uah",
                        f"{len(subs)} активних абонементів"))
    r.cols = [Col("code", "№"), Col("type", "Сертифікат"), Col("client", "Пацієнт"), Col("seller", "Продав(ла)"),
              Col("sold", "Продано", "date"), Col("nominal", "Номінал", "uah", True), Col("balance", "Залишок", "uah", True),
              Col("used", "Використано", "uah", True), Col("valid", "Діє до", "date"), Col("state", "Стан")]
    seen = set()
    for c in sorted(sold + live, key=lambda c: c.sold_at or datetime.min, reverse=True):
        if c.id in seen:
            continue
        seen.add(c.id)
        r.rows.append({"code": c.code or str(c.id), "type": c.type_name or "", "client": c.client_name or "—",
                       "seller": c.seller or "", "sold": c.sold_at, "nominal": c.nominal, "balance": c.balance,
                       "used": max(c.nominal - c.balance, 0), "valid": c.valid_to, "state": STATES[c.state(today)],
                       "_href": f"/patients/{c.patient_id}" if c.patient_id else None})
    by_seller = defaultdict(float)
    for c in sold:
        by_seller[c.seller or "—"] += c.paid
    r.chart = sorted(by_seller.items(), key=lambda kv: -kv[1])
    r.chart_title = "Продажі сертифікатів за адміністраторами"
    return r.finish()


def cash(db: Session, d_from: date, d_to: date) -> Report:
    """Стан кас і рахунків: залишок на початок / кінець, рух за період; для рахунків monobank — залишок у банку зараз і різниця."""
    from ..models import BankTxn
    from . import cashboxes as cbx
    from ..integrations import monobank
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to + timedelta(days=1), datetime.min.time())
    try:
        bal = monobank.balances(db)
    except Exception:  # noqa: BLE001
        bal = {}
    m = monobank.cashbox_map(db)
    bank_by_box: dict = {}
    for x in monobank.saved(db):
        for acc in x.get("accounts", []):
            box = m.get(monobank.account_key(acc)) or m.get(acc.get("id"))
            if box:
                bank_by_box[box] = {"id": acc["id"], "fop": x["fop"], "bal": (bal.get(acc["id"]) or {}).get("balance"),
                                    "at": (bal.get(acc["id"]) or {}).get("at")}
    r = Report("cash", "Стан рахунків", "Залишки кас і рахунків CRM на початок і кінець періоду. Для рахунків monobank — скільки на рахунку в банку зараз "
               "і різниця з CRM: оплати, ще не привʼязані до поставок, або надходження, яких ще немає в CRM.",
               empty="Немає активних кас — Налаштування → Каси / рахунки.")
    r.cols = [Col("name", "Рахунок"), Col("start", "Залишок на початок", "uah", True), Col("in", "Прихід", "uah", True),
              Col("out", "Витрата", "uah", True), Col("tr", "Переміщення", "uah", True), Col("end", "Залишок на кінець", "uah", True), Col("bank", "У банку зараз", "uah", True),
              Col("diff", "Різниця", "uah", True), Col("open", "Не рознесено monobank", "uah", True), Col("note", "")]
    for x in cbx.items(db):
        if x.get("deleted") or not x.get("active"):
            continue
        mv = cbx.moves(db, x)
        st, en = cbx.balance_at(db, x, a, mv), cbx.balance_at(db, x, b, mv)
        per = [v for v in mv if a <= v.at < b]
        inn = round(sum(v.amount for v in per if v.kind == "in" and not v.transfer), 2)
        out = round(sum(v.amount for v in per if v.kind == "out" and not v.transfer), 2)
        tr = round(sum(v.amount if v.kind == "in" else -v.amount for v in per if v.transfer), 2)
        row = {"name": x["name"], "start": st, "in": inn, "out": out, "tr": tr or None, "end": en, "_href": f"/settings/cashboxes?open={x.get('id', '')}"}
        bk = bank_by_box.get(x["name"])
        if bk:
            now_crm = cbx.balance_at(db, x, datetime.now() + timedelta(seconds=1), mv)
            since = cbx._since(x)
            q = select(BankTxn).where(BankTxn.account == bk["id"], BankTxn.amount < 0, BankTxn.status == "new")
            if since:
                q = q.where(BankTxn.at >= datetime.combine(since, datetime.min.time()))
            if "операційн" in (x["name"] or "").lower():      # з операційних рахунків платимо постачальникам; термінали — переміщення
                row["open"] = round(-sum(t.amount for t in db.scalars(q)), 2) or None
            if bk["bal"] is not None:
                row["bank"] = bk["bal"]
                row["diff"] = round(bk["bal"] - (now_crm or 0), 2)
                row["note"] = f"monobank ФОП {bk['fop']} · {bk['at'] or ''}"
            else:
                row["note"] = f"monobank ФОП {bk['fop']} · баланс недоступний"
        r.rows.append(row)
    r.finish()
    t = r.total or {}
    r.tiles = [("На початок", t.get("start", 0), None, "uah", d_from.strftime("%d.%m.%Y")), ("Прихід", t.get("in", 0), None, "uah", "без переміщень"),
               ("Витрата", t.get("out", 0), None, "uah", "без переміщень"), ("На кінець", t.get("end", 0), None, "uah", d_to.strftime("%d.%m.%Y"))]
    if any(rw.get("bank") is not None for rw in r.rows):
        r.tiles.append(("Різниця з банком", t.get("diff", 0), None, "uah", "банк − CRM зараз"))
    r.chart = [(rw["name"][:14], rw["end"] or 0) for rw in r.rows]
    r.chart_title = "Залишки на кінець періоду"
    return r


def _disc_pct(price: float, qty: float, paid: float, flagged: bool) -> float:
    """Знижка позиції у %: з різниці ціни й оплаченої суми (позначка знижки без різниці — мінімальна знижка)."""
    full = round(price * (qty or 1), 2)
    pct = (1 - paid / full) * 100 if full > 0 and paid < full - 0.01 else 0.0
    return round(max(pct, 0.01 if flagged else 0.0), 2)


def cosmetics(db: Session, d_from: date, d_to: date, staff: str = "") -> Report:
    """Нарахування % лікарям з продажу косметики: 10% від ціни продажу без знижки, 5% — зі знижкою,
    0 — якщо ціна товару менше 1000 ₴. Позиції чеків CleverBox (ініціатор) і продажів каси CRM (спеціаліст)."""
    from ..models import Sale, Doctor

    def f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    rows = []
    for c in _checks(db, d_from, d_to):
        if getattr(c, "crm", False):                 # продажі каси CRM — нижче, окремо
            continue
        for it in c.items or []:
            if (it.get("item_type") or "") != "product":
                continue
            q = f(it.get("quantity")) or 1
            paid = f(it.get("payd_amount") or it.get("total"))
            price = f(it.get("price")) or (paid / q if q else paid)
            rows.append({"at": c.date, "staff": (it.get("initiator_name") or "").strip() or "Без ініціатора",
                         "name": (it.get("item_name") or "—").strip(), "qty": q, "price": price, "paid": paid,
                         "disc_pct": _disc_pct(price, q, paid, f(it.get("discount")) > 0), "ref": f"чек CleverBox {c.id}"})
    a, b = _rng(d_from, d_to)
    docs = {d.id: d.name for d in db.scalars(select(Doctor))}
    for sl in db.scalars(select(Sale).where(Sale.status == "paid", Sale.at >= a, Sale.at <= b)):
        for it in sl.items:
            if it.kind != "product":
                continue
            q = it.qty or 1
            rows.append({"at": sl.at, "staff": docs.get(it.doctor_id, "Без ініціатора"), "name": it.name, "qty": q,
                         "price": float(it.price or 0), "paid": float(it.total or 0), "product_id": it.ref_id,
                         "disc_pct": _disc_pct(float(it.price or 0), q, float(it.total or 0), bool(it.discount)), "ref": f"чек CRM Ч-{sl.id}"})
    staff_all = sorted({x["staff"] for x in rows})
    if staff:
        rows = [x for x in rows if x["staff"] == staff]
    from .cosmetics_pct import Rules
    rules = Rules(db)
    for x in rows:
        rules.apply(x)
    r = Report("cosmetics", "Бонус за продаж косметики",
               rules.describe() + " Лікар — ініціатор позиції в чеку CleverBox або спеціаліст у продажі каси CRM.")
    r.grouped = True
    r.cols = [Col("name", "Товар / лікар"), Col("day", "Дата", "date"), Col("qty", "К-сть", "num", True), Col("price", "Ціна", "uah"),
              Col("paid", "Продано на", "uah", True), Col("rule", "Умова"), Col("pct", "%", "pct"), Col("sum", "Нараховано", "uah", True)]
    by: dict = defaultdict(list)
    for x in rows:
        by[x["staff"]].append(x)
    for who, xs in sorted(by.items(), key=lambda kv: -sum(z["sum"] for z in kv[1])):
        xs.sort(key=lambda z: z["at"])
        r.rows.append({"name": who, "qty": sum(z["qty"] for z in xs), "paid": round(sum(z["paid"] for z in xs), 2),
                       "sum": round(sum(z["sum"] for z in xs), 2), "_group": True})
        r.rows.extend({"name": z["name"] + f" · {z['ref']}", "day": z["at"].date(), "qty": z["qty"], "price": round(z["price"], 2),
                       "paid": round(z["paid"], 2), "rule": z["rule"], "pct": z["pct"], "sum": z["sum"]} for z in xs)
    r.filters = [{"name": "staff", "label": "Лікар", "value": staff, "options": [("", "Усі")] + [(x, x) for x in staff_all]}]
    r.chart = [(w[:18], sum(z["sum"] for z in xs)) for w, xs in sorted(by.items(), key=lambda kv: -sum(z["sum"] for z in kv[1]))[:12]]
    r.chart_title = "Нараховано за лікарями"
    # окремий вигляд: аватари лікарів і фото товарів
    from ..models import Product
    from ..parsers.observ import norm as _nrm
    doc_ph = {d.name: d.photo_url for d in db.scalars(select(Doctor)) if d.photo_url}
    p_ph, p_ph_id = {}, {}
    for pid, nm, ph in db.execute(select(Product.id, Product.name, Product.photo_url).where(Product.photo_url.is_not(None))):
        p_ph_id[pid] = ph
        p_ph.setdefault(_nrm(nm or ""), ph)
    groups = []
    for who, xs in sorted(by.items(), key=lambda kv: -sum(z["sum"] for z in kv[1])):
        groups.append({"staff": who, "photo": doc_ph.get(who), "n": len(xs), "qty": sum(z["qty"] for z in xs),
                       "paid": round(sum(z["paid"] for z in xs), 2), "sum": round(sum(z["sum"] for z in xs), 2),
                       "rows": [{**z, "photo": p_ph_id.get(z.get("product_id")) or p_ph.get(_nrm(z["name"] or ""))} for z in xs]})
    r.extra = {"groups": groups, "staff_all": staff_all, "staff": staff, "cfg": rules.c}
    r.finish()
    t = r.total or {}
    r.tiles = [("Нараховано лікарям", t.get("sum", 0), None, "uah", ""),
               ("Продано косметики", t.get("paid", 0), None, "uah", f"{t.get('qty', 0):g} шт."),
               ("Без %", round(sum(z["paid"] for z in rows if z["pct"] == 0), 2), None, "uah", "")]
    return r


def sources(db: Session, d_from: date, d_to: date, src: str = "") -> Report:
    """Візити за джерелами запису (поле «Джерело запису» у вікні нового візиту) за датою візиту."""
    from ..models import Appointment, Sale, Doctor
    from .visit_statuses import labels
    a, b = _rng(d_from, d_to)
    appts = db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start <= b, Appointment.cb_status.is_(None))
                       .order_by(Appointment.start)).all()
    sale_tot = {sid: float(t or 0) for sid, t in db.execute(select(Sale.id, Sale.total).where(
        Sale.id.in_({x.sale_id for x in appts if x.sale_id}), Sale.status == "paid"))} if appts else {}
    first = {}
    pids = {x.patient_id for x in appts}
    if pids:
        for pid, st in db.execute(select(Appointment.patient_id, func.min(Appointment.start)).where(Appointment.patient_id.in_(pids))
                                  .group_by(Appointment.patient_id)):
            first[pid] = st
    NONE = "Не вказано"
    by = defaultdict(lambda: {"n": 0, "done": 0, "lost": 0, "new": 0, "plan": 0.0, "paid": 0.0})
    for x in appts:
        k = x.booking_source or NONE
        g = by[k]
        g["n"] += 1
        g["done"] += x.status in ("done", "paid") or bool(x.sale_id)
        g["lost"] += x.status in ("cancelled", "no_show")
        g["new"] += first.get(x.patient_id) == x.start
        g["plan"] += 0 if x.status in ("cancelled", "no_show") else float(x.total or 0)
        g["paid"] += sale_tot.get(x.sale_id, 0)
    r = Report("sources", "Візити за джерелами запису",
               "Звідки записалися пацієнти: джерело обирається у вікні нового візиту (список — «Налаштування → Клініка → Джерела запису»). "
               "Період — за датою візиту. «Не вказано» — візити, створені до появи поля або без джерела.")
    opts = [("", "Усі джерела")] + [(k, k) for k in sorted(by, key=lambda k: -by[k]["n"])]
    r.filters = [{"name": "src", "label": "Джерело", "value": src, "options": opts}]
    if src:
        st_ = labels(db)
        docs = {d.id: d.name for d in db.scalars(select(Doctor))}
        r.cols = [Col("day", "Дата", "date"), Col("time", "Час"), Col("name", "Пацієнт"), Col("doc", "Лікар"), Col("st", "Статус"),
                  Col("new", "Новий"), Col("plan", "Сума запису", "uah", True), Col("paid", "Оплачено", "uah", True)]
        for x in appts:
            if (x.booking_source or NONE) != src:
                continue
            r.rows.append({"day": x.start.date(), "time": x.start.strftime("%H:%M"), "name": x.patient.full_name if x.patient else "—",
                           "doc": docs.get(x.doctor_id, "—"), "st": st_.get(x.status, x.status), "new": "так" if first.get(x.patient_id) == x.start else "",
                           "plan": 0 if x.status in ("cancelled", "no_show") else round(float(x.total or 0), 2),
                           "paid": round(sale_tot.get(x.sale_id, 0), 2), "_href": f"/calendar?start={x.start.date()}&open={x.id}"})
    else:
        r.cols = [Col("name", "Джерело"), Col("n", "Записів", "int", True), Col("done", "Відбулось", "int", True),
                  Col("lost", "Скасовано / не прийшов", "int", True), Col("new", "Нових пацієнтів", "int", True),
                  Col("conv", "Відбулось, %", "pct"), Col("plan", "Сума записів", "uah", True), Col("paid", "Оплачено", "uah", True)]
        for k, g in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
            r.rows.append({"name": k, **{z: g[z] for z in ("n", "done", "lost", "new")}, "conv": round(100 * g["done"] / g["n"], 1) if g["n"] else 0,
                           "plan": round(g["plan"], 2), "paid": round(g["paid"], 2), "_href": f"/reports?r=sources&df={d_from}&dt_={d_to}&src=" + __import__("urllib.parse", fromlist=["x"]).quote(k)})
        r.chart = [(k[:18], g["n"]) for k, g in sorted(by.items(), key=lambda kv: -kv[1]["n"])[:12]]
        r.chart_title = "Записів за джерелами"
    r.finish()
    tot = sum(g["n"] for g in by.values())
    r.tiles = [("Записів", tot, None, "int", ""), ("Нових пацієнтів", sum(g["new"] for g in by.values()), None, "int", ""),
               ("Оплачено", round(sum(g["paid"] for g in by.values()), 2), None, "uah", ""),
               ("Без джерела", by[NONE]["n"] if NONE in by else 0, None, "int", "")]
    return r


BUILDERS = {"cash": cash, "revenue": revenue, "doctors": doctors, "services": services, "products": products,
            "patients": patients, "plans": plans, "certificates": certificates, "supply": supply,
            "cosmetics": cosmetics}


def build(db: Session, key: str, d_from: date, d_to: date, **opts) -> Report:
    if key == "services":
        return services(db, d_from, d_to, **{k: v for k, v in opts.items() if k in ("cat", "staff", "zero") and v})
    if key == "cosmetics":
        return cosmetics(db, d_from, d_to, staff=opts.get("staff") or "")
    if key == "sources":
        return sources(db, d_from, d_to, src=opts.get("src") or "")
    if key == "certificates" and opts.get("cat") == "use":
        from .reports_sales import cert_use
        r = cert_use(db, d_from, d_to)
        r.filters = [{"name": "cat", "label": "Показати", "value": "use", "options": [("", "Реєстр"), ("use", "Використання")]}]
        return r
    if key == "certificates":
        r = certificates(db, d_from, d_to)
        r.filters = [{"name": "cat", "label": "Показати", "value": "", "options": [("", "Реєстр"), ("use", "Використання")]}]
        return r
    if key == "discounts":
        from .reports_sales import discounts
        return discounts(db, d_from, d_to, opts.get("cat") or "")
    if key in ("mat_use", "inventory", "dead", "stock_value", "turnover"):
        from . import reports_stock as _st
        return _st.BUILDERS[key](db, d_from, d_to, opts.get("cat") or "")
    if key in ("load", "admin_kpi", "tasks", "cancels"):
        from . import reports_staff as _sf
        return _sf.BUILDERS[key](db, d_from, d_to, opts.get("cat") or "")
    if key == "cohorts":
        from .reports_sales import cohorts
        return cohorts(db, d_from, d_to)
    if key in ("rfm", "bdays"):
        from . import reports_sales as _rs2
        return getattr(_rs2, key)(db, d_from, d_to, opts.get("cat") or "")
    if key == "lost":
        from .reports_sales import lost
        return lost(db, d_from, d_to, opts.get("cat") or "")
    if key in ("ltv", "src_sales"):
        from . import reports_sales as _rs
        return getattr(_rs, key)(db, d_from, d_to)
    if key == "abc":
        from .reports_sales import abc
        return abc(db, d_from, d_to, opts.get("cat") or "")
    if key == "cross":
        from .reports_sales import cross
        return cross(db, d_from, d_to, opts.get("cat") or "")
    return BUILDERS.get(key, revenue)(db, d_from, d_to)


def to_xlsx(rep: Report, d_from: date, d_to: date) -> bytes:
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = re.sub(r"[:\\/?*\[\]]", " ", rep.title)[:30]
    ws.append([f"{rep.title} · {d_from:%d.%m.%Y} — {d_to:%d.%m.%Y}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    ws.append([c.label for c in rep.cols])
    for cell in ws[3]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="3B3050")
    for row in rep.rows:
        ws.append([_xl(row.get(c.key), c.kind) for c in rep.cols])
        if row.get("_group"):
            for cell in ws[ws.max_row]:
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="E4DEEF")
    if rep.total:
        ws.append(["Разом"] + [_xl(rep.total.get(c.key), c.kind) if c.key in rep.total else None for c in rep.cols[1:]])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    for i, c in enumerate(rep.cols, 1):
        ws.column_dimensions[ws.cell(3, i).column_letter].width = 42 if i == 1 else (24 if c.kind == "text" else 15)
        if c.kind in ("uah", "num"):
            for (cell,) in ws.iter_rows(min_row=4, min_col=i, max_col=i):
                cell.number_format = "#,##0.00" if c.kind == "uah" else "#,##0.##"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xl(v, kind):
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    return v
