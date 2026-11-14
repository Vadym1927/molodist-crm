"""Звіти блоку «Склад»: витрата матеріалів (норма техкарти ↔ факт у візитах), інвентаризації (нестачі й надлишки),
неліквіди, вартість складу, оборотність. Джерело — журнал StockMove, документи StockDoc, витратні візитів VisitMaterial."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Appointment, Doctor, Product, StockDoc, StockDocLine, StockMove, VisitMaterial
from .catalog import WAREHOUSES
from .reports import Col, Report, _rng

OUT_KINDS = ("sale", "procedure", "writeoff")


def _products(db: Session) -> dict[int, Product]:
    return {p.id: p for p in db.scalars(select(Product))}


def _uc(db: Session) -> dict[int, float]:
    from .techcards import unit_costs
    return unit_costs(db)


def _wh(k: str | None) -> str:
    return WAREHOUSES.get(k or "", k or "—")


# ---------------- витрата матеріалів: норма ↔ факт ----------------

def mat_use(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    from .techcards import _card_of
    a, b = _rng(d_from, d_to)
    view = view if view in ("doctors", "services", "over") else ""
    appts = {x.id: x for x in db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start <= b,
                                                                  Appointment.status.notin_(("cancelled", "no_show"))))}
    mats = list(db.scalars(select(VisitMaterial).where(VisitMaterial.appt_id.in_(appts)))) if appts else []
    prods, docs = _products(db), {d.id: d.name for d in db.scalars(select(Doctor))}
    cards: dict = {}

    def norm_of(sid):
        if sid not in cards:
            c = _card_of(db, sid)
            cards[sid] = {ln.product_id: float(ln.qty or 0) for ln in c.lines} if c else None
        return cards[sid]

    P = defaultdict(lambda: {"norm": 0.0, "fact": 0.0, "norm_sum": 0.0, "fact_sum": 0.0, "visits": set()})
    D = defaultdict(lambda: {"norm_sum": 0.0, "fact_sum": 0.0, "visits": set()})
    S = defaultdict(lambda: {"norm_sum": 0.0, "fact_sum": 0.0, "visits": set()})
    seen_norm = set()
    with_mats = {m.appt_id for m in mats}
    for m in mats:
        ap = appts[m.appt_id]
        svcs = ap.services or []
        sname = (svcs[m.svc_idx].get("name") if m.svc_idx < len(svcs) and isinstance(svcs[m.svc_idx], dict) else None) or "—"
        fs = float(m.qty or 0) * float(m.unit_cost or 0)
        g = P[m.product_id]
        g["fact"] += float(m.qty or 0)
        g["fact_sum"] += fs
        g["visits"].add(m.appt_id)
        for bucket, key in ((D, docs.get(ap.doctor_id, "—")), (S, sname)):
            bucket[key]["fact_sum"] += fs
            bucket[key]["visits"].add(m.appt_id)
    for aid in with_mats:                                     # норма — за техкартами послуг цих візитів
        ap = appts[aid]
        for idx, s in enumerate(ap.services or []):
            if not isinstance(s, dict) or (aid, idx) in seen_norm:
                continue
            seen_norm.add((aid, idx))
            nm = norm_of(s.get("id"))
            if not nm:
                continue
            for pid, q in nm.items():
                cost = next((float(m.unit_cost or 0) for m in mats if m.appt_id == aid and m.product_id == pid), 0.0)
                P[pid]["norm"] += q
                P[pid]["norm_sum"] += q * cost
                D[docs.get(ap.doctor_id, "—")]["norm_sum"] += q * cost
                S[s.get("name") or "—"]["norm_sum"] += q * cost
    r = Report("mat_use", "Витрата матеріалів на процедури",
               "Скільки матеріалів мало піти за техкартами послуг (норма) і скільки внесено у візитах (факт) — за датою візиту. "
               "Перевитрата — факт більший за норму. Матеріали, додані у візит поза техкартою, мають норму 0.",
               empty="За період у візитах немає витратних матеріалів.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "За матеріалами"), ("over", "Лише перевитрата"), ("doctors", "За лікарями"), ("services", "За процедурами")]}]
    nsum, fsum = sum(g["norm_sum"] for g in P.values()), sum(g["fact_sum"] for g in P.values())
    over = sum(max(g["fact_sum"] - g["norm_sum"], 0) for g in P.values())
    r.tiles = [("Візитів з матеріалами", len(with_mats), None, "int", ""), ("За нормою", round(nsum, 2), None, "uah", ""),
               ("Фактично", round(fsum, 2), None, "uah", f"{round(100 * fsum / nsum - 100, 1):+}% до норми" if nsum else ""),
               ("Перевитрата", round(over, 2), None, "uah", "сума по матеріалах, де факт > норми")]
    if view in ("doctors", "services"):
        src = D if view == "doctors" else S
        r.cols = [Col("name", "Лікар" if view == "doctors" else "Процедура"), Col("visits", "Візитів", "int", True),
                  Col("norm_sum", "За нормою", "uah", True), Col("fact_sum", "Фактично", "uah", True), Col("diff", "Різниця", "uah", True),
                  Col("pct", "% до норми", "pct"), Col("per", "Матеріалів на візит", "uah")]
        for k, g in sorted(src.items(), key=lambda kv: -(kv[1]["fact_sum"] - kv[1]["norm_sum"])):
            n = len(g["visits"])
            r.rows.append({"name": k, "visits": n, "norm_sum": round(g["norm_sum"], 2), "fact_sum": round(g["fact_sum"], 2),
                           "diff": round(g["fact_sum"] - g["norm_sum"], 2),
                           "pct": round(100 * g["fact_sum"] / g["norm_sum"] - 100, 1) if g["norm_sum"] else None,
                           "per": round(g["fact_sum"] / n, 2) if n else 0})
    else:
        r.cols = [Col("name", "Матеріал"), Col("unit", "Од."), Col("visits", "Візитів", "int"), Col("norm", "Норма", "num"), Col("fact", "Факт", "num"),
                  Col("dq", "Різниця", "num"), Col("norm_sum", "За нормою", "uah", True), Col("fact_sum", "Фактично", "uah", True),
                  Col("diff", "Різниця, ₴", "uah", True)]
        for pid, g in sorted(P.items(), key=lambda kv: -(kv[1]["fact_sum"] - kv[1]["norm_sum"])):
            d = g["fact_sum"] - g["norm_sum"]
            if view == "over" and d <= 0.005:
                continue
            p = prods.get(pid)
            r.rows.append({"name": p.name if p else f"#{pid}", "unit": (p.unit if p else "") or "", "visits": len(g["visits"]),
                           "norm": round(g["norm"], 3), "fact": round(g["fact"], 3), "dq": round(g["fact"] - g["norm"], 3),
                           "norm_sum": round(g["norm_sum"], 2), "fact_sum": round(g["fact_sum"], 2), "diff": round(d, 2),
                           "_href": f"/products/{pid}"})
    r.chart = [(k, round(g["fact_sum"] - g["norm_sum"], 2)) for k, g in sorted(D.items(), key=lambda kv: -(kv[1]["fact_sum"] - kv[1]["norm_sum"]))][:12]
    r.chart_title = "Різниця «факт − норма» за лікарями, ₴"
    return r.finish()


# ---------------- інвентаризації ----------------

def inventory(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    uc, prods = _uc(db), _products(db)
    docs = list(db.scalars(select(StockDoc).where(StockDoc.kind == "inventory", StockDoc.status == "posted",
                                                  StockDoc.doc_date >= d_from, StockDoc.doc_date <= d_to).order_by(StockDoc.doc_date.desc())))
    view = view if view in ("short", "plus") else ""
    r = Report("inventory", "Інвентаризації: нестачі й надлишки",
               "Проведені інвентаризації за період: позиції, де фактична кількість не збіглася з обліковою. "
               "Сума — різниця × ціна одиниці (з документа або остання поставка).", empty="За період проведених інвентаризацій з розбіжностями немає.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view, "options": [("", "Усі розбіжності"), ("short", "Нестачі"), ("plus", "Надлишки")]}]
    r.cols = [Col("date", "Дата", "date"), Col("doc", "Документ"), Col("wh", "Склад"), Col("name", "Товар"), Col("sys", "Облік", "num"),
              Col("fact", "Факт", "num"), Col("diff", "Різниця", "num"), Col("sum", "Сума", "uah", True)]
    short = plus = 0.0
    by_wh = defaultdict(float)
    for d in docs:
        for ln in d.lines:
            if ln.sys_qty is None:
                continue
            diff = float(ln.qty or 0) - float(ln.sys_qty or 0)
            if abs(diff) < 1e-6:
                continue
            val = diff * float(ln.unit_cost or uc.get(ln.product_id, 0) or 0)
            short += min(val, 0)
            plus += max(val, 0)
            by_wh[_wh(d.wh_to or d.wh_from)] += min(val, 0)
            if (view == "short" and diff > 0) or (view == "plus" and diff < 0):
                continue
            p = prods.get(ln.product_id)
            r.rows.append({"date": d.doc_date, "doc": d.number, "wh": _wh(d.wh_to or d.wh_from), "name": p.name if p else f"#{ln.product_id}",
                           "sys": round(float(ln.sys_qty), 3), "fact": round(float(ln.qty or 0), 3), "diff": round(diff, 3), "sum": round(val, 2),
                           "_href": f"/stock/docs/{d.id}"})
    r.rows.sort(key=lambda x: x["sum"])
    r.tiles = [("Інвентаризацій", len(docs), None, "int", ""), ("Нестачі", round(short, 2), None, "uah", ""),
               ("Надлишки", round(plus, 2), None, "uah", ""), ("Підсумок", round(short + plus, 2), None, "uah", "надлишки мінус нестачі")]
    r.chart = [(k, round(-v, 2)) for k, v in sorted(by_wh.items(), key=lambda kv: kv[1]) if v < 0]
    r.chart_title = "Нестачі за складами, ₴"
    return r.finish()


# ---------------- залишки, рух ----------------

def _stock(db: Session, at: datetime | None = None) -> dict[tuple[int, str], float]:
    from .stock import balances
    return {k: float(v) for k, v in balances(db, at=at).items() if abs(float(v or 0)) > 1e-6}


def _outflow(db: Session, a: datetime, b: datetime) -> dict[int, float]:
    out = defaultdict(float)
    for m in db.scalars(select(StockMove).where(StockMove.at >= a, StockMove.at <= b, StockMove.kind.in_(OUT_KINDS), StockMove.qty < 0)):
        out[m.product_id] += -float(m.qty)
    return out


def dead(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    days = int(view) if view in ("60", "90", "180") else 90
    uc, prods = _uc(db), _products(db)
    end = datetime.combine(d_to, datetime.max.time())
    stock = defaultdict(float)
    whs = defaultdict(set)
    for (pid, wh), q in _stock(db).items():
        if q > 0:
            stock[pid] += q
            whs[pid].add(_wh(wh))
    last: dict[int, datetime] = {}
    for m in db.scalars(select(StockMove).where(StockMove.kind.in_(OUT_KINDS), StockMove.qty < 0, StockMove.product_id.in_(stock))) if stock else []:
        if m.product_id not in last or m.at > last[m.product_id]:
            last[m.product_id] = m.at
    exp: dict[int, date] = {}
    for m in db.scalars(select(StockMove).where(StockMove.expiry_date.is_not(None), StockMove.product_id.in_(stock))) if stock else []:
        if m.expiry_date >= d_to and (m.product_id not in exp or m.expiry_date < exp[m.product_id]):
            exp[m.product_id] = m.expiry_date
    r = Report("dead", "Неліквіди", f"Товари, що є на складі, але не продавались і не списувались на процедури {days}+ днів (до кінця періоду). "
               "Сума — залишок × ціна останньої поставки: гроші, заморожені на полицях. Обладнання не враховується.",
               empty="Неліквідів немає — усе, що є на складі, рухається.")
    r.filters = [{"name": "cat", "label": "Без руху", "value": str(days) if view else "",
                  "options": [("60", "60+ днів"), ("", "90+ днів"), ("180", "180+ днів")]}]
    r.cols = [Col("name", "Товар"), Col("brand", "Бренд"), Col("wh", "Склад"), Col("qty", "Залишок", "num"), Col("sum", "Сума", "uah", True),
              Col("last", "Останній рух", "date"), Col("idle", "Днів без руху", "int"), Col("exp", "Придатний до", "date")]
    by_wh = defaultdict(float)
    for pid, q in stock.items():
        p = prods.get(pid)
        if not p or p.warehouse == "equip":
            continue
        lt = last.get(pid)
        idle = (end - lt).days if lt else None
        if idle is not None and idle < days:
            continue
        val = q * uc.get(pid, 0.0)
        for w in whs[pid]:
            by_wh[w] += val / len(whs[pid])
        r.rows.append({"name": p.name, "brand": p.brand or "", "wh": ", ".join(sorted(whs[pid])), "qty": round(q, 3), "sum": round(val, 2),
                       "last": lt, "idle": idle if idle is not None else 9999, "exp": exp.get(pid), "_href": f"/products/{pid}"})
    r.rows.sort(key=lambda x: -x["sum"])
    soon = [x for x in r.rows if x["exp"] and (x["exp"] - d_to).days <= 90]
    tot = sum(x["sum"] for x in r.rows)
    r.tiles = [("Заморожено в неліквідах", round(tot, 2), None, "uah", f"{len(r.rows)} позицій"),
               ("Ніколи не рухались", sum(1 for x in r.rows if x["idle"] == 9999), None, "int", "немає жодного продажу чи списання"),
               ("Термін спливає за 90 дн", round(sum(x["sum"] for x in soon), 2), None, "uah", f"{len(soon)} позицій — розпродати першими")]
    r.chart, r.chart_title = sorted(((k, round(v, 2)) for k, v in by_wh.items()), key=lambda kv: -kv[1]), "Неліквіди за складами, ₴"
    return r.finish()


def stock_value(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    view = view if view in ("brand", "products") else ""
    uc, prods = _uc(db), _products(db)
    now = _stock(db, datetime.combine(d_to, datetime.max.time()))
    was = _stock(db, datetime.combine(d_from, datetime.min.time()))
    r = Report("stock_value", "Вартість складу", "Залишки в закупівельних цінах (ціна останньої поставки) на кінець періоду й зміна проти початку періоду. "
               "Обидві дати оцінено за поточними цінами — зміна показує рух товару, а не подорожчання.", empty="Залишків на складах немає.")
    r.filters = [{"name": "cat", "label": "Групувати", "value": view, "options": [("", "За складами"), ("brand", "За брендами"), ("products", "За товарами")]}]

    def key(pid, wh):
        p = prods.get(pid)
        return _wh(wh) if not view else ((p.brand or "Без бренду") if view == "brand" else pid) if p else "—"

    agg = defaultdict(lambda: {"now": 0.0, "was": 0.0, "qty": 0.0, "n": set()})
    for src, fld in ((now, "now"), (was, "was")):
        for (pid, wh), q in src.items():
            if q <= 0 or pid not in prods:
                continue
            g = agg[key(pid, wh)]
            g[fld] += q * uc.get(pid, 0.0)
            if fld == "now":
                g["qty"] += q
                g["n"].add(pid)
    tot_now, tot_was = sum(g["now"] for g in agg.values()), sum(g["was"] for g in agg.values())
    name_col = {"": "Склад", "brand": "Бренд", "products": "Товар"}[view]
    r.cols = [Col("name", name_col)] + ([Col("qty", "Залишок", "num")] if view == "products" else [Col("n", "Позицій", "int", True)]) + \
             [Col("now", "Вартість на кінець", "uah", True), Col("share", "Частка", "pct"), Col("was", "На початок", "uah", True), Col("diff", "Зміна", "uah", True)]
    for k, g in sorted(agg.items(), key=lambda kv: -kv[1]["now"]):
        if g["now"] < 0.005 and g["was"] < 0.005:
            continue
        p = prods.get(k) if view == "products" else None
        r.rows.append({"name": p.name if p else k, "qty": round(g["qty"], 3), "n": len(g["n"]), "now": round(g["now"], 2),
                       "share": round(100 * g["now"] / tot_now, 1) if tot_now else 0, "was": round(g["was"], 2), "diff": round(g["now"] - g["was"], 2),
                       "_href": f"/products/{k}" if p else None})
    r.tiles = [("Вартість складу", round(tot_now, 2), None, "uah", f"на {d_to:%d.%m.%Y}"), ("На початок періоду", round(tot_was, 2), None, "uah", f"на {d_from:%d.%m.%Y}"),
               ("Зміна", round(tot_now - tot_was, 2), None, "uah", ""), ("Позицій із залишком", len({pid for (pid, _), q in now.items() if q > 0}), None, "int", "")]
    r.chart = [(x["name"], x["now"]) for x in r.rows[:12]]
    r.chart_title = "Вартість за " + {"": "складами", "brand": "брендами", "products": "товарами"}[view]
    return r.finish()


def turnover(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    view = view if view in ("low", "over") else ""
    uc, prods = _uc(db), _products(db)
    a, b = _rng(d_from, d_to)
    n_days = max((d_to - d_from).days + 1, 1)
    out = _outflow(db, a, b)
    stock = defaultdict(float)
    for (pid, _), q in _stock(db).items():
        stock[pid] += q
    r = Report("turnover", "Оборотність складу", f"Витрата (продажі + списання на процедури + списання) за період ({n_days} дн.) і на скільки днів вистачить "
               "поточного залишку за такого темпу. «Впритул» — менше 14 днів, «із запасом» — понад 90 днів. Обладнання не враховується.",
               empty="За період руху товарів не було.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view, "options": [("", "Усі товари"), ("low", "Впритул (< 14 дн)"), ("over", "Із запасом (> 90 дн)")]}]
    r.cols = [Col("name", "Товар"), Col("brand", "Бренд"), Col("out", "Витрата за період", "num"), Col("per", "На день", "num"), Col("qty", "Залишок", "num"),
              Col("days", "Вистачить, днів", "int"), Col("sum", "Вартість залишку", "uah", True), Col("state", "Стан")]
    low = over = 0
    over_sum = 0.0
    for pid in set(out) | {k for k, q in stock.items() if q > 0}:
        p = prods.get(pid)
        if not p or p.warehouse == "equip":
            continue
        o, q = out.get(pid, 0.0), max(stock.get(pid, 0.0), 0.0)
        if o <= 0:
            continue                                          # без руху — це звіт «Неліквіди»
        per = o / n_days
        days = int(q / per) if per else None
        st = "впритул" if days < 14 else ("із запасом" if days > 90 else "норма")
        low += st == "впритул"
        if st == "із запасом":
            over += 1
            over_sum += q * uc.get(pid, 0.0)
        if (view == "low" and st != "впритул") or (view == "over" and st != "із запасом"):
            continue
        r.rows.append({"name": p.name, "brand": p.brand or "", "out": round(o, 3), "per": round(per, 3), "qty": round(q, 3), "days": days,
                       "sum": round(q * uc.get(pid, 0.0), 2), "state": st, "_href": f"/products/{pid}"})
    r.rows.sort(key=lambda x: x["days"])
    r.tiles = [("Товарів у русі", low + over + sum(1 for x in r.rows if x["state"] == "норма") if not view else len(r.rows), None, "int", ""),
               ("Впритул", low, None, "int", "вистачить менше ніж на 14 днів"), ("Із запасом", over, None, "int", "понад 90 днів"),
               ("Гроші в надлишковому запасі", round(over_sum, 2), None, "uah", "залишок товарів «із запасом»")]
    bk = [("< 14 дн", 0, 13), ("14–30", 14, 30), ("31–90", 31, 90), ("> 90", 91, 10 ** 9)]
    r.chart = [(k, sum(1 for x in r.rows if lo <= x["days"] <= hi)) for k, lo, hi in bk]
    r.chart_title = "Товари за запасом у днях"
    return r.finish()


BUILDERS = {"mat_use": mat_use, "inventory": inventory, "dead": dead, "stock_value": stock_value, "turnover": turnover}
