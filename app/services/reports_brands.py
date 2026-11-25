"""Маржа по брендах: вітрина (продаж товарів) і процедури (препарати й розхідники в послугах).

Собівартість — ціна ОСТАННЬОЇ поставки за одиницю обліку (techcards.unit_costs, правило власника), а не ціна,
зафіксована в момент продажу: звіт показує, скільки бренд заробляє за поточних закупівельних цін.

• Вітрина: товарні позиції чеків (CleverBox і каса CRM). Виручка — оплачено після знижок.
• Процедури: оплачені в касі CRM послуги, витратні матеріали їхніх візитів (VisitMaterial, з урахуванням
  «один комплект на візит»). Виручку послуги ділимо між брендами її матеріалів пропорційно їхній собівартості
  (послуга з препаратом Rejuran і шприцом — майже вся виручка йде на бренд препарату). Послуги без матеріалів
  у бренди не потрапляють — окремий підсумок «Без матеріалів»."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, Product, Sale, VisitMaterial
from ..parsers.observ import norm
from .reports import Col, Report, _checks, _items, _rng, prev_period

NO_BRAND = "Без бренду"


def _brand(p: Product | None) -> str:
    return ((p.brand if p else "") or "").strip() or NO_BRAND


def vitrina(db: Session, d_from: date, d_to: date, uc: dict, prods: dict) -> dict:
    by_name = {}
    for p in prods.values():
        by_name.setdefault(norm(p.name), p)
    out = defaultdict(lambda: {"rev": 0.0, "cost": 0.0, "qty": 0.0, "nocost": 0.0})
    for c in _checks(db, d_from, d_to):
        for k, name, q, amt, _staff in _items(c):
            if k != "product":
                continue
            p = by_name.get(norm(name))
            x = out[_brand(p)]
            x["rev"] += amt
            x["qty"] += q
            u = uc.get(p.id) if p else None
            if u:
                x["cost"] += u * q
            else:
                x["nocost"] += amt
    return out


def procedures(db: Session, d_from: date, d_to: date, uc: dict, prods: dict) -> tuple[dict, float]:
    from .techcards import effective
    a, b = _rng(d_from, d_to)
    out = defaultdict(lambda: {"rev": 0.0, "cost": 0.0, "qty": 0.0, "nocost": 0.0, "svc": 0})
    bare = 0.0
    for sl in db.scalars(select(Sale).where(Sale.status == "paid", Sale.at >= a, Sale.at <= b)):
        slots: dict = defaultdict(list)                     # id послуги → [(appt_id, svc_idx), …] по черзі
        for aid in (sl.appt_ids or ([sl.appointment_id] if sl.appointment_id else [])):
            ap = db.get(Appointment, aid)
            for i, s in enumerate((ap.services if ap else None) or []):
                slots[s.get("id")].append((aid, i))
        mats: dict = {}
        for it in sl.items:
            if it.kind != "service":
                continue
            rev = float(it.total or 0)
            key = slots[it.ref_id].pop(0) if slots.get(it.ref_id) else None
            if key is None:
                bare += rev
                continue
            aid, idx = key
            if aid not in mats:
                rows = db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id == aid)).all()
                eff = effective(db, rows)
                mats[aid] = [(m, eff.get(m.id, m.qty)) for m in rows]
            parts = defaultdict(float)
            qty_by = defaultdict(float)
            for m, q in mats[aid]:
                if m.svc_idx != idx or not q:
                    continue
                u = uc.get(m.product_id) or float(m.unit_cost or 0)
                br = _brand(prods.get(m.product_id))
                parts[br] += u * q
                qty_by[br] += q
            tot = sum(parts.values())
            if tot <= 0:
                bare += rev
                continue
            for br, cst in parts.items():
                x = out[br]
                x["rev"] += rev * cst / tot
                x["cost"] += cst
                x["qty"] += qty_by[br]
                x["svc"] += 1
    return out, bare


def _pct(profit: float, rev: float):
    return round(100 * profit / rev, 1) if rev else None


def build(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    from .techcards import unit_costs
    uc = unit_costs(db)
    prods = {p.id: p for p in db.scalars(select(Product))}
    vit = vitrina(db, d_from, d_to, uc, prods)
    pro, bare = procedures(db, d_from, d_to, uc, prods)
    pv = vitrina(db, *prev_period(d_from, d_to), uc, prods)
    pp, _ = procedures(db, *prev_period(d_from, d_to), uc, prods)

    r = Report("brands", "Маржа по брендах",
               "Собівартість — за ціною останньої поставки. Вітрина — продаж товарів пацієнтам. Процедури — препарати й "
               "розхідники в оплачених послугах каси CRM: виручку послуги ділимо між брендами її матеріалів пропорційно "
               "собівартості. Маржа = (виручка − собівартість) / виручка.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "Вітрина + процедури"), ("vitrina", "Лише вітрина"), ("proc", "Лише процедури")]}]
    r.cols = [Col("brand", "Бренд"),
              Col("v_rev", "Вітрина: виручка", "uah", True), Col("v_cost", "Вітрина: собівартість", "uah", True),
              Col("v_profit", "Вітрина: маржа ₴", "uah", True), Col("v_pct", "Вітрина: маржа %", "pct"),
              Col("p_rev", "Процедури: виручка", "uah", True), Col("p_cost", "Процедури: собівартість", "uah", True),
              Col("p_profit", "Процедури: маржа ₴", "uah", True), Col("p_pct", "Процедури: маржа %", "pct"),
              Col("rev", "Разом: виручка", "uah", True), Col("profit", "Разом: маржа ₴", "uah", True),
              Col("pct", "Разом: маржа %", "pct"), Col("prev", "Маржа ₴ попер. період", "uah", True)]
    if view == "vitrina":
        r.cols = [c for c in r.cols if c.key == "brand" or c.key.startswith("v_") or c.key == "prev"]
    elif view == "proc":
        r.cols = [c for c in r.cols if c.key == "brand" or c.key.startswith("p_") or c.key == "prev"]
    nocost = 0.0
    for br in sorted(set(vit) | set(pro)):
        v, p = vit.get(br), pro.get(br)
        vr, vc = (v["rev"], v["cost"]) if v else (0.0, 0.0)
        pr_, pc = (p["rev"], p["cost"]) if p else (0.0, 0.0)
        if view == "vitrina" and not v or view == "proc" and not p:
            continue
        nocost += v["nocost"] if v else 0
        pvv, ppp = pv.get(br), pp.get(br)
        prev = ((pvv["rev"] - pvv["cost"]) if pvv and view != "proc" else 0) + ((ppp["rev"] - ppp["cost"]) if ppp and view != "vitrina" else 0)
        row = {"brand": br,
               "v_rev": round(vr, 2), "v_cost": round(vc, 2), "v_profit": round(vr - vc, 2), "v_pct": _pct(vr - vc, vr),
               "p_rev": round(pr_, 2), "p_cost": round(pc, 2), "p_profit": round(pr_ - pc, 2), "p_pct": _pct(pr_ - pc, pr_),
               "rev": round(vr + pr_, 2), "profit": round(vr - vc + pr_ - pc, 2), "pct": _pct(vr - vc + pr_ - pc, vr + pr_),
               "prev": round(prev, 2)}
        if v and v["nocost"]:
            row["v_pct"] = None if vr - v["nocost"] <= 0 else _pct(vr - v["nocost"] - vc, vr - v["nocost"])
        r.rows.append(row)
    key = {"vitrina": "v_profit", "proc": "p_profit"}.get(view, "profit")
    r.rows.sort(key=lambda z: -(z.get(key) or 0))
    r.chart = [(z["brand"][:18], z[key]) for z in r.rows[:12]]
    r.chart_title = "Маржа ₴ за брендами"
    r.finish()
    t = r.total or {}
    vr, vp, prr, ppf = t.get("v_rev", 0), t.get("v_profit", 0), t.get("p_rev", 0), t.get("p_profit", 0)
    r.tiles = [("Вітрина: маржа", vp, None, "uah", f"{_pct(vp, vr)}% від {round(vr):,} ₴".replace(",", " ") if vr else ""),
               ("Процедури: маржа", ppf, None, "uah", f"{_pct(ppf, prr)}% від {round(prr):,} ₴".replace(",", " ") if prr else ""),
               ("Разом маржа", vp + ppf, t.get("prev"), "uah", f"{_pct(vp + ppf, vr + prr)}%" if vr + prr else ""),
               ("Послуги без матеріалів", round(bare, 2), None, "uah", "виручка не розподілена на бренди"),
               ("Товари без собівартості", round(nocost, 2), None, "uah", "виручка — внесіть поставки")]
    r.extra = {"bare": bare, "nocost": nocost}
    return r
