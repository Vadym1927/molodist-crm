"""Фінанси як у CleverBox: фіноперації, продажі (за день і за період), нарахування й взаєморозрахунки по з/п,
баланси й бонуси пацієнтів. Дані — чеки CleverBox (Check), виписки monobank (BankTxn) і ручні операції CRM (FinOp)."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (BankTxn, Check, Doctor, FinOp, Patient, SalaryAdj, SalaryRule, Supplier, SyncState)

EXPENSE_CATS = ["Зарплата", "Оренда", "Комунальні послуги", "Закупівля товару", "Витратні матеріали", "Маркетинг і реклама",
                "Податки й збори", "Банк і еквайринг", "Обладнання й ремонт", "Навчання", "Господарські витрати", "Інше"]
INCOME_CATS = ["Внесення в касу", "Повернення від постачальника", "Інше надходження"]
NON_MONEY = ("бонус", "баланс", "депозит", "сертиф", "абонем", "подар")      # оплата не грошима
ITEM_TYPES = {"service": "Послуги", "product": "Товари", "certificate": "Сертифікати", "subscription": "Абонементи"}


def is_money(payment_name: str | None) -> bool:
    n = (payment_name or "").lower()
    return not any(w in n for w in NON_MONEY)


def item_name(it: dict) -> str:
    for k in ("item_name", "name", "service_name", "product_name", "title", "good_name"):
        if it.get(k):
            return str(it[k])
    return "—"


def item_amount(it: dict) -> float:
    try:
        return float(it.get("payd_amount") if it.get("payd_amount") not in (None, "") else (it.get("total") or 0))
    except (TypeError, ValueError):
        return 0.0


def _range(d_from: date, d_to: date):
    return datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())


def checks(db: Session, d_from: date, d_to: date) -> list[Check]:
    a, b = _range(d_from, d_to)
    return db.scalars(select(Check).where(Check.deleted.is_(False), Check.date >= a, Check.date <= b)
                      .order_by(Check.date.desc())).all()


def _patients(db: Session, ids) -> dict[int, Patient]:
    ids = [i for i in set(ids) if i]
    return {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_(ids or [0])))}


# ---------------- журнал фіноперацій: лише CRM і CleverBox ----------------

KIND_LABELS = {"income": "Дохід", "expense": "Витрата", "transfer": "Переміщення", "storno": "Сторно"}


@dataclass
class J:
    src: str                 # crm | cb
    num: str                 # «#32263» / «CRM-15»
    at: datetime
    kind: str                # income | expense | transfer | storno
    amount: float
    who: str = ""
    who_link: str | None = None
    basis: str = ""
    article: str = ""
    filial: str = ""
    account: str = ""
    vid: str = ""
    author: str = ""
    comment: str = ""
    money: bool = True
    id: int | None = None
    storned: bool = False
    deleted: bool = False
    is_storno: bool = False

    @property
    def sign(self) -> int:
        return 1 if self.kind == "income" else (-1 if self.kind in ("expense", "storno") else 0)


def journal(db: Session, d_from: date, d_to: date, users: dict | None = None, archive: bool = False) -> list[J]:
    from ..models import CbFinOp
    users = users or {}
    a, b = _range(d_from, d_to)
    out: list[J] = []
    if not archive:
        cb_days = set()
        for x in db.scalars(select(CbFinOp).where(CbFinOp.at >= a, CbFinOp.at <= b)):
            cb_days.add(x.at.date())
            out.append(J("cb", f"#{x.id}" if x.id > 0 else "—", x.at, x.kind, x.amount, who=x.counterparty or "",
                         basis=x.basis or "", article=x.article or "", filial=x.filial or "", account=x.account or "",
                         vid=x.vid or "", author=x.author or "", comment=x.comment or "", money=x.kind != "transfer"))
        cs = [c for c in checks(db, d_from, d_to) if c.date.date() not in cb_days]   # дні без журналу CleverBox — з чеків
        pats = _patients(db, [c.patient_id for c in cs])
        for c in cs:
            p = pats.get(c.patient_id)
            for pay in c.payments or [{"payment_name": "—", "amount": c.amount}]:
                amt = float(pay.get("amount") or 0)
                if not amt:
                    continue
                name = pay.get("payment_name") or "—"
                out.append(J("cb", f"#{c.id}", c.date, "income" if amt > 0 else "expense", abs(amt),
                             who=p.full_name if p else "—", who_link=f"/patients/{p.id}" if p else None, basis=f"Продаж #{c.id}",
                             article="Оплата послуг, товарів", account=name, vid=name, author=c.author or "",
                             money=is_money(name)))
    if not archive:                                     # продажі в CRM (каса)
        from ..models import Sale
        for s in db.scalars(select(Sale).where(Sale.at >= a, Sale.at <= b)):
            who = s.patient.full_name if s.patient else "—"
            link = f"/patients/{s.patient_id}" if s.patient_id else None
            for p in s.payments:
                money = p.method not in ("Бонуси", "Сертифікат")
                out.append(J("crm", f"Ч-{s.id}", s.at, "income", p.amount, who=who, who_link=link, basis=f"Продаж CRM #{s.id}",
                             article="Оплата послуг, товарів", account=p.method, vid=p.method, author=users.get(s.created_by, ""),
                             money=money))
                if s.status == "refunded" and s.refunded_at:
                    out.append(J("crm", f"Ч-{s.id}", s.refunded_at, "expense", p.amount, who=who, who_link=link,
                                 basis=f"Повернення чека #{s.id}", article="Повернення коштів", account=p.method,
                                 comment=s.refund_reason or "", money=money))
    q = select(FinOp).where(FinOp.day >= d_from, FinOp.day <= d_to)
    q = q.where(FinOp.deleted_at.is_not(None)) if archive else q.where(FinOp.deleted_at.is_(None))
    ops = db.scalars(q).all()
    storned = {x.storno_of for x in db.scalars(select(FinOp).where(FinOp.storno_of.is_not(None), FinOp.deleted_at.is_(None)))}
    docs = {d.id: d.name for d in db.scalars(select(Doctor))}
    for x in ops:
        who = x.counterparty or (docs.get(x.doctor_id, "") if x.doctor_id else "")
        basis = f"сторно CRM-{x.storno_of}" if x.storno_of else (f"зарплата за {x.salary_month}" if x.salary_month else "")
        at = datetime.combine(x.day, (x.created_at or datetime.min).time()) if x.created_at else datetime.combine(x.day, datetime.min.time())
        out.append(J("crm", f"CRM-{x.id}", at, x.kind, x.amount, who=who, basis=basis, article=x.category,
                     account=x.cashbox or "", vid=x.cashbox_to or "", author=users.get(x.created_by, ""), comment=x.comment or "",
                     money=x.kind != "transfer", id=x.id, storned=x.id in storned, deleted=bool(x.deleted_at),
                     is_storno=bool(x.storno_of)))
    out.sort(key=lambda r: r.at, reverse=True)
    return out


def journal_totals(rows: list[J], exclude_income: set[str] | None = None) -> dict:
    """exclude_income — статті, які не рахуються в дохід (Налаштування → Звіти), напр. «Внесення коштів в касу»."""
    ex = exclude_income or set()
    t = {"income": 0.0, "expense": 0.0, "transfer": 0.0, "non_money": 0.0, "excluded": 0.0, "n": len(rows)}
    for r in rows:
        if r.kind == "transfer":
            t["transfer"] += r.amount
        elif r.kind == "income" and r.article.strip().lower() in ex:
            t["excluded"] += r.amount
        elif not r.money:
            t["non_money"] += r.amount
        elif r.kind == "income":
            t["income"] += r.amount
        else:
            t["expense"] += r.amount
    t["net"] = round(t["income"] - t["expense"], 2)
    return t


# ---------------- продажі ----------------

def sales_summary(cs: list[Check]) -> dict:
    s = {"revenue": 0.0, "discount": 0.0, "n": len(cs), "by_type": defaultdict(float), "by_pay": defaultdict(float),
         "by_staff": defaultdict(lambda: {"services": 0.0, "products": 0.0, "n": 0}), "money": 0.0}
    for c in cs:
        s["revenue"] += c.amount or 0
        s["discount"] += c.discount or 0
        for it in c.items or []:
            amt = item_amount(it)
            t = ITEM_TYPES.get(it.get("item_type"), "Інше")
            s["by_type"][t] += amt
            st = it.get("initiator_name") or c.author or "—"
            if it.get("item_type") == "service":
                s["by_staff"][st]["services"] += amt
                s["by_staff"][st]["n"] += int(it.get("quantity") or 1)
            elif it.get("item_type") == "product":
                s["by_staff"][st]["products"] += amt
        for pay in c.payments or []:
            s["by_pay"][pay.get("payment_name") or "—"] += float(pay.get("amount") or 0)
            if is_money(pay.get("payment_name")):
                s["money"] += float(pay.get("amount") or 0)
    s["avg"] = round(s["revenue"] / s["n"], 2) if s["n"] else 0
    s["by_type"] = dict(sorted(s["by_type"].items(), key=lambda kv: -kv[1]))
    s["by_pay"] = dict(sorted(s["by_pay"].items(), key=lambda kv: -kv[1]))
    s["by_staff"] = dict(sorted(s["by_staff"].items(), key=lambda kv: -(kv[1]["services"] + kv[1]["products"])))
    return s


def sales_rows(db: Session, cs: list[Check], q: str = "", staff: str = "") -> list[dict]:
    pats = _patients(db, [c.patient_id for c in cs])
    ql = (q or "").strip().lower()
    out = []
    for c in cs:
        p = pats.get(c.patient_id)
        items = [{"name": item_name(it), "type": ITEM_TYPES.get(it.get("item_type"), "Інше"), "qty": it.get("quantity") or 1,
                  "amount": item_amount(it), "staff": it.get("initiator_name") or ""} for it in c.items or []]
        if staff and not any(staff == i["staff"] for i in items) and staff != (c.author or ""):
            continue
        if ql and ql not in (p.full_name.lower() if p else "") and ql not in str(c.id) and \
                not any(ql in i["name"].lower() for i in items) and ql not in ((p.phone or "") if p else ""):
            continue
        out.append({"check": c, "patient": p, "lines": items, "pays": c.payments or []})
    return out


# ---------------- зарплата ----------------

def month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def month_bounds(key: str) -> tuple[date, date]:
    y, m = int(key[:4]), int(key[5:7])
    start = date(y, m, 1)
    end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    return start, end


def months_between(a: str, b: str) -> list[str]:
    out, y, m = [], int(a[:4]), int(a[5:7])
    while f"{y}-{m:02d}" <= b:
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def payroll_start(db: Session) -> str:
    st = db.get(SyncState, "payroll_start")
    return st.value if st and st.value else month_key(date.today())


def set_payroll_start(db: Session, key: str) -> None:
    st = db.get(SyncState, "payroll_start") or SyncState(key="payroll_start")
    st.value, st.updated_at = key, datetime.utcnow()
    db.merge(st)
    db.commit()


def _staff_sales(db: Session, d_from: date, d_to: date, doctors: list[Doctor]) -> dict[tuple[str, int], dict]:
    """(місяць, лікар) → послуги / товари з чеків (за initiator_name позиції)."""
    from ..integrations.cleverbox import match_doctor
    out: dict = defaultdict(lambda: {"services": 0.0, "products": 0.0, "n": 0})
    cache: dict[str, Doctor | None] = {}
    for c in checks(db, d_from, d_to):
        mk = month_key(c.date.date())
        for it in c.items or []:
            nm = it.get("initiator_name") or ""
            if nm not in cache:
                cache[nm] = match_doctor(nm, doctors)
            d = cache[nm]
            if not d:
                continue
            a = out[(mk, d.id)]
            if it.get("item_type") == "service":
                a["services"] += item_amount(it)
                a["n"] += int(it.get("quantity") or 1)
            elif it.get("item_type") == "product":
                a["products"] += item_amount(it)
    from .sales import staff_totals                     # + продажі в CRM
    for k, v in staff_totals(db, d_from, d_to).items():
        a = out[k]
        for kk in ("services", "products", "n"):
            a[kk] += v[kk]
    return out


def payroll(db: Session, month: str, upto_from: str | None = None) -> list[dict]:
    """Нарахування за місяць (і, якщо upto_from, — по всіх місяцях з upto_from для взаєморозрахунків)."""
    doctors = db.scalars(select(Doctor).order_by(Doctor.name)).all()
    rules = {r.doctor_id: r for r in db.scalars(select(SalaryRule))}
    months = months_between(upto_from, month) if upto_from else [month]
    d_from, _ = month_bounds(months[0])
    _, d_to = month_bounds(month)
    sales = _staff_sales(db, d_from, d_to, doctors)
    adjs = defaultdict(list)
    for a in db.scalars(select(SalaryAdj).where(SalaryAdj.month.in_(months))):
        adjs[(a.month, a.doctor_id)].append(a)
    rows = []
    for d in doctors:
        r = rules.get(d.id)
        if not r and not d.active:
            continue
        per = {}
        for mk in months:
            s = sales.get((mk, d.id), {"services": 0.0, "products": 0.0, "n": 0})
            base = (r.base or 0) if r else 0
            ps = (r.pct_services or 0) if r else 0
            pp = (r.pct_products or 0) if r else 0
            adj = sum(x.amount for x in adjs.get((mk, d.id), []))
            acc = round(base + s["services"] * ps / 100 + s["products"] * pp / 100 + adj, 2)
            per[mk] = {"services": round(s["services"], 2), "products": round(s["products"], 2), "n": s["n"], "base": base,
                       "from_services": round(s["services"] * ps / 100, 2), "from_products": round(s["products"] * pp / 100, 2),
                       "adj": round(adj, 2), "adjs": adjs.get((mk, d.id), []), "accrued": acc}
        cur = per[month]
        if not r and not cur["services"] and not cur["products"]:
            continue
        rows.append({"doctor": d, "rule": r, "m": cur, "per": per})
    return rows


def paid_salary(db: Session, months: list[str]) -> dict[tuple[str, int], float]:
    out: dict = defaultdict(float)
    for x in db.scalars(select(FinOp).where(FinOp.deleted_at.is_(None), FinOp.category == "Зарплата",
                                            FinOp.doctor_id.is_not(None))):
        mk = x.salary_month or month_key(x.day)
        if mk in months:
            out[(mk, x.doctor_id)] += x.amount if x.kind == "expense" else -x.amount
    return out


def settlements(db: Session, month: str) -> list[dict]:
    start = min(payroll_start(db), month)
    months = months_between(start, month)
    rows = payroll(db, month, upto_from=start)
    paid = paid_salary(db, months)
    out = []
    for r in rows:
        did = r["doctor"].id
        opening = round(sum(r["per"][mk]["accrued"] - paid.get((mk, did), 0) for mk in months[:-1]), 2)
        acc, pd = r["m"]["accrued"], round(paid.get((month, did), 0), 2)
        out.append({**r, "opening": opening, "accrued": acc, "paid": pd, "closing": round(opening + acc - pd, 2)})
    return out


# ---------------- баланси й бонуси ----------------

def balances(db: Session, q: str = "") -> list[Patient]:
    rows = db.scalars(select(Patient).where(Patient.cb_balance.is_not(None), Patient.cb_balance != 0)).all()
    ql = q.strip().lower()
    if ql:
        rows = [p for p in rows if ql in p.full_name.lower() or ql in (p.phone or "")]
    return sorted(rows, key=lambda p: -(p.cb_balance or 0))


def bonuses(db: Session, d_from: date, d_to: date, q: str = "") -> dict:
    holders = db.scalars(select(Patient).where(Patient.cb_bonus.is_not(None), Patient.cb_bonus > 0)).all()
    ql = q.strip().lower()
    if ql:
        holders = [p for p in holders if ql in p.full_name.lower() or ql in (p.phone or "")]
    spends = []
    cs = checks(db, d_from, d_to)
    pats = _patients(db, [c.patient_id for c in cs])
    for c in cs:
        for pay in c.payments or []:
            if "бонус" in (pay.get("payment_name") or "").lower():
                p = pats.get(c.patient_id)
                if ql and not (p and (ql in p.full_name.lower() or ql in (p.phone or ""))):
                    continue
                spends.append({"at": c.date, "patient": p, "amount": float(pay.get("amount") or 0), "check": c.id,
                               "total": c.amount, "name": pay.get("payment_name")})
    return {"holders": sorted(holders, key=lambda p: -(p.cb_bonus or 0)), "spends": spends,
            "total": round(sum(p.cb_bonus or 0 for p in holders), 2), "spent": round(sum(x["amount"] for x in spends), 2)}
