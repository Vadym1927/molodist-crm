"""Звіт «Прибутки й збитки (P&L) по місяцях» (/reports?r=pnlm) — за моделлю власника «PnL 2026 Гібрид»:

  ДОХІД: послуги за напрямками (категорія послуги), косметика, сертифікати / абонементи / інше
  − СОБІВАРТІСТЬ: 1) витратні матеріали за техкартами + амортизація апаратів (нарахування), за напрямками;
                  2) оплата роботи лікарів (виплати ЗП лікарям за журналом); 4) закупівельна вартість косметики;
                  5) відсоток лікарям за косметику (нараховано за правилами — довідково, бо входить у виплати)
  = ВАЛОВИЙ ПРИБУТОК (і окремо по послугах) · валова маржа (ціль ≥ 60%)
  − OPEX за групами: оренда, зарплата (адміністратори, бухгалтерія, керівник, прибирання…), маркетинг, реклама,
    програми та сервіси, банки та комісії, господарські, податки, витрати на персонал, інше
  = ОПЕРАЦІЙНИЙ ПРИБУТОК
  − амортизація та капітальні витрати (ремонт, обладнання, зобовʼязання / розтермінування)
  − разові витрати (навчання, допомога ЗСУ, благодійність)
  = ЧИСТИЙ ПРИБУТОК · рентабельність (ціль ≥ 20%)
  KPI: нові пацієнти, пацієнтів, візитів, візитів на пацієнта, середній чек послуги, LTV за період,
       COGS/дохід (≤ 40%), OPEX/дохід (≤ 25%), частка маркетингу, CPA, маржинальність косметики, точка беззбитковості.
  Вилучення власника / дивіденди — поза прибутком, довідково.

Джерела: виручка — чеки CleverBox + каса CRM (місяці без чеків — надходження журналу); витрати — журнал фіноперацій
(CleverBox з 2022 + CRM); собівартість — техкарти / норми й закупівельні ціни; амортизація — модуль «Амортизація»."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .pnl import FIRST_YEAR, MONTHS, MONTHS_FULL
from .patient_money import years  # noqa: F401

# групи OPEX у порядку таблиці власника (перший збіг виграє)
OPEX_GROUPS = [
    ("rent", "Оренда", r"оренд|комунал|електро|світло|опален|газ\b|приміщен"),
    ("salary", "Зарплата (адміністратори, бухгалтерія, керівник, прибирання)", r"__salary__"),
    ("marketing", "Маркетинг", r"маркетолог|маркетинг|smm|смм|таргетолог|блогер|контент|фото|відео|друкован|брендован|дизайн"),
    ("ads", "Реклама", r"реклам|google|гугл|таргет|instagram|інстаграм|facebook|смс|sms|розсилк|зовнішн|bard|лендінг|сайт"),
    ("software", "Програми та сервіси", r"програм|сервіс|підписк|crm|cleverbox|checkbox|телефон|ringostat|хостинг|домен|софт|openai|агент|ai|аі|google workspace|helsi|tilda|shop"),
    ("bank", "Банки та комісії", r"комісі|еквайр|банк|рко"),
    ("household", "Господарські витрати", r"господар|канцел|кава|чай|вода|молоко|хімчист|доставк|нп\b|нова пошта|бензин|генератор|інтернет|утиліз|посуд|raffaello|адміністративн|одн\.|одноразов"),
    ("tax", "Податки", r"податк|єсв|єдиний|пдфо|військов|\bвз\b|\bєп\b"),
    ("staff", "Витрати на персонал", r"корпоратив|день народж|\bдр\b|подарун|тімбілд|персонал (інше|др|копоратив)"),
]
CAPEX_RE = re.compile(r"капітальн|обладнан|ремонт|меблі|техніка|зобов|розтермін|кредит|лізинг|амортизац", re.I)
ONEOFF_RE = re.compile(r"навчанн|курс|семінар|конференц|зсу|допомог|благодійн|волонтер|разов", re.I)
OWNER_RE = re.compile(r"дивіденд|девіденд|власник|вилученн|особист", re.I)
PURCHASE_RE = re.compile(r"закуп|постач|товар|препарат|розхідн|матеріал|косметик|накладн", re.I)
_OPEX_RX = [(k, t, re.compile(rx, re.I)) for k, t, rx in OPEX_GROUPS if rx != "__salary__"]
_STAFF_RX = re.compile(OPEX_GROUPS[-1][2], re.I)
KPI_TARGETS = {"gross_pct": 60, "cogs_pct": 40, "opex_pct": 25, "net_pct": 20}


def classify(article: str, is_sal: bool, role: str | None) -> tuple[str, str]:
    """(блок, група): cogs_labor — ЗП лікарів; opex — група; capex | oneoff | owner | purchase — поза OPEX."""
    a = article or ""
    if OWNER_RE.search(a):
        return "owner", "owner"
    if _STAFF_RX.search(a):
        return "opex", "staff"
    if is_sal:
        return ("cogs_labor", "doctor") if role == "doctor" else ("opex", "salary")
    if ONEOFF_RE.search(a):
        return "oneoff", "oneoff"
    if CAPEX_RE.search(a):
        return "capex", "capex"
    if PURCHASE_RE.search(a):
        return "purchase", "purchase"
    for k, _t, rx in _OPEX_RX:
        if rx.search(a):
            return "opex", k
    return "opex", "other"


def _m(d: date | datetime) -> str:
    return d.strftime("%Y-%m")


def _pct(a: float, b: float):
    return round(100 * a / b, 1) if b else None


def _collect(db: Session, year: int) -> dict:
    from . import reports as rp, finops as fo, clinic
    from .payroll_share import is_salary, role_of
    from .fin_articles import salary_names as _sn
    from .cb_services_import import service_index
    from .catalog import product_rows
    from .cosmetics_pct import Rules
    from ..parsers.observ import norm
    from ..integrations.cleverbox import match_doctor
    from ..models import AmortPeriod, Check, Doctor, Sale
    d_from, d_to = date(year, 1, 1), date(year, 12, 31)
    today = date.today()
    months = [f"{year}-{i:02d}" for i in range(1, 13) if year < today.year or i <= today.month]
    z = lambda: {m: 0.0 for m in months}  # noqa: E731
    doctors = db.scalars(select(Doctor)).all()
    dcache: dict[str, int | None] = {}

    def doc_of(name: str):
        if name not in dcache:
            d = match_doctor(name, doctors)
            dcache[name] = d.id if d else None
        return dcache[name]
    dmap = {d.id: d for d in doctors}
    idx = service_index(db)

    def cat_of(name: str) -> str:
        sv = idx.get(norm(name))
        return (sv.category if sv and sv.category else "Без категорії").strip()
    # ---------------- ДОХІД ----------------
    rev_s, rev_p, rev_o = z(), z(), z()
    rev_cat: dict[str, dict] = defaultdict(z)
    cogs_p, cosm_pct = z(), z()
    pats_m: dict[str, set] = defaultdict(set)
    visits_m: dict[str, set] = defaultdict(set)
    svc_items = z()
    cat = {norm(pr.product.name): pr for pr in product_rows(db, [])}
    rules = Rules(db)
    for c in rp._checks(db, d_from, d_to):
        mk = _m(c.date)
        if mk not in rev_s:
            continue
        for kind, name, q, amt, staff in rp._items(c):
            if kind == "service":
                rev_s[mk] += amt
                rev_cat[cat_of(name)][mk] += amt
                svc_items[mk] += float(q or 1)
                if c.patient_id:
                    pats_m[mk].add(c.patient_id)
                    visits_m[mk].add((c.patient_id, c.date.date()))
            elif kind == "product":
                rev_p[mk] += amt
                pr = cat.get(norm(name))
                if pr and pr.cost:
                    cogs_p[mk] += float(pr.cost) * float(q or 1)
                x = {"staff": staff, "name": name, "price": (amt / float(q or 1)) if q else amt, "qty": q, "paid": amt, "disc_pct": 0,
                     "product_id": pr.product.id if pr else None}
                try:
                    rules.apply(x)
                    cosm_pct[mk] += float(x.get("sum") or 0)
                except Exception:  # noqa: BLE001
                    pass
                if c.patient_id:
                    pats_m[mk].add(c.patient_id)
            else:
                rev_o[mk] += amt
    # ---------------- СОБІВАРТІСТЬ послуг (матеріали) ----------------
    cogs_s = z()
    cogs_cat: dict[str, dict] = defaultdict(z)
    unit_cost = rp._norm_costs(db)
    for ln in rp._service_lines(db, d_from, d_to):
        mk = _m(ln["day"])
        if mk not in cogs_s:
            continue
        if ln.get("cost_actual") is not None:
            v = float(ln["cost_actual"])
        else:
            sv = idx.get(norm(ln["name"]))
            v = ((unit_cost.get(sv.id, 0.0) or float(sv.cost_fixed or 0)) if sv else 0.0) * float(ln["qty"] or 1)
        cogs_s[mk] += v
        cogs_cat[cat_of(ln["name"])][mk] += v
    amort = z()
    for p in db.scalars(select(AmortPeriod).where(AmortPeriod.year == year)):
        mk = f"{year}-{p.month:02d}"
        if mk in amort:
            amort[mk] += float(p.amount or 0)
    # ---------------- ЖУРНАЛ: ЗП лікарів, OPEX, CAPEX, разові, власник ----------------
    ex = clinic.excluded_income(db)
    sal_names = set(_sn(db))
    labor = z()
    labor_doc: dict[str, dict] = defaultdict(z)
    opex = {k: z() for k, _t, _r in OPEX_GROUPS}
    opex["other"] = z()
    opex_art: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(z))     # група → стаття → місяці
    capex, oneoff, owner, purchase, non_money, j_income = z(), z(), z(), z(), z(), z()
    capex_art: dict[str, dict] = defaultdict(z)
    oneoff_art: dict[str, dict] = defaultdict(z)
    for r in fo.journal(db, d_from, d_to):
        mk = _m(r.at)
        if mk not in labor or r.kind == "transfer":
            continue
        if r.kind == "income":
            if r.money and (r.article or "").strip().lower() not in ex:
                j_income[mk] += r.amount
            elif not r.money:
                non_money[mk] += r.amount
            continue
        if not r.money:
            continue
        amt = r.amount if r.kind == "expense" else -r.amount
        is_sal = is_salary(r.article, sal_names)
        role = None
        if is_sal:
            did = None
            if r.src == "crm" and r.id:
                from ..models import FinOp
                op = db.get(FinOp, r.id)
                did = op.doctor_id if op else None
            if did is None and r.who:
                did = doc_of(r.who)
            role = role_of(dmap[did]) if did and did in dmap else "admin"
            if role == "doctor":
                labor_doc[dmap[did].name][mk] += amt
        block, grp = classify(r.article, is_sal, role)
        if block == "cogs_labor":
            labor[mk] += amt
        elif block == "opex":
            opex[grp][mk] += amt
            opex_art[grp][(r.article or "Без статті").strip()][mk] += amt
        elif block == "capex":
            capex[mk] += amt
            capex_art[(r.article or "Без статті").strip()][mk] += amt
        elif block == "oneoff":
            oneoff[mk] += amt
            oneoff_art[(r.article or "Без статті").strip()][mk] += amt
        elif block == "owner":
            owner[mk] += amt
        elif block == "purchase":
            purchase[mk] += amt
    # ---------------- нові пацієнти (перший чек в історії) ----------------
    first = {}
    for pid, d0 in db.execute(select(Check.patient_id, func.min(Check.date)).where(Check.deleted.is_(False), Check.patient_id.is_not(None))
                              .group_by(Check.patient_id)):
        first[pid] = d0
    for pid, d0 in db.execute(select(Sale.patient_id, func.min(Sale.at)).where(Sale.status == "paid", Sale.patient_id.is_not(None)).group_by(Sale.patient_id)):
        if pid not in first or d0 < first[pid]:
            first[pid] = d0
    new_m = z()
    for pid, d0 in first.items():
        mk = _m(d0)
        if mk in new_m:
            new_m[mk] += 1
    # ---------------- рядки по місяцях ----------------
    rows = []
    for mk in months:
        rev = rev_s[mk] + rev_p[mk] + rev_o[mk]
        from_journal = False
        if not rev and j_income[mk]:
            rev, from_journal = j_income[mk], True
        mat = cogs_s[mk] + amort[mk]
        cogs_serv = mat + labor[mk]
        cogs = cogs_serv + cogs_p[mk]
        gross = rev - cogs
        gross_s = rev_s[mk] - cogs_serv
        ox = sum(opex[k][mk] for k in opex)
        op = gross - ox
        net = op - capex[mk] - oneoff[mk]
        n_p, n_v, n_new = len(pats_m[mk]), len(visits_m[mk]), int(new_m[mk])
        mkt = opex["marketing"][mk] + opex["ads"][mk]
        gm = gross / rev if rev else 0
        rows.append({"m": mk, "label": MONTHS[int(mk[5:]) - 1], "full": MONTHS_FULL[int(mk[5:]) - 1], "current": mk == _m(today), "from_journal": from_journal,
                     "rev": rev, "rev_s": rev_s[mk], "rev_p": rev_p[mk], "rev_o": rev_o[mk],
                     "rev_cat": {c_: v[mk] for c_, v in rev_cat.items()},
                     "mat": mat, "cogs_s": cogs_s[mk], "amort": amort[mk], "cogs_cat": {c_: v[mk] for c_, v in cogs_cat.items()},
                     "labor": labor[mk], "labor_doc": {n: v[mk] for n, v in labor_doc.items()}, "cogs_p": cogs_p[mk], "cosm_pct": cosm_pct[mk],
                     "cogs_serv": cogs_serv, "cogs": cogs, "cogs_pct": _pct(cogs, rev),
                     "gross": gross, "gross_s": gross_s, "gross_pct": _pct(gross, rev), "gross_p": rev_p[mk] - cogs_p[mk],
                     "gross_p_pct": _pct(rev_p[mk] - cogs_p[mk], rev_p[mk]),
                     "opex": ox, "opex_g": {k: opex[k][mk] for k in opex}, "opex_pct": _pct(ox, rev),
                     "op": op, "op_pct": _pct(op, rev), "capex": capex[mk], "oneoff": oneoff[mk], "net": net, "net_pct": _pct(net, rev),
                     "owner": owner[mk], "purchase": purchase[mk], "non_money": non_money[mk], "j_income": j_income[mk],
                     "k_new": n_new, "k_pat": n_p, "k_vis": n_v, "k_vpp": round(n_v / n_p, 2) if n_p else None,
                     "k_avg": round(rev_s[mk] / svc_items[mk]) if svc_items[mk] else None, "k_ltv": round(rev / n_p) if n_p else None,
                     "k_mkt_pct": _pct(mkt, rev), "k_cpa": round(mkt / n_new) if n_new else None, "k_mkt_gp": _pct(mkt, gross),
                     "k_breakeven": round(ox / gm) if gm > 0 else None})
    return {"year": year, "months": months, "rows": rows,
            "cats": sorted(set(rev_cat) | set(cogs_cat), key=lambda c_: -sum(rev_cat[c_].values())),
            "docs": sorted(labor_doc, key=lambda n: -sum(labor_doc[n].values())),
            "opex_art": {g: {a: dict(v) for a, v in arts.items()} for g, arts in opex_art.items()},
            "capex_art": {a: dict(v) for a, v in capex_art.items()}, "oneoff_art": {a: dict(v) for a, v in oneoff_art.items()}}


def _sum(rows: list[dict], key: str, sub: str | None = None) -> float:
    return round(sum(((r[key].get(sub, 0.0) if isinstance(r[key], dict) else r[key][sub]) if sub else r[key]) for r in rows), 2)


def build(db: Session, year: int) -> dict:
    cur = _collect(db, year)
    prev = _collect(db, year - 1) if year - 1 >= FIRST_YEAR else None
    rows = cur["rows"]
    done = [r for r in rows if r["rev"] or r["opex"] or r["labor"]]
    t: dict = {}
    for k in ("rev", "rev_s", "rev_p", "rev_o", "mat", "cogs_s", "amort", "labor", "cogs_p", "cosm_pct", "cogs_serv", "cogs", "gross", "gross_s", "gross_p",
              "opex", "op", "capex", "oneoff", "net", "owner", "purchase", "non_money", "j_income", "k_new", "k_pat", "k_vis"):
        t[k] = _sum(rows, k)
    t["opex_g"] = {k: _sum(rows, "opex_g", k) for k in rows[0]["opex_g"]} if rows else {}
    t["rev_cat"] = {c: _sum(rows, "rev_cat", c) for c in cur["cats"]}
    t["cogs_cat"] = {c: _sum(rows, "cogs_cat", c) for c in cur["cats"]}
    t["labor_doc"] = {d: _sum(rows, "labor_doc", d) for d in cur["docs"]}
    for a, b, key in (("cogs", "rev", "cogs_pct"), ("gross", "rev", "gross_pct"), ("gross_p", "rev_p", "gross_p_pct"), ("opex", "rev", "opex_pct"),
                      ("op", "rev", "op_pct"), ("net", "rev", "net_pct")):
        t[key] = _pct(t[a], t[b])
    mkt = t["opex_g"].get("marketing", 0) + t["opex_g"].get("ads", 0)
    t["k_vpp"] = round(t["k_vis"] / t["k_pat"], 2) if t["k_pat"] else None
    t["k_avg"] = round(sum(r["k_avg"] * 1 for r in done if r["k_avg"]) / max(1, sum(1 for r in done if r["k_avg"]))) if done else None
    t["k_ltv"] = round(t["rev"] / t["k_pat"]) if t["k_pat"] else None
    t["k_mkt_pct"], t["k_mkt_gp"] = _pct(mkt, t["rev"]), _pct(mkt, t["gross"])
    t["k_cpa"] = round(mkt / t["k_new"]) if t["k_new"] else None
    gm = t["gross"] / t["rev"] if t["rev"] else 0
    t["k_breakeven"] = round(t["opex"] / gm) if gm > 0 else None
    n = len(done) or 1
    avg = {k: round(t[k] / n, 2) for k in ("rev", "cogs", "gross", "opex", "op", "net", "capex", "oneoff")}
    pm = {r["m"][5:]: r for r in prev["rows"]} if prev else {}
    for r in rows:
        p = pm.get(r["m"][5:])
        r["prev"] = p
        r["yoy_rev"] = _pct(r["rev"] - p["rev"], p["rev"]) if p and p["rev"] else None
        r["yoy_net"] = _pct(r["net"] - p["net"], abs(p["net"])) if p and p["net"] else None
    pt = None
    if prev:
        same = [x for x in prev["rows"] if x["m"][5:] in {r["m"][5:] for r in done}]
        pt = {"rev": _sum(same, "rev"), "net": _sum(same, "net"), "full_rev": _sum(prev["rows"], "rev"), "full_net": _sum(prev["rows"], "net")}
        pt["yoy_rev"] = _pct(t["rev"] - pt["rev"], pt["rev"]) if pt["rev"] else None
        pt["yoy_net"] = _pct(t["net"] - pt["net"], abs(pt["net"])) if pt["net"] else None
    pos = [r for r in done if r["net"] > 0]
    best = max(done, key=lambda r: r["net"]) if done else None
    worst = min(done, key=lambda r: r["net"]) if done else None
    cov = {"cogs_months": sum(1 for r in rows if r["cogs_s"]), "n": len(done), "journal_months": sum(1 for r in rows if r["from_journal"]),
           "rev_vs_journal": round(t["rev"] - t["j_income"], 2), "labor_months": sum(1 for r in rows if r["labor"])}
    def _arts(d: dict) -> list:
        return sorted(((a, mv, round(sum(mv.values()), 2)) for a, mv in d.items()), key=lambda x: -x[2])
    return {"year": year, "rows": rows, "t": t, "avg": avg, "prev": prev, "pt": pt, "best": best, "worst": worst, "pos": len(pos),
            "neg": len(done) - len(pos), "cov": cov, "cats": cur["cats"], "docs": cur["docs"],
            "opex_art": {g: _arts(d) for g, d in cur["opex_art"].items()}, "capex_art": _arts(cur["capex_art"]), "oneoff_art": _arts(cur["oneoff_art"]),
            "OPEX": [(k, t_) for k, t_, _r in OPEX_GROUPS] + [("other", "Інші витрати")], "TARGETS": KPI_TARGETS, "MONTHS": MONTHS,
            "max_rev": max([r["rev"] for r in rows] + [1]), "max_abs": max([abs(r["net"]) for r in rows] + [1]), "has_data": bool(done)}


def to_xlsx(r: dict) -> bytes:
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = f"P&L {r['year']}"
    ws.append([f"Прибутки й збитки по місяцях · {r['year']} (гібридна модель)"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["Показник"] + [x["label"] for x in r["rows"]] + ["Разом"])
    for c in ws[2]:
        c.font = Font(bold=True)
    t = r["t"]

    def line(title, key, sub=None, bold=False, pct=False):
        vals = []
        for x in r["rows"]:
            v = x[key]
            v = (v.get(sub, 0.0) if isinstance(v, dict) else v) if sub else v
            vals.append(None if v is None else round(v, 2))
        tv = t[key]
        tv = (tv.get(sub, 0.0) if isinstance(tv, dict) else tv) if sub else tv
        ws.append([title] + vals + [None if tv is None else round(tv, 2)])
        if bold:
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
    line("ДОХІД", "rev", bold=True)
    line("Послуги загалом", "rev_s")
    for c_ in r["cats"]:
        line("  " + c_, "rev_cat", c_)
    line("Косметика", "rev_p")
    line("Сертифікати / абонементи / інше", "rev_o")
    line("СОБІВАРТІСТЬ", "cogs", bold=True)
    line("Собівартість послуг", "cogs_serv")
    line("1. Витратні матеріали з амортизацією", "mat")
    for c_ in r["cats"]:
        line("  " + c_, "cogs_cat", c_)
    line("  амортизація апаратів (нарахування)", "amort")
    line("2. Оплата роботи лікарів", "labor")
    for d in r["docs"]:
        line("  " + d, "labor_doc", d)
    line("4. Косметика (закупівельна)", "cogs_p")
    line("5. Відсоток по косметиці (довідково, входить у виплати)", "cosm_pct")
    line("ВАЛОВИЙ ПРИБУТОК", "gross", bold=True)
    line("Валовий прибуток по послугах", "gross_s")
    line("Валова маржа, %", "gross_pct", pct=True)
    line("ОПЕРАЦІЙНІ ВИТРАТИ (OPEX)", "opex", bold=True)
    for k, title in r["OPEX"]:
        line(title, "opex_g", k)
        for a, mv, tot_ in r["opex_art"].get(k) or []:
            ws.append(["  " + a] + [round(mv.get(x["m"], 0.0), 2) for x in r["rows"]] + [tot_])
    line("ОПЕРАЦІЙНИЙ ПРИБУТОК", "op", bold=True)
    line("АМОРТИЗАЦІЯ ТА КАПІТАЛЬНІ ВИТРАТИ", "capex", bold=True)
    for a, mv, tot_ in r["capex_art"]:
        ws.append(["  " + a] + [round(mv.get(x["m"], 0.0), 2) for x in r["rows"]] + [tot_])
    line("РАЗОВІ ВИТРАТИ", "oneoff", bold=True)
    for a, mv, tot_ in r["oneoff_art"]:
        ws.append(["  " + a] + [round(mv.get(x["m"], 0.0), 2) for x in r["rows"]] + [tot_])
    line("ЧИСТИЙ ПРИБУТОК", "net", bold=True)
    ws.append([])
    ws.append(["KPI"])
    for title, key in (("Нові пацієнти", "k_new"), ("Кількість пацієнтів", "k_pat"), ("Кількість візитів", "k_vis"), ("Візитів на пацієнта", "k_vpp"),
                       ("Середній чек послуги", "k_avg"), ("LTV за період (дохід / пацієнтів)", "k_ltv"), ("COGS / дохід, % (≤ 40)", "cogs_pct"),
                       ("OPEX / дохід, % (≤ 25)", "opex_pct"), ("Частка маркетингу в доході, %", "k_mkt_pct"), ("CPA (маркетинг / нових)", "k_cpa"),
                       ("Маркетинг / валовий прибуток, %", "k_mkt_gp"), ("Валова маржа, % (≥ 60)", "gross_pct"), ("Точка беззбитковості", "k_breakeven"),
                       ("Рентабельність, % (≥ 20)", "net_pct"), ("Маржинальність косметики, %", "gross_p_pct")):
        line(title, key)
    ws.append([])
    ws.append(["Довідково (поза прибутком)"])
    line("Вилучення власника / дивіденди", "owner")
    line("Закупівлі (касові; у прибутку — через собівартість)", "purchase")
    line("Оплати не грошима (бонуси, сертифікати, баланс)", "non_money")
    ws.column_dimensions["A"].width = 52
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
