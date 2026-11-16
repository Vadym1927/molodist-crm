"""Звіт «Інтернет-магазин»: замовлення molodist.store (історія з Shop-Express + нові), KPI, товари, покупці, статуси."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy.orm import Session

from .reports import Col, Report, prev_period
from . import shop_orders as so

MON = ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]


def shop(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    view = view if view in ("products", "buyers", "statuses", "list") else ""
    rows = so.listing(db, d_from, d_to)
    k = so.kpi(rows)
    pf, pt = prev_period(d_from, d_to)
    kp = so.kpi(so.listing(db, pf, pt))
    r = Report("shop", "Інтернет-магазин",
               "Замовлення сайту molodist.store: історія перенесена з Shop-Express, нові додаються щодня. Виручка — без скасованих, "
               "повернутих і фейкових. Покупець привʼязаний до картки пацієнта за телефоном.",
               empty="Замовлень за період немає. Перенести — «Налаштування → molodist.store → Перенести замовлення».")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "По місяцях"), ("products", "Товари"), ("buyers", "Покупці"), ("statuses", "Статуси"), ("list", "Усі замовлення")]}]
    r.tiles = [("Виручка", k["revenue"], kp["revenue"], "uah", f"{k['ok']} замовлень"),
               ("Середній чек", k["avg"], kp["avg"], "uah", ""),
               ("Покупців", k["buyers"], kp["buyers"], "int", f"повторних {k['repeat']}"),
               ("Скасовано / повернуто", k["lost"], kp["lost"], "int", f"з {k['orders']} замовлень")]
    ok = [o for o in rows if not so.is_lost(o)]
    if view == "products":
        agg = defaultdict(lambda: {"qty": 0.0, "sum": 0.0, "orders": 0, "pid": None})
        for o in ok:
            for it in o.items or []:
                a = agg[it.get("name") or "—"]
                a["qty"] += it.get("qty") or 0
                a["sum"] += it.get("total") or 0
                a["orders"] += 1
                a["pid"] = a["pid"] or it.get("product_id")
        r.cols = [Col("name", "Товар"), Col("orders", "Замовлень", "int", True), Col("qty", "Штук", "num", True), Col("sum", "Сума", "uah", True)]
        for n, a in sorted(agg.items(), key=lambda kv: -kv[1]["sum"]):
            r.rows.append({"name": n, "orders": a["orders"], "qty": a["qty"], "sum": round(a["sum"], 2),
                           **({"_href": f"/products/{a['pid']}"} if a["pid"] else {})})
        return r
    if view == "buyers":
        agg = defaultdict(lambda: {"n": 0, "sum": 0.0, "last": None, "name": "", "pid": None})
        for o in ok:
            a = agg[o.patient_id or o.phone or o.email or o.id]
            a["n"] += 1
            a["sum"] += o.total or 0
            a["name"] = a["name"] or o.name or ""
            a["pid"] = o.patient_id
            a["last"] = max(filter(None, [a["last"], o.created_at]), default=None)
        r.cols = [Col("name", "Покупець"), Col("n", "Замовлень", "int", True), Col("sum", "Сума", "uah", True), Col("last", "Останнє", "date")]
        for a in sorted(agg.values(), key=lambda a: -a["sum"]):
            r.rows.append({"name": a["name"] or "—", "n": a["n"], "sum": round(a["sum"], 2), "last": a["last"],
                           **({"_href": f"/patients/{a['pid']}#t-shop"} if a["pid"] else {})})
        return r
    if view == "statuses":
        agg = defaultdict(lambda: [0, 0.0])
        for o in rows:
            agg[o.status or "—"][0] += 1
            agg[o.status or "—"][1] += o.total or 0
        r.cols = [Col("st", "Статус"), Col("n", "Замовлень", "int", True), Col("sum", "Сума", "uah", True)]
        r.rows = [{"st": s, "n": v[0], "sum": round(v[1], 2)} for s, v in sorted(agg.items(), key=lambda kv: -kv[1][0])]
        return r
    if view == "list":
        r.cols = [Col("no", "№"), Col("at", "Дата", "date"), Col("name", "Покупець"), Col("st", "Статус"), Col("pay", "Оплата"),
                  Col("dl", "Доставка"), Col("sum", "Сума", "uah", True)]
        r.rows = [{"no": o.number or o.ext_id, "at": o.created_at, "name": o.name or "—", "st": o.status or "", "pay": ("оплачено" if o.paid else "") ,
                   "dl": o.delivery or "", "sum": o.total or 0, "_href": f"/shop-orders/{o.id}"} for o in rows]
        return r
    agg = defaultdict(lambda: {"n": 0, "sum": 0.0, "lost": 0})
    for o in rows:
        if not o.created_at:
            continue
        a = agg[(o.created_at.year, o.created_at.month)]
        if so.is_lost(o):
            a["lost"] += 1
        else:
            a["n"] += 1
            a["sum"] += o.total or 0
    r.cols = [Col("m", "Місяць"), Col("n", "Замовлень", "int", True), Col("sum", "Виручка", "uah", True), Col("avg", "Середній чек", "uah"),
              Col("lost", "Скасовано", "int", True)]
    for (y, m), a in sorted(agg.items()):
        r.rows.append({"m": f"{MON[m - 1]} {y}", "n": a["n"], "sum": round(a["sum"], 2), "avg": round(a["sum"] / a["n"], 2) if a["n"] else 0,
                       "lost": a["lost"]})
    r.chart = [(x["m"], x["sum"]) for x in r.rows]
    r.chart_title = "Виручка магазину по місяцях"
    return r


BUILDERS = {"shop": shop}
