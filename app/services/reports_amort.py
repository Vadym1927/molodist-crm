"""Звіти блоку «Амортизація»: віддача й завантаження апаратів, прогноз ресурсу розхідників."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AmortAsset, CbService, TechCardLine
from .reports import Col, Report, _rng


def _pct(a: float, b: float) -> float | None:
    return round(100 * a / b, 1) if b else None


def _months(d_from: date, d_to: date) -> float:
    return max(((d_to - d_from).days + 1) / 30.44, 0.03)


def devices(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """Апарати: скільки процедур і виручки дав кожен за період, скільки амортизації нараховано, як завантажений проти плану."""
    from . import amort as am
    from .cb_services_import import service_index
    from .reports import _service_lines
    from ..parsers.observ import norm
    a, b = _rng(d_from, d_to)
    b2 = b + timedelta(seconds=1)
    assets = [x for x in db.scalars(select(AmortAsset).where(AmortAsset.active.is_(True)).order_by(AmortAsset.sort, AmortAsset.id))
              if x.kind == "device"]
    idx = service_index(db)
    lines = _service_lines(db, d_from, d_to)
    mon = _months(d_from, d_to)
    r = Report("devices", "Віддача й завантаження апаратів",
               "По кожному апарату за період: оплачені процедури й виручка послуг, у техкарті яких є цей апарат; нарахована амортизація "
               "(за списаннями у візитах); завантаження — процедур на місяць проти плану з налаштувань апарата. «Амортизовано» й "
               "«залишкова вартість» — на сьогодні, за весь час. Якщо оплачених процедур більше, ніж списано, — є незакриті візити "
               "(список на сторінці апарата).",
               empty="Апаратів в обліку немає — додайте їх у «Налаштування апаратів і ставок».")
    r.cols = [Col("name", "Апарат"), Col("procs", "Оплачено процедур", "int", True), Col("wr", "Списано процедур", "int", True),
              Col("rev", "Виручка", "uah", True), Col("per", "Виручка на процедуру", "uah"), Col("am", "Амортизація", "uah", True),
              Col("share", "Амортизація від виручки", "pct"), Col("pm", "Процедур / міс", "num"), Col("plan", "План / міс", "num"),
              Col("load", "Виконання плану", "pct"), Col("cost", "Вартість апарата", "uah", True), Col("done", "Амортизовано", "pct"),
              Col("left", "Залишкова вартість", "uah", True), Col("end", "Повна амортизація", "date")]
    for x in assets:
        cards = {ln.card_id for ln in db.scalars(select(TechCardLine).where(TechCardLine.product_id == x.product_id))} if x.product_id else set()
        procs = rev = 0.0
        for ln in lines:
            sv = idx.get(norm(ln["name"]))
            if sv and sv.techcard_id in cards:
                procs += ln["qty"]
                rev += ln["paid"]
        u = am.usage(db, x, a, b2)
        t = am.totals(db, x)
        amt = round(u["qty"] * float(x.rate or 0), 2)
        pace = float(t["accrued"]) and amt / mon                      # темп амортизації за період, ₴ / міс
        end = (date.today() + timedelta(days=30.44 * t["residual"] / pace)) if pace and t["residual"] > 0 else None
        r.rows.append({"name": x.name, "procs": round(procs), "wr": u["procs"], "rev": round(rev, 2), "per": round(rev / procs, 2) if procs else None,
                       "am": amt, "share": _pct(amt, rev), "pm": round(procs / mon, 1), "plan": x.avg_month,
                       "load": _pct(procs / mon, float(x.avg_month or 0)), "cost": round(float(x.cost or 0)), "done": t.get("pct"),
                       "left": round(max(t["residual"], 0)), "end": end, "_href": f"/amort/{x.id}"})
    r.rows.sort(key=lambda z: -z["rev"])
    T = {c.key: round(sum(z.get(c.key) or 0 for z in r.rows), 2) for c in r.cols if c.total}
    r.total = T
    gap = sum(max(z["procs"] - z["wr"], 0) for z in r.rows)
    best = r.rows[0] if r.rows and r.rows[0]["rev"] else None
    r.tiles = [("Виручка апаратних процедур", T.get("rev", 0), None, "uah", f"{int(T.get('procs', 0))} процедур"),
               ("Амортизація за період", T.get("am", 0), None, "uah", f"{_pct(T.get('am', 0), T.get('rev', 0)) or 0}% від виручки"),
               ("Найбільше заробляє", best["rev"] if best else 0, None, "uah", best["name"] if best else "немає даних"),
               ("Оплачено, але не списано", gap, None, "int", "процедур — незакриті візити" if gap else "усі візити закриті")]
    r.chart = [(z["name"][:16], z["rev"]) for z in r.rows[:12]]
    r.chart_title = "Виручка за апаратами"
    return r


def consumables(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """Розхідники з ресурсом (маніпули, мотори, лампи): скільки лишилось, коли закінчиться за поточного темпу, чи вистачає фонду на заміну."""
    from . import amort as am
    today = date.today()
    now = datetime.combine(today, datetime.max.time())
    assets = [x for x in db.scalars(select(AmortAsset).where(AmortAsset.active.is_(True)).order_by(AmortAsset.sort, AmortAsset.id))
              if x.kind == "consumable"]
    parents = {x.id: x.name for x in db.scalars(select(AmortAsset))}
    r = Report("consumables", "Прогноз ресурсу розхідників",
               "Маніпули, мотори, лампи з обмеженим ресурсом — стан на сьогодні (період на звіт не впливає). Темп — середня витрата за "
               "останні 90 днів. «Закінчиться» — дата, коли ресурс буде вичерпано за такого темпу. «Фонд на заміну» — нараховано "
               "амортизації проти вартості нового: якщо менше 100% до дати заміни, грошей у фонді не вистачить.",
               empty="Розхідників з ресурсом в обліку немає.")
    r.cols = [Col("name", "Розхідник"), Col("dev", "Апарат"), Col("res", "Ресурс", "num"), Col("used", "Використано", "num"),
              Col("pct", "Використано, %", "pct"), Col("left", "Залишок", "num"), Col("pace", "Темп / міс", "num"),
              Col("months", "Вистачить, міс", "num"), Col("end", "Закінчиться", "date"), Col("cost", "Вартість заміни", "uah", True),
              Col("acc", "Нараховано у фонд", "uah", True), Col("fund", "Фонд на заміну", "pct"), Col("st", "Стан")]
    soon = over = 0
    need = 0.0
    for x in assets:
        t = am.totals(db, x, today)
        u = am.usage(db, x, now - timedelta(days=90), now)
        pace = u["qty"] / 3
        left = max(float(t.get("resource_left") or 0), 0.0)
        months = round(left / pace, 1) if pace else None
        end = today + timedelta(days=30.44 * months) if months is not None else None
        st = "вичерпано" if t.get("over") or (x.resource and left <= 0) else ("заміна до 3 міс" if months is not None and months <= 3 else
                                                                               ("не використовується" if not pace else "в нормі"))
        over += st == "вичерпано"
        soon += st == "заміна до 3 міс"
        if st in ("вичерпано", "заміна до 3 міс"):
            need += max(float(x.cost or 0) - float(t["accrued"]), 0)
        r.rows.append({"name": x.name, "dev": parents.get(x.parent_id, ""), "res": x.resource, "used": t["used"], "pct": t.get("pct"),
                       "left": round(left, 1), "pace": round(pace, 1), "months": months, "end": end, "cost": round(float(x.cost or 0)),
                       "acc": round(float(t["accrued"])), "fund": _pct(float(t["accrued"]), float(x.cost or 0)), "st": st,
                       "_href": f"/amort/{x.id}", "_m": months if months is not None else 10 ** 6})
    r.rows.sort(key=lambda z: (z["st"] != "вичерпано", z["_m"]))
    r.total = {"cost": sum(z["cost"] for z in r.rows), "acc": sum(z["acc"] for z in r.rows)}
    r.tiles = [("Розхідників", len(r.rows), None, "int", ""), ("Вичерпано", over, None, "int", "ресурс закінчився"),
               ("Заміна до 3 місяців", soon, None, "int", "за поточного темпу"),
               ("Бракує у фонді", round(need), None, "uah", "на найближчі заміни" if need else "на найближчі заміни вистачає")]
    return r


BUILDERS = {"devices": devices, "consumables": consumables}
