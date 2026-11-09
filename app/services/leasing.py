"""Лізинг і розтермінування обладнання (звіт лише для власника): скільки ще платити, графік, привʼязка до платіжного календаря.

Договір (SyncState «leasing»): назва, група (інвертор / планшет / OBSERV), повна сума, сплачено до CRM, щомісячний платіж,
дата наступного платежу. Для «instalment» майбутні платежі створюються в платіжному календарі (PayPlan + PayItem, група «Кредити»);
оплата в календарі = фіноперація, і звіт бачить її сам. Для «fund» (OBSERV) графіка немає — гроші накопичуються амортизацією в банці monobank.
"""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PayItem, PayPlan, SyncState

KEY = "leasing"
SEED_KEY = "leasing_seed_v1"
ARTICLE = "Зобовʼязання Розтермінування MONOBANK"
GROUPS = {"inverter": "Інвертор Deye", "tablet": "Планшет", "observ": "OBSERV 520x", "other": "Інше обладнання"}
KINDS = {"instalment": "Розтермінування / лізинг", "fund": "Погашення з фонду амортизації"}
OLD_PLANS = ("Лізинг обладнання · інвертор", "Лізинг обладнання · батареї")

# Дані з таблиці власника «Інвектор» (09.11.2026): сплачено до CRM і наступний платіж.
SEED = [
    {"name": "Інвертор Deye", "group": "inverter", "kind": "instalment", "total": 79049, "paid_before": 55328, "monthly": 7904, "next": "2026-12-08", "bank": "monobank · 10 платежів", "start": "2026-05-08"},
    {"name": "Батарея Deye · 1", "group": "inverter", "kind": "instalment", "total": 40000, "paid_before": 28000, "monthly": 4000, "next": "2026-12-10", "bank": "monobank · 10 платежів", "start": "2026-05-10"},
    {"name": "Батарея Deye · 2", "group": "inverter", "kind": "instalment", "total": 40000, "paid_before": 24000, "monthly": 4000, "next": "2026-11-14", "bank": "monobank · 10 платежів", "start": "2026-05-14"},
    {"name": "Батарея Deye · 3", "group": "inverter", "kind": "instalment", "total": 42750, "paid_before": 12825, "monthly": 4275, "next": "2026-11-14", "bank": "monobank · 10 платежів", "start": "2026-08-14"},
    {"name": "Планшет", "group": "tablet", "kind": "instalment", "total": 56000, "paid_before": 26000, "monthly": 10000, "next": "2026-12-10", "bank": "", "start": "2026-03-01",
     "note": "Червень–листопад не сплачувались — графік з грудня, 10 000 / міс"},
    {"name": "OBSERV 520x", "group": "observ", "kind": "fund", "total": 630000, "paid_before": 18950, "monthly": None, "next": None, "bank": "", "start": "2026-06-01",
     "note": "Погашається з амортизації, яка накопичується в банці monobank «OBSERV 520x»"},
]


def _d(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _add_months(d: date, k: int, day: int | None = None) -> date:
    y, m = divmod(d.month - 1 + k, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(day or d.day, calendar.monthrange(y, m)[1]))


def contracts(db: Session) -> list[dict]:
    st = db.get(SyncState, KEY)
    try:
        return json.loads(st.value) if st and st.value else []
    except ValueError:
        return []


def _save(db: Session, xs: list[dict]) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(xs, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def schedule(total: float, paid: float, monthly: float | None, first: date | None) -> list[tuple[date, float]]:
    """Решта платежів: по monthly щомісяця з first, останній — залишок."""
    left = round(float(total or 0) - float(paid or 0), 2)
    if left <= 0 or not monthly or not first:
        return []
    n = max(1, round(left / float(monthly)))         # дрібний залишок — в останній платіж, а не окремим місяцем
    out = []
    for k in range(min(n, 240)):
        a = round(float(monthly), 2) if k < n - 1 else round(left - float(monthly) * (n - 1), 2)
        out.append((_add_months(first, k, first.day), a))
    return out


def sync_calendar(db: Session, c: dict, today: date | None = None) -> int:
    """Шаблон у платіжному календарі + усі майбутні платежі договору (без дублів; оплачені не чіпає)."""
    if c.get("kind") != "instalment":
        return 0
    today = today or date.today()
    plan = db.get(PayPlan, c.get("plan_id")) if c.get("plan_id") else None
    items = db.scalars(select(PayItem).where(PayItem.plan_id == plan.id)).all() if plan else []
    paid_crm = sum(float(i.amount_fact or i.amount or 0) for i in items if i.status == "paid")
    first = _d(c.get("next"))
    sch = schedule(c["total"], float(c.get("paid_before") or 0) + paid_crm, c.get("monthly"), first)
    if not plan:
        plan = PayPlan(name=f"Лізинг · {c['name']}", group="debt", article=ARTICLE, counterparty=c.get("bank") or None, amount=c.get("monthly"),
                       schedule="once", day=first.day if first else None, start=first, active=False, needs_approval=False,
                       note="Графік — зі звіту «Лізинг обладнання»", sort=900)
        db.add(plan)
        db.flush()
        c["plan_id"] = plan.id
    plan.name, plan.amount, plan.counterparty = f"Лізинг · {c['name']}", c.get("monthly"), c.get("bank") or None
    plan.start, plan.end = (sch[0][0], sch[-1][0]) if sch else (None, today)
    plan.active = False                                 # платежі створює звіт, не шаблон (інакше дубль при перенесенні дати)
    open_ = {i.due: i for i in items if i.status in ("planned", "approved")}
    want = dict(sch)
    n = 0
    for due, it in open_.items():                       # неоплачені поза новим графіком — пропустити (не видаляємо)
        if due not in want:
            it.status, it.note = "skipped", "Графік лізингу змінено"
    have = {i.due for i in items if i.status != "skipped"}
    for due, amt in sch:
        it = open_.get(due)
        if it:
            it.amount, it.name = amt, plan.name
            continue
        if due in have:
            continue
        old = next((i for i in items if i.due == due and i.status == "skipped"), None)
        if old:                                         # унікальність (plan_id, due) — повертаємо пропущений
            old.status, old.amount, old.note = "approved", amt, None
            continue
        db.add(PayItem(plan_id=plan.id, due=due, name=plan.name, group="debt", article=ARTICLE, counterparty=c.get("bank") or None,
                       amount=amt, status="approved", source="plan", ref=f"leasing:{c['id']}"))
        n += 1
    db.commit()
    return n


def ensure_setup(db: Session) -> bool:
    """Перший запуск: договори з таблиці власника, графіки в календар, старі загальні шаблони лізингу вимкнути."""
    if db.get(SyncState, SEED_KEY):
        return False
    xs = contracts(db)
    if not xs:
        xs = [dict(x, id=i + 1) for i, x in enumerate(SEED)]
    for p in db.scalars(select(PayPlan).where(PayPlan.name.in_(OLD_PLANS))):
        p.active = False
        for it in db.scalars(select(PayItem).where(PayItem.plan_id == p.id, PayItem.status.in_(("planned", "approved")))):
            it.status, it.note = "skipped", "Замінено графіком зі звіту «Лізинг обладнання»"
    for c in xs:
        sync_calendar(db, c)
    _save(db, xs)
    db.merge(SyncState(key=SEED_KEY, value=datetime.now().isoformat(timespec="minutes"), updated_at=datetime.utcnow()))
    db.commit()
    return True


def save(db: Session, cid: int | None, f: dict) -> dict:
    xs = contracts(db)
    c = next((x for x in xs if x["id"] == cid), None) if cid else None
    if not c:
        c = {"id": max([x["id"] for x in xs] + [0]) + 1}
        xs.append(c)

    def num(k):
        try:
            return round(float(str(f.get(k) or "").replace(" ", "").replace(",", ".")), 2) if str(f.get(k) or "").strip() else None
        except ValueError:
            return None
    c.update(name=(f.get("name") or c.get("name") or "Договір").strip()[:120], group=f.get("group") if f.get("group") in GROUPS else c.get("group", "other"),
             kind=f.get("kind") if f.get("kind") in KINDS else c.get("kind", "instalment"), total=num("total") or 0, paid_before=num("paid_before") or 0,
             monthly=num("monthly"), next=(_d(f.get("next")) or None) and str(_d(f.get("next"))), bank=(f.get("bank") or "").strip()[:80],
             note=(f.get("note") or "").strip()[:300], start=c.get("start"))
    sync_calendar(db, c)
    _save(db, xs)
    return c


def _jar_balance(db: Session) -> dict:
    """Скільки лежить у банці OBSERV: залишок monobank (якщо підключено), інакше — каса CRM «Банка OBSERV 520x»."""
    from . import amort, cashboxes as cbx
    from ..integrations import monobank
    try:
        j = monobank.jar(db, amort.OBSERV_JAR, refresh=False)
    except Exception:  # noqa: BLE001
        j = None
    if j and j.get("balance") is not None:
        return {"balance": float(j["balance"]), "src": f"банка monobank · {j.get('at') or ''}".strip(" ·")}
    box = next((b for b in cbx.overview(db) if b["name"].lower() == amort.OBSERV_BOX.lower()), None)
    return {"balance": float(box["balance"]) if box else 0.0, "src": "каса CRM «Банка OBSERV 520x»"}


def build(db: Session, today: date | None = None) -> dict:
    today = today or date.today()
    rows = []
    for c in contracts(db):
        items = db.scalars(select(PayItem).where(PayItem.plan_id == c["plan_id"]).order_by(PayItem.due)).all() if c.get("plan_id") else []
        paid_crm = round(sum(float(i.amount_fact or i.amount or 0) for i in items if i.status == "paid"), 2)
        paid = round(float(c.get("paid_before") or 0) + paid_crm, 2)
        left = round(max(float(c.get("total") or 0) - paid, 0), 2)
        sch = [{"due": i.due, "amount": float(i.amount or 0), "status": i.status, "id": i.id, "paid_at": i.paid_at,
                "fact": i.amount_fact, "overdue": i.status in ("planned", "approved") and i.due < today} for i in items if i.status != "skipped"]
        rest = [s for s in sch if s["status"] in ("planned", "approved")]
        r = {**c, "paid": paid, "paid_crm": paid_crm, "left": left, "pct": round(100 * paid / c["total"], 1) if c.get("total") else 0,
             "sched": sch, "n_left": len(rest), "end": rest[-1]["due"] if rest else None, "next_pay": rest[0] if rest else None,
             "overdue": [s for s in rest if s["overdue"]], "group_t": GROUPS.get(c.get("group"), c.get("group")), "kind_t": KINDS.get(c.get("kind")),
             "closed": bool(c.get("total")) and left <= 0 and not rest,
             "closed_at": max((s["paid_at"] or s["due"] for s in sch if s["status"] == "paid"), default=None),
             "payments": [{"day": s["paid_at"] or s["due"], "amount": float(s["fact"] or s["amount"] or 0), "due": s["due"], "id": s["id"]}
                          for s in sch if s["status"] == "paid"]}
        if c.get("kind") == "fund":
            j = _jar_balance(db)
            r["jar"], r["jar_src"] = round(j["balance"], 2), j["src"]
            r["cover"] = round(100 * j["balance"] / left, 1) if left else 100.0
        rows.append(r)
    history = sorted([r for r in rows if r["closed"]], key=lambda r: r["closed_at"] or date.min, reverse=True)
    pay_hist = sorted([dict(p, name=r["name"], group_t=r["group_t"]) for r in rows for p in r["payments"]], key=lambda p: p["day"], reverse=True)
    groups = []
    for g, t in GROUPS.items():
        gr = [r for r in rows if r.get("group") == g and not r["closed"]]
        if gr:
            groups.append({"key": g, "title": t, "rows": gr, "total": round(sum(r["total"] for r in gr), 2), "paid": round(sum(r["paid"] for r in gr), 2),
                           "left": round(sum(r["left"] for r in gr), 2), "end": max((r["end"] for r in gr if r["end"]), default=None),
                           "fund": all(r.get("kind") == "fund" for r in gr)})
    inst = [r for r in rows if r.get("kind") == "instalment" and not r["closed"]]
    months: dict = {}
    for r in inst:
        for s in r["sched"]:
            if s["status"] in ("planned", "approved"):
                k = (s["due"].year, s["due"].month)
                months.setdefault(k, {"total": 0.0, "by": {}})
                months[k]["total"] += s["amount"]
                months[k]["by"][r["id"]] = months[k]["by"].get(r["id"], 0) + s["amount"]
    this = months.get((today.year, today.month), {}).get("total", 0.0)
    return {"rows": rows, "groups": groups, "inst": inst, "months": sorted(months.items()), "this_month": round(this, 2),
            "left": round(sum(r["left"] for r in inst), 2), "left_fund": round(sum(r["left"] for r in rows if r.get("kind") == "fund"), 2),
            "paid": round(sum(r["paid"] for r in rows), 2), "total": round(sum(r["total"] for r in rows), 2),
            "end": max((r["end"] for r in inst if r["end"]), default=None),
            "overdue": [(r, s) for r in inst for s in r["overdue"]], "today": today, "GROUPS": GROUPS, "KINDS": KINDS,
            "history": history, "pay_hist": pay_hist, "pay_hist_sum": round(sum(p["amount"] for p in pay_hist), 2)}
