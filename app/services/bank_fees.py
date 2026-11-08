"""Звіт «Комісії банків і еквайрингу» (/reports?r=fees): скільки забирають банки по кожній касі / рахунку.

Джерело — журнал фіноперацій (finops.journal): витрати за статтями комісій (еквайринг, комісія банку, РКО, обслуговування
рахунку) по рахунку списання + обороти (надходження грошима) по тих самих рахунках → ефективна ставка %.
CleverBox з 2022 + комісії, які CRM нараховує сама (Налаштування → Каси → % комісії)."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from sqlalchemy.orm import Session

from .pnl import FIRST_YEAR, MONTHS
from .patient_money import period, years  # noqa: F401 — спільні фільтри періоду

FEE_RE = re.compile(r"комісі|еквайр|рко\b|розрахунково-касов|обслуговуванн.*рахунк|банківськ.*послуг|інтерне?т-банк|liqpay|wayforpay|fondy|monopay|plata", re.I)
# явно не комісія, хоч і про банк: кредити, розтермінування
NOT_FEE_RE = re.compile(r"кредит|розтермін|відсотк.*кредит|позик|лізинг", re.I)


def is_fee(article: str, who: str = "") -> bool:
    a = article or ""
    return bool(FEE_RE.search(a)) and not NOT_FEE_RE.search(a)


def build(db: Session, year: int | None, month: int | None = None) -> dict:
    from . import finops as fo, clinic, cashboxes as cbx
    d_from, d_to, label = period(year, month)
    ex = clinic.excluded_income(db)
    rows = fo.journal(db, d_from, d_to)
    acc: dict[str, dict] = {}

    def box(name: str) -> dict:
        return acc.setdefault(name or "— без рахунку", {"name": name or "— без рахунку", "fees": 0.0, "turn": 0.0, "n": 0,
                                                       "m_fee": defaultdict(float), "m_turn": defaultdict(float), "arts": defaultdict(float),
                                                       "src": defaultdict(float), "ops": []})

    fees = turn = 0.0
    arts: dict[str, float] = defaultdict(float)
    bym_fee: dict[str, float] = defaultdict(float)
    bym_turn: dict[str, float] = defaultdict(float)
    for r in rows:
        if r.kind == "transfer" or not r.money:
            continue
        m = r.at.strftime("%Y-%m")
        if r.kind == "income":
            if (r.article or "").strip().lower() in ex:
                continue
            b = box(r.account)
            b["turn"] += r.amount
            b["m_turn"][m] += r.amount
            turn += r.amount
            bym_turn[m] += r.amount
        elif r.kind == "expense" and is_fee(r.article, r.who):
            b = box(r.account)
            b["fees"] += r.amount
            b["n"] += 1
            b["m_fee"][m] += r.amount
            b["arts"][r.article or "Комісія"] += r.amount
            b["src"]["CRM" if r.src == "crm" else "CleverBox"] += r.amount
            if len(b["ops"]) < 400:
                b["ops"].append({"at": r.at, "num": r.num, "article": r.article, "amount": r.amount, "comment": r.comment, "src": r.src})
            fees += r.amount
            arts[r.article or "Комісія"] += r.amount
            bym_fee[m] += r.amount
    lst = []
    for b in acc.values():
        if b["fees"] < 0.005 and b["turn"] < 0.005:
            continue
        b["pct"] = round(100 * b["fees"] / b["turn"], 2) if b["turn"] else None
        b["m_fee"], b["m_turn"], b["arts"], b["src"] = dict(b["m_fee"]), dict(b["m_turn"]), dict(sorted(b["arts"].items(), key=lambda x: -x[1])), dict(b["src"])
        b["ops"].sort(key=lambda o: o["at"], reverse=True)
        lst.append(b)
    lst.sort(key=lambda b: (-b["fees"], -b["turn"]))
    months = sorted(set(bym_fee) | set(bym_turn))
    if year and not month:
        months = [f"{year}-{i:02d}" for i in range(1, 13) if year < date.today().year or i <= date.today().month]
    # налаштовані % комісій по касах — щоб показати ставку поруч із фактичною
    cfg = {x["name"]: {"pct": float(x.get("fee_pct") or 0), "article": x.get("fee_article") or cbx.FEE_ARTICLE, "type": x.get("type")}
           for x in cbx.items(db) if not x.get("deleted")}
    top = lst[0] if lst and lst[0]["fees"] else None
    return {"year": year, "month": month, "label": label, "d_from": d_from, "d_to": d_to, "rows": lst, "fees": round(fees, 2),
            "turn": round(turn, 2), "pct": round(100 * fees / turn, 2) if turn else None,
            "arts": sorted(arts.items(), key=lambda x: -x[1]), "months": months,
            "bym_fee": dict(bym_fee), "bym_turn": dict(bym_turn), "bym_max": max(list(bym_fee.values()) + [1]),
            "cfg": cfg, "top": top, "with_fees": [b for b in lst if b["fees"] >= 0.005], "n_ops": sum(b["n"] for b in lst),
            "per_month": round(fees / max(1, len([m for m in months if bym_fee.get(m)])), 2),
            "first_year": FIRST_YEAR, "MONTHS": MONTHS}


def to_xlsx(r: dict) -> bytes:
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Комісії банків"
    ws.append([f"Комісії банків і еквайрингу · {r['label']} ({r['d_from']:%d.%m.%Y} — {r['d_to']:%d.%m.%Y})"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["Рахунок / каса", "Оборот (надходження), ₴", "Комісії, ₴", "Ефективна ставка, %", "Операцій", "Налаштований %"] + r["months"])
    for c in ws[2]:
        c.font = Font(bold=True)
    for b in r["rows"]:
        ws.append([b["name"], round(b["turn"], 2), round(b["fees"], 2), b["pct"], b["n"], (r["cfg"].get(b["name"]) or {}).get("pct") or None]
                  + [round(b["m_fee"].get(m, 0), 2) for m in r["months"]])
    ws.append(["Разом", r["turn"], r["fees"], r["pct"], r["n_ops"], None] + [round(r["bym_fee"].get(m, 0), 2) for m in r["months"]])
    ws.append([])
    ws.append(["За статтями"])
    for a, v in r["arts"]:
        ws.append([a, None, round(v, 2)])
    ws2 = wb.create_sheet("Операції")
    ws2.append(["Дата", "№", "Рахунок", "Стаття", "Сума", "Коментар", "Джерело"])
    for b in r["rows"]:
        for o in b["ops"]:
            ws2.append([o["at"].strftime("%d.%m.%Y %H:%M"), o["num"], b["name"], o["article"], round(o["amount"], 2), o["comment"], o["src"]])
    ws.column_dimensions["A"].width = 36
    ws2.column_dimensions["F"].width = 50
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
