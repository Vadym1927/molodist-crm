"""Платіжний календар (/finance/calendar) — що і коли треба заплатити, звідки, чи погоджено, чи оплачено, і чи вистачить грошей.

Ідеї з таблиці власника «OPEX Calendar»: залишки по рахунках на початок, список платежів за групами (OPEX, маркетинг, ФОТ,
податки, борги) з платником, плановою сумою, дозволом на оплату, плановою й фактичною датою, відхиленням; плановий прихід.
У CRM це не таблиця на місяць, а шаблони регулярних платежів → платежі на дати (генеруються автоматично), рахунки постачальників
на погодженні (payreq) і разові платежі; оплата = фіноперація CRM (зіставляється автоматично за статтею / рахунком / датою),
залишки — з кас CRM, очікувані надходження — із записів у календарі."""
from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FinOp, PayItem, PayPlan

GROUPS = [("opex", "Операційні витрати"), ("marketing", "Маркетинг і реклама"), ("fot", "Фонд оплати праці"), ("tax", "Податки й юридичні"),
          ("debt", "Кредити й зобовʼязання"), ("purchase", "Закупівлі"), ("capex", "Обладнання й ремонт")]
GROUP_T = dict(GROUPS)
SCHEDULES = {"monthly": "щомісяця", "twice": "двічі на місяць", "weekly": "щотижня", "quarterly": "щокварталу", "once": "разово"}
STATUSES = {"planned": "Заплановано", "approved": "Погоджено", "paid": "Оплачено", "skipped": "Пропущено"}
MATCH_BEFORE, MATCH_AFTER = 7, 10          # фіноперація за статтею в цьому вікні навколо планової дати = оплата
MONTHS = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень", "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def _clamp(y: int, m: int, d: int) -> date:
    return date(y, m, min(max(int(d or 1), 1), calendar.monthrange(y, m)[1]))


def plan_dates(p: PayPlan, y: int, m: int) -> list[date]:
    """Дати платежу за шаблоном у місяці."""
    first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    if p.start and p.start > last:
        return []
    if p.end and p.end < first:
        return []
    out: list[date] = []
    if p.schedule == "once":
        if p.start and first <= p.start <= last:
            out = [p.start]
    elif p.schedule == "weekly":
        wd = p.weekday if p.weekday is not None else 0
        d = first
        while d <= last:
            if d.weekday() == wd:
                out.append(d)
            d += timedelta(days=1)
    elif p.schedule == "quarterly":
        if m in (1, 4, 7, 10):
            out = [_clamp(y, m, p.day or 1)]
    elif p.schedule == "twice":
        out = [_clamp(y, m, p.day or 15), _clamp(y, m, p.day2 or 30)]
    else:
        out = [_clamp(y, m, p.day or 1)]
    return [d for d in out if (not p.start or d >= p.start) and (not p.end or d <= p.end)]


def ensure_month(db: Session, y: int, m: int) -> int:
    """Створює платежі місяця з активних шаблонів (без дублів) і підтягує рахунки постачальників на погодженні."""
    have = {(i.plan_id, i.due) for i in db.scalars(select(PayItem).where(PayItem.plan_id.is_not(None),
                                                                           PayItem.due >= date(y, m, 1), PayItem.due <= date(y, m, calendar.monthrange(y, m)[1])))}
    n = 0
    for p in db.scalars(select(PayPlan).where(PayPlan.active.is_(True))):
        for d in plan_dates(p, y, m):
            if (p.id, d) in have:
                continue
            db.add(PayItem(plan_id=p.id, due=d, name=p.name, group=p.group, article=p.article, cashbox=p.cashbox, counterparty=p.counterparty,
                           amount=p.amount, status="planned" if p.needs_approval else "approved", source="plan"))
            n += 1
    n += _pull_payreqs(db)
    if n:
        db.commit()
    return n


def _pull_payreqs(db: Session) -> int:
    """Рахунки постачальників (/payreq), ще не оплачені, — у календар на сьогодні (або дату рахунку + 3 дні)."""
    from ..models import PayRequest
    have = {i.ref for i in db.scalars(select(PayItem).where(PayItem.ref.like("payreq:%")))}
    n = 0
    for pr in db.scalars(select(PayRequest).where(PayRequest.status.in_(("new", "approved", "draft")))):
        ref = f"payreq:{pr.id}"
        if ref in have:
            continue
        due = max(date.today(), (pr.invoice_date + timedelta(days=3)) if pr.invoice_date else date.today())
        db.add(PayItem(due=due, name=f"Рахунок {pr.invoice_no or '№?'} · {pr.receiver_name or pr.payer_name or 'постачальник'}"[:200], group="purchase",
                       article="Закупка товара", cashbox=pr.fop, counterparty=(pr.receiver_name or "")[:200] or None, amount=pr.amount,
                       status="approved" if pr.status in ("approved", "draft") else "planned", source="payreq", ref=ref))
        n += 1
    # рахунки, що вже оплачені / відхилені — закриваємо відповідні платежі
    for it in db.scalars(select(PayItem).where(PayItem.ref.like("payreq:%"), PayItem.status.in_(("planned", "approved")))):
        pr = db.get(PayRequest, int(it.ref.split(":")[1]))
        if pr and pr.status == "paid":
            it.status, it.paid_at, it.amount_fact = "paid", pr.decided_at.date() if pr.decided_at else date.today(), pr.amount
            n += 1
        elif pr and pr.status in ("declined", "rejected"):
            it.status = "skipped"
            n += 1
    return n


def match_paid(db: Session, items: list[PayItem]) -> int:
    """Незакриті платежі ↔ фіноперації CRM: та сама стаття (і рахунок, якщо вказано) у вікні дат, одна операція — один платіж."""
    used = {i.finop_id for i in db.scalars(select(PayItem).where(PayItem.finop_id.is_not(None)))}
    open_ = [i for i in items if i.status in ("planned", "approved") and i.article]
    if not open_:
        return 0
    lo = min(i.due for i in open_) - timedelta(days=MATCH_BEFORE)
    hi = max(i.due for i in open_) + timedelta(days=MATCH_AFTER)
    ops = db.scalars(select(FinOp).where(FinOp.deleted_at.is_(None), FinOp.kind == "expense", FinOp.day >= lo, FinOp.day <= hi,
                                         FinOp.storno_of.is_(None)).order_by(FinOp.day)).all()
    n = 0
    for it in sorted(open_, key=lambda i: i.due):
        best = None
        for op in ops:
            if op.id in used or (op.category or "").strip().lower() != (it.article or "").strip().lower():
                continue
            if it.cashbox and op.cashbox and op.cashbox != it.cashbox:
                continue
            if not (it.due - timedelta(days=MATCH_BEFORE) <= op.day <= it.due + timedelta(days=MATCH_AFTER)):
                continue
            score = (abs((op.day - it.due).days), abs(float(op.amount or 0) - float(it.amount or op.amount or 0)))
            if best is None or score < best[0]:
                best = (score, op)
        if best:
            op = best[1]
            it.status, it.finop_id, it.paid_at, it.amount_fact = "paid", op.id, op.day, float(op.amount or 0)
            used.add(op.id)
            n += 1
    if n:
        db.commit()
    return n


def pay(db: Session, it: PayItem, amount: float, day: date, cashbox: str | None, user_id: int | None, comment: str = "") -> FinOp:
    """Кнопка «Оплачено»: створює витрату в журналі й закриває платіж (амортизація / зарплата — окремо, див. pay_dynamic)."""
    if (it.name or "").startswith("OBSERV → банка"):
        from . import amort
        op = FinOp(day=day, kind="transfer", category="Погашення OBSERV", amount=round(float(amount), 2), cashbox=(cashbox or it.cashbox or None),
                   cashbox_to=amort.OBSERV_BOX, counterparty=amort.OBSERV_BOX, comment=(comment or f"Платіжний календар: {it.name}")[:500],
                   created_by=user_id, ref=f"paycal:{it.id}")
        db.add(op)
        db.flush()
        it.status, it.finop_id, it.paid_at, it.amount_fact = "paid", op.id, day, op.amount
        if cashbox:
            it.cashbox = cashbox
        db.commit()
        return op
    if dyn_kind(it) == "salary_one":
        from .fin_articles import salary_names
        sal = it.article if it.article in salary_names(db) else salary_names(db)[0]
        did = int((it.ref or "").split(":")[2])
        op = FinOp(day=day, kind="expense", category=sal[:60], amount=round(float(amount), 2), cashbox=(cashbox or it.cashbox or None), doctor_id=did,
                   counterparty=(it.counterparty or None), comment=(comment or f"Платіжний календар: {it.name}")[:500], created_by=user_id, ref=f"paycal:{it.id}")
        db.add(op)
        db.flush()
        it.status, it.finop_id, it.paid_at, it.amount_fact = "paid", op.id, day, op.amount
        if cashbox:
            it.cashbox = cashbox
        db.commit()
        return op
    if dyn_kind(it) in ("amort_device", "amort_cons"):
        op = pay_dynamic(db, it, amount, day, cashbox, user_id, comment)
        if op or it.status == "paid":
            return op
    op = FinOp(day=day, kind="expense", category=(it.article or "Інше")[:60], amount=round(float(amount), 2), cashbox=(cashbox or it.cashbox or None),
               counterparty=(it.counterparty or None), comment=(comment or f"Платіжний календар: {it.name}")[:500], created_by=user_id,
               ref=f"paycal:{it.id}")
    db.add(op)
    db.flush()
    it.status, it.finop_id, it.paid_at, it.amount_fact = "paid", op.id, day, op.amount
    if cashbox:
        it.cashbox = cashbox
    db.commit()
    return op


def expected_income(db: Session, d_from: date, d_to: date) -> dict:
    """Очікувані надходження: записи в календарі (не скасовані) × ціни послуг, по днях."""
    from ..models import Appointment, CbService
    a, b = datetime.combine(d_from, datetime.min.time()), datetime.combine(d_to, datetime.max.time())
    prices = {s.id: float(s.price or 0) for s in db.scalars(select(CbService))}
    by_day: dict[date, float] = defaultdict(float)
    n = 0
    for ap in db.scalars(select(Appointment).where(Appointment.start >= a, Appointment.start <= b, Appointment.status.notin_(("cancelled", "no_show")))):
        tot = 0.0
        for s in ap.services or []:
            try:
                tot += float(s.get("price") or prices.get(int(s.get("id") or 0), 0) or 0)
            except (TypeError, ValueError):
                pass
        if tot:
            by_day[ap.start.date()] += tot
            n += 1
    return {"by_day": dict(by_day), "total": round(sum(by_day.values()), 2), "n": n}


INC_PLAN_KEY = "paycal_income_plan"


def income_plans(db: Session) -> dict[str, float]:
    """Ручний план приходу на тиждень: {"2026-11-02": 250000} (ключ — понеділок тижня)."""
    import json
    from ..models import SyncState
    st = db.get(SyncState, INC_PLAN_KEY)
    try:
        return {k: float(v) for k, v in (json.loads(st.value) if st and st.value else {}).items()}
    except (ValueError, TypeError):
        return {}


def set_income_plan(db: Session, week_start: date, amount: float | None) -> None:
    import json
    from ..models import SyncState
    d = income_plans(db)
    k = week_start.isoformat()
    if amount is None:
        d.pop(k, None)
    else:
        d[k] = float(amount)
    db.merge(SyncState(key=INC_PLAN_KEY, value=json.dumps(d), updated_at=datetime.utcnow()))
    db.commit()


def income_fact(db: Session, d_from: date, d_to: date) -> dict[date, float]:
    """Фактичний прихід грошима по днях: надходження журналу (чеки CleverBox / каса CRM / доходи CRM), без переміщень,
    оплат не грошима і статей «не в дохід»."""
    from . import finops as fo, clinic
    ex = clinic.excluded_income(db)
    out: dict[date, float] = defaultdict(float)
    for r in fo.journal(db, d_from, d_to):
        if r.kind == "income" and r.money and (r.article or "").strip().lower() not in ex:
            out[r.at.date()] += float(r.amount or 0)
        elif r.kind == "storno" and r.money:
            out[r.at.date()] -= float(r.amount or 0)
    return dict(out)


def shifts_by_day(db: Session, d_from: date, d_to: date) -> dict[date, dict]:
    """Закриті касові зміни: день закриття → {n, total}."""
    from ..models import CashShift
    out: dict[date, dict] = defaultdict(lambda: {"n": 0, "total": 0.0})
    for sh in db.scalars(select(CashShift).where(CashShift.closed_at.is_not(None), CashShift.closed_at >= datetime.combine(d_from, datetime.min.time()),
                                                 CashShift.closed_at <= datetime.combine(d_to, datetime.max.time()))):
        x = out[sh.closed_at.date()]
        x["n"] += 1
        x["total"] += float((sh.summary or {}).get("total") or 0)
    return dict(out)


def move(db: Session, it: PayItem, due: date) -> None:
    """Перенести платіж на іншу дату (і в інший місяць): шаблон не змінюється, лише цей платіж."""
    it.due = due
    db.commit()


def _fop_of(name: str) -> str | None:
    m = re.search(r"ФОП\s+([А-ЯІЇЄҐA-Z][\wʼ'’-]+)", name or "")
    return m.group(1) if m else None


def money_now(db: Session, boxes: list[dict] | None = None, mono_balances=None) -> dict:
    """Гроші зараз для шапки календаря: рахунки monobank (залишок у банку), готівка по ФОП (каси CRM) і сейф."""
    from . import cashboxes as cbx
    from ..integrations import monobank
    boxes = boxes if boxes is not None else [b for b in cbx.overview(db) if b.get("active", True) is not False and not b.get("deleted")]
    by_name = {b["name"]: b for b in boxes}
    if mono_balances is None:
        try:
            mono_balances = monobank.balances(db)
        except Exception:  # noqa: BLE001
            mono_balances = {}
    m = monobank.cashbox_map(db)
    mono, seen = [], set()
    for x in monobank.saved(db):
        watch = set(x.get("watch") or [])
        for a in x.get("accounts", []):
            box = m.get(monobank.account_key(a)) or m.get(a.get("id"))
            if a.get("id") in seen or (a.get("id") not in watch and not box) or a.get("currency", 980) != 980:
                continue
            seen.add(a.get("id"))
            b = mono_balances.get(a.get("id")) or {}
            crm = by_name.get(box, {}).get("balance") if box else None
            bal = b.get("balance")
            mono.append({"name": box or f"Рахунок …{(a.get('iban') or '')[-4:]}", "fop": x.get("fop"), "iban4": (a.get("iban") or "")[-4:],
                         "balance": bal, "at": b.get("at"), "crm": crm,
                         "diff": round(bal - crm, 2) if bal is not None and crm is not None and abs(bal - crm) >= 1 else None})
    if not mono:                                   # monobank не підключено — рахунки за даними CRM
        mono = [{"name": b["name"], "fop": _fop_of(b["name"]), "iban4": "", "balance": round(float(b.get("balance") or 0), 2), "at": None,
                 "crm": None, "diff": None, "crm_only": True} for b in boxes if b.get("type") == "bank"]
    cash_fop: dict = defaultdict(list)
    safe = []
    for b in boxes:
        if b.get("type") != "cash":
            continue
        row = {"name": b["name"], "balance": round(float(b.get("balance") or 0), 2), "last": b.get("last")}
        if re.search(r"[сc]ейф", b["name"], re.I):
            safe.append(row)
        else:
            cash_fop[_fop_of(b["name"]) or "Інше"].append(row)
    cash = [{"fop": k, "rows": v, "total": round(sum(r["balance"] for r in v), 2)} for k, v in sorted(cash_fop.items(), key=lambda kv: (kv[0] == "Інше", kv[0]))]
    mono_total = round(sum(r["balance"] or 0 for r in mono), 2)
    cash_total = round(sum(c["total"] for c in cash), 2)
    safe_total = round(sum(r["balance"] for r in safe), 2)
    return {"mono": mono, "mono_total": mono_total, "mono_ok": any(r["balance"] is not None for r in mono), "mono_crm": bool(mono) and all(r.get("crm_only") for r in mono),
            "cash": cash, "cash_total": cash_total, "safe": safe, "safe_total": safe_total,
            "total": round(mono_total + cash_total + safe_total, 2)}



def month_view(db: Session, y: int, m: int, group: str = "", status: str = "", today: date | None = None) -> dict:
    from . import cashboxes as cbx
    today = today or date.today()
    ensure_month(db, y, m)
    first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    items = db.scalars(select(PayItem).where(PayItem.due >= first - timedelta(days=45), PayItem.due <= last).order_by(PayItem.due, PayItem.id)).all()
    match_paid(db, [i for i in items if not dyn_kind(i)])
    refresh_dynamic(db, items, today)
    items = db.scalars(select(PayItem).where(PayItem.due >= first - timedelta(days=45), PayItem.due <= last,
                                             (PayItem.ref.is_(None)) | (PayItem.ref != "split-parent")).order_by(PayItem.due, PayItem.id)).all()
    overdue = [i for i in items if i.due < first and i.status in ("planned", "approved")]      # хвости минулих місяців
    cur = [i for i in items if first <= i.due <= last]
    shown = cur
    if group:
        shown = [i for i in shown if i.group == group]
    if status:
        shown = [i for i in shown if (i.status == status if status != "overdue" else (i.status in ("planned", "approved") and i.due < today))]
    # тижні: витрати (план / оплачено / лишилось) і прихід (план / факт за закриттям кас)
    w0 = first - timedelta(days=first.weekday())
    w_end = last + timedelta(days=6 - last.weekday())
    exp_all = expected_income(db, w0, w_end)["by_day"]                  # записи × ціна — план приходу (усі дні тижня, й минулі)
    fact_all = income_fact(db, w0, min(w_end, today))
    shifts_all = shifts_by_day(db, w0, min(w_end, today))
    manual = income_plans(db)
    weeks: list[dict] = []
    d = w0
    while d <= last:
        e = d + timedelta(days=6)
        wk_items = [i for i in shown if d <= i.due <= e]
        days = [d + timedelta(days=k) for k in range(7)]
        auto = round(sum(exp_all.get(x, 0.0) for x in days), 2)
        man = manual.get(d.isoformat())
        fact = round(sum(fact_all.get(x, 0.0) for x in days if x <= today), 2)
        sh_n = sum(shifts_all.get(x, {}).get("n", 0) for x in days)
        sh_t = round(sum(shifts_all.get(x, {}).get("total", 0.0) for x in days), 2)
        inc_plan = man if man is not None else auto
        weeks.append({"start": d, "end": e, "rows": wk_items,
                      "plan": round(sum(float(i.amount or 0) for i in wk_items if i.status != "skipped"), 2),
                      "paid": round(sum(float(i.amount_fact or i.amount or 0) for i in wk_items if i.status == "paid"), 2),
                      "left": round(sum(float(i.amount or 0) for i in wk_items if i.status in ("planned", "approved")), 2),
                      "current": d <= today <= e, "past": e < today, "future": d > today,
                      "inc_auto": auto, "inc_manual": man, "inc_plan": round(inc_plan, 2), "inc_fact": fact,
                      "inc_left": round(sum(exp_all.get(x, 0.0) for x in days if x > today), 2),
                      "inc_pct": round(100 * fact / inc_plan, 1) if inc_plan and d <= today else None,
                      "shifts": sh_n, "shifts_total": sh_t,
                      "net_plan": round(inc_plan - sum(float(i.amount or 0) for i in wk_items if i.status != "skipped"), 2),
                      "net_fact": round(fact - sum(float(i.amount_fact or i.amount or 0) for i in wk_items if i.status == "paid"), 2) if d <= today else None})
        d += timedelta(days=7)
    inc_month = {"plan": round(sum(w["inc_plan"] for w in weeks), 2), "fact": round(sum(w["inc_fact"] for w in weeks), 2),
                 "left": round(sum(w["inc_left"] for w in weeks if w["end"] >= today), 2)}
    boxes = [b for b in cbx.overview(db) if b.get("active", True) is not False and not b.get("deleted")]
    cash = round(sum(float(b["balance"] or 0) for b in boxes), 2)
    left = round(sum(float(i.amount or 0) for i in cur if i.status in ("planned", "approved")) + sum(float(i.amount or 0) for i in overdue), 2)
    inc = expected_income(db, max(today + timedelta(days=1), first), last) if last > today else {"by_day": {}, "total": 0.0, "n": 0}
    by_group = defaultdict(lambda: {"plan": 0.0, "paid": 0.0, "left": 0.0, "n": 0})
    for i in cur:
        g = by_group[i.group]
        g["n"] += 1
        if i.status != "skipped":
            g["plan"] += float(i.amount or 0)
        if i.status == "paid":
            g["paid"] += float(i.amount_fact or i.amount or 0)
        elif i.status in ("planned", "approved"):
            g["left"] += float(i.amount or 0)
    by_box = defaultdict(float)
    for i in cur + overdue:
        if i.status in ("planned", "approved"):
            by_box[i.cashbox or "— не вказано"] += float(i.amount or 0)
    plans = db.scalars(select(PayPlan).order_by(PayPlan.active.desc(), PayPlan.group, PayPlan.sort, PayPlan.name)).all()
    return {"y": y, "m": m, "first": first, "last": last, "today": today, "rows": cur, "shown": shown, "overdue": overdue, "weeks": weeks,
            "boxes": boxes, "cash": cash, "left": left, "money": money_now(db, boxes), "purchases": purchases(db, first, last), "dyn_kind": dyn_kind, "income": inc, "inc_month": inc_month,
            "forecast": round(cash - left + inc["total"], 2),
            "paid": round(sum(float(i.amount_fact or i.amount or 0) for i in cur if i.status == "paid"), 2),
            "plan": round(sum(float(i.amount or 0) for i in cur if i.status != "skipped"), 2),
            "approved": round(sum(float(i.amount or 0) for i in cur if i.status == "approved"), 2),
            "to_approve": [i for i in cur + overdue if i.status == "planned"],
            "to_approve_sum": round(sum(float(i.amount or 0) for i in cur + overdue if i.status == "planned"), 2),
            "overdue_sum": round(sum(float(i.amount or 0) for i in overdue), 2),
            "due_soon": [i for i in cur + overdue if i.status in ("planned", "approved") and i.due <= today + timedelta(days=7)],
            "by_group": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by_group.items()},
            "by_box": dict(sorted(by_box.items(), key=lambda x: -x[1])), "plans": plans, "group": group, "status": status,
            "GROUPS": GROUPS, "GROUP_T": GROUP_T, "STATUSES": STATUSES, "SCHEDULES": SCHEDULES, "MONTHS": MONTHS, "WEEKDAYS": WEEKDAYS,
            "prev": (y - 1, 12) if m == 1 else (y, m - 1), "next": (y + 1, 1) if m == 12 else (y, m + 1)}


def deviation(it: PayItem) -> float | None:
    if it.status != "paid" or it.amount is None or it.amount_fact is None:
        return None
    return round(float(it.amount_fact) - float(it.amount), 2)


def digest_text(db: Session, today: date | None = None) -> str | None:
    """Ранкове нагадування власнику: що платити сьогодні й що прострочено / чекає погодження."""
    today = today or date.today()
    ensure_month(db, today.year, today.month)
    items = db.scalars(select(PayItem).where(PayItem.due <= today + timedelta(days=2), PayItem.due >= today - timedelta(days=60),
                                             PayItem.status.in_(("planned", "approved"))).order_by(PayItem.due)).all()
    match_paid(db, items)
    items = [i for i in items if i.status in ("planned", "approved")]
    if not items:
        return None
    from html import escape

    def line(i: PayItem) -> str:
        amt = f"{float(i.amount):,.0f} ₴".replace(",", " ") if i.amount else "сума не вказана"
        return f"• {escape(i.name)} — {amt}{' · ' + escape(i.cashbox) if i.cashbox else ''}{' · ⏳ погодити' if i.status == 'planned' else ''}"
    over = [i for i in items if i.due < today]
    tod = [i for i in items if i.due == today]
    soon = [i for i in items if today < i.due <= today + timedelta(days=2)]
    parts = ["💳 <b>Платіжний календар</b>"]
    if over:
        parts.append(f"\n🔴 Прострочено ({len(over)}):\n" + "\n".join(line(i) + f" · {i.due:%d.%m}" for i in over[:10]))
    if tod:
        parts.append(f"\n📅 Сьогодні ({len(tod)}):\n" + "\n".join(line(i) for i in tod[:15]))
    if soon:
        parts.append(f"\n🔜 Найближчі 2 дні:\n" + "\n".join(line(i) + f" · {i.due:%d.%m}" for i in soon[:10]))
    tot = sum(float(i.amount or 0) for i in over + tod)
    if tot:
        parts.append(f"\nРазом до оплати: <b>{tot:,.0f} ₴</b>".replace(",", " "))
    return "\n".join(parts)


SEED_KEY = "paycal_seeded_v1"


def seed_plans(db: Session) -> int:
    """Перший запуск: типові регулярні платежі клініки (з «OPEX Calendar» власника) — лише назви, групи, дні й статті; суми вказує власник."""
    from ..models import SyncState
    if db.get(SyncState, SEED_KEY) or db.scalar(select(PayPlan.id).limit(1)):
        return 0
    from .fin_articles import names as _names
    arts = set(_names(db, "expense", active_only=False))

    def art(*cands):
        for c in cands:
            if c in arts:
                return c
        return cands[0]
    rows = [
        ("Оренда приміщення клініки", "opex", art("Оренда", "Оренда приміщення"), "monthly", 13, None, 30060.0, True),
        ("Комунальні послуги", "opex", art("Комунальні", "Господарські Комунальні"), "monthly", 13, None, None, False),
        ("Інша оренда (зберігання)", "opex", art("Оренда", "Інша оренда"), "monthly", 14, None, 1000.0, False),
        ("CleverBox", "opex", art("Програми CleverBox", "Сервіси та підписки"), "monthly", 8, None, 2934.66, False),
        ("Helsi", "opex", art("Сервіси та підписки Helsi", "Сервіси та підписки"), "monthly", 6, None, 850.0, False),
        ("Shop-Express", "opex", art("Сервіси та підписки Shop-Express", "Сервіси та підписки"), "monthly", 8, None, 1522.5, False),
        ("Tilda", "opex", art("Сервіси та підписки Tilda", "Сервіси та підписки"), "monthly", 6, None, 503.0, False),
        ("Інтернет", "opex", art("Господарські Інтернет", "Господарські"), "monthly", 6, None, 449.0, False),
        ("Мобільний звʼязок", "opex", art("Господарські Телефонія", "Господарські"), "monthly", 8, None, 255.0, False),
        ("СМС-розсилка", "marketing", art("СМС розсилка", "Реклама"), "monthly", 8, None, 1000.0, False),
        ("Хімчистка", "opex", art("Господарські Хімчистка", "Господарські"), "twice", 7, 27, 1140.0, False),
        ("Google Ads", "marketing", art("Реклама Google Ads", "Реклама"), "monthly", 1, None, None, True),
        ("Таргет Instagram", "marketing", art("Таргет Instagram", "Реклама"), "monthly", 1, None, None, True),
        ("Зарплата спеціалістів", "fot", art("Персонал Зарплата", "Зарплата"), "twice", 1, 16, None, True),
        ("Виплати адміністраторам", "fot", art("Персонал Зарплата", "Зарплата"), "twice", 1, 16, None, True),
        ("Відсотки від косметики", "fot", art("Персонал Зарплата", "Зарплата"), "monthly", 1, None, None, True),
        ("Бухгалтер", "tax", art("Бухгалтерія", "Персонал Зарплата"), "monthly", 15, None, 18000.0, False),
        ("Податки з ЗП (ЄСВ, ПДФО, ВЗ)", "tax", art("Податки ЄСВ на ЗП", "Податки"), "twice", 15, 30, None, True),
        ("Єдиний податок ФОП Колган", "tax", art("Податки ЄП ФОП Колган", "Податки"), "monthly", 15, None, 1729.4, False),
        ("Єдиний податок ФОП Бондар", "tax", art("Податки ЄП ФОП Бондар", "Податки"), "monthly", 15, None, 1729.4, False),
        ("ЄСВ ФОП Колган", "tax", art("Податки ЄСВ ФОП Колган", "Податки"), "quarterly", 15, None, 5707.0, False),
        ("Військовий збір ФОП", "tax", art("Податки ВЗ ФОП Колган", "Податки"), "monthly", 15, None, 1729.4, False),
        ("Амортизація виробнича → фонд", "capex", art("Амортизація обладнання"), "twice", 1, 16, None, False),
        ("Амортизація розхідника → фонд", "capex", art("Амортизація обладнання"), "twice", 1, 16, None, False),
    ]
    for i, (name, g, a, sch, d1, d2, amt, appr) in enumerate(rows):
        db.add(PayPlan(name=name, group=g, article=a, schedule=sch, day=d1, day2=d2, amount=amt, needs_approval=appr, sort=i, start=date.today().replace(day=1)))
    db.merge(SyncState(key=SEED_KEY, value=date.today().isoformat(), updated_at=datetime.utcnow()))
    db.commit()
    return len(rows)


# ---------------- «живі» суми: амортизація, зарплата, % косметики ----------------
DYN_RE = [("amort_device", re.compile(r"амортизац\w*\s+виробн", re.I)), ("amort_cons", re.compile(r"амортизац\w*\s+розхідн", re.I)),
          ("salary_doc", re.compile(r"зарплата\s+спеціаліст", re.I)), ("salary_adm", re.compile(r"виплати\s+адмін", re.I)),
          ("cosm", re.compile(r"відсотки\s+від\s+косметик", re.I))]
DYN_NOTE = {"amort_device": "Нараховано й не погашено (виробнича) — вкладка «Амортизація»",
            "amort_cons": "Нараховано й не погашено (розхідники) — вкладка «Амортизація»",
            "salary_doc": "Баланс лікарів: нараховано − виплачено", "salary_adm": "Баланс адміністраторів: нараховано − виплачено",
            "cosm": "% з продажу косметики за попередній місяць — після ручного погодження"}


def dyn_kind(it) -> str | None:
    ref = it.ref or ""
    if ref.startswith("sal:"):
        return "salary_one"
    if ref.startswith("cosm:"):
        return "cosm_one"
    if ref == "split-parent":
        return "split"
    name = (it.plan.name if getattr(it, "plan", None) else None) or it.name or ""
    return next((k for k, rx in DYN_RE if rx.search(name)), None)


def _amort_due(db: Session, kind: str, today: date) -> tuple[float, float, list]:
    """(не погашено, живий півмісяць, [(asset, diff)])."""
    from ..models import AmortAsset
    from . import amort
    k = "device" if kind == "amort_device" else "consumable"
    rows, due, live = [], 0.0, 0.0
    for a in db.scalars(select(AmortAsset).where(AmortAsset.active.is_(True), AmortAsset.kind == k)):
        t = amort.totals(db, a, today)
        if t["diff"] > 0.009:
            rows.append((a, t["diff"]))
            due += t["diff"]
        live += float(t["live"].get("amount") or 0)
    return round(due, 2), round(live, 2), rows


def _salary_due(db: Session, kind: str) -> tuple[float, list]:
    from ..models import Doctor
    from . import payroll_docs, payroll_share
    want = "doctor" if kind == "salary_doc" else "admin"
    bal = payroll_docs.balances(db)
    rows = []
    for d in db.scalars(select(Doctor).where(Doctor.id.in_(list(bal) or [0]))):
        if payroll_share.role_of(d) == want and bal.get(d.id, 0) > 0.009:
            rows.append((d, bal[d.id]))
    return round(sum(v for _, v in rows), 2), sorted(rows, key=lambda x: -x[1])


def _payroll_covered(db: Session, due: date) -> bool:
    """Чи є проведене нарахування за півмісяць перед датою виплати (1-го — за 16–кінець, 16-го — за 1–15)."""
    from ..models import PayrollDoc
    end = due - timedelta(days=1)
    return any(d.d_to >= end - timedelta(days=3) and any(ln.posted for ln in d.lines)
               for d in db.scalars(select(PayrollDoc).where(PayrollDoc.deleted_at.is_(None), PayrollDoc.d_to >= end - timedelta(days=20))))


def _cosm_due(db: Session, due: date) -> tuple[float, list]:
    from . import reports
    pm_last = due.replace(day=1) - timedelta(days=1)
    try:
        r = reports.cosmetics(db, pm_last.replace(day=1), pm_last)
    except Exception:  # noqa: BLE001
        return 0.0, []
    rows = [(x["name"], x["sum"]) for x in r.rows if x.get("_group") and x.get("sum")]
    return round(sum(v for _, v in rows), 2), rows


def _fmt(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


def refresh_dynamic(db: Session, items: list[PayItem], today: date | None = None) -> None:
    """Суми амортизації / зарплати / % косметики рахуються наживо. Минулі неоплачені одного виду зводяться в останній
    (решта — «пропущено»), якщо борг уже погашено у вкладці — платіж закривається сам. Майбутній — прогноз."""
    today = today or date.today()
    if split_people(db, items, today):
        items = list(items) + db.scalars(select(PayItem).where(PayItem.ref.like("sal:%") | PayItem.ref.like("cosm:%"),
                                                               PayItem.due >= min(i.due for i in items), PayItem.due <= max(i.due for i in items))).all()
    refresh_people(db, items, today)
    by: dict = defaultdict(list)
    for it in items:
        k = dyn_kind(it)
        if k and k.startswith("amort") and it.status in ("planned", "approved"):
            by[k].append(it)
    changed = False
    cache: dict = {}
    for k, its in by.items():
        its.sort(key=lambda i: i.due)
        past = [i for i in its if i.due <= today]
        fut = [i for i in its if i.due > today]
        if k == "cosm":
            for it in its:
                if it.due <= today + timedelta(days=31):
                    amt, rows = _cosm_due(db, it.due)
                    it.amount = amt or None
                    it.note = (DYN_NOTE[k] + (": " + " · ".join(f"{n} {_fmt(v)}" for n, v in rows[:6]) if rows else ""))[:300]
                    changed = True
            continue
        if k not in cache:
            cache[k] = _amort_due(db, k, today) if k.startswith("amort") else (*_salary_due(db, k)[:1], 0.0, _salary_due(db, k)[1])
        due, live, rows = cache[k]
        detail = " · ".join(f"{getattr(o, 'name', o)} {_fmt(v)}" for o, v in rows[:6])
        if past:
            for it in past[:-1]:
                it.status, it.note = "skipped", f"Сума перейшла в платіж {past[-1].due.strftime('%d.%m')}"
            last = past[-1]
            if due <= 0.5 and k.startswith("salary") and not _payroll_covered(db, last.due):
                last.amount, last.note = None, "Нарахування за період ще немає — Фінанси → Нарахування зарплати → «На баланс»"
            elif due <= 0.5:
                last.status, last.amount_fact, last.paid_at = "paid", 0.0, today
                last.note = "Погашено поза календарем" + (" (вкладка «Амортизація»)" if k.startswith("amort") else " (виплати зарплати)")
            else:
                last.amount = due
                last.note = (DYN_NOTE[k] + (": " + detail if detail else ""))[:300]
            changed = True
        if fut:
            nxt = fut[0]
            nxt.amount = (round(live, 2) or None) if k.startswith("amort") else None
            nxt.note = ("Прогноз за поточний півмісяць — точна сума в день нарахування" if k.startswith("amort")
                        else "Сума зʼявиться після нарахування зарплати за період")
            changed = True
    if changed:
        db.commit()


def pay_dynamic(db: Session, it: PayItem, amount: float, day: date, cashbox: str | None, user_id: int | None, comment: str = ""):
    """Оплата амортизації — погашення у фонд по кожному апарату / розхіднику (як у вкладці «Амортизація»);
    зарплати — виплата кожному співробітнику (зменшує його баланс). Повертає першу фіноперацію."""
    from . import amort
    from .fin_articles import salary_names
    k = dyn_kind(it)
    if k and k.startswith("amort"):
        _due, _live, rows = _amort_due(db, k, day)
    elif k in ("salary_doc", "salary_adm"):
        _due, rows = _salary_due(db, k)
    else:
        return None
    total = sum(v for _, v in rows)
    if not rows or total <= 0:
        if k.startswith("amort"):
            raise ValueError("Немає непогашеної амортизації — усе вже в фонді")
        return None
    left, first = round(float(amount), 2), None
    for n, (obj, v) in enumerate(rows):
        part = round(min(v, left) if amount >= total else (left if n == len(rows) - 1 else round(amount * v / total, 2)), 2)
        if part <= 0:
            continue
        left = round(left - part, 2)
        if k.startswith("amort"):
            t = amort.pay(db, obj, day, part, cashbox or "", user_id, note=comment or f"Платіжний календар: {it.name}")
            fid = t.finop_id
        else:
            sal = it.article if it.article in salary_names(db) else salary_names(db)[0]
            op = FinOp(day=day, kind="expense", category=sal[:60], amount=part, cashbox=cashbox or it.cashbox or None, doctor_id=obj.id,
                       counterparty=obj.name[:200], comment=(comment or f"Платіжний календар: {it.name}")[:500], created_by=user_id, ref=f"paycal:{it.id}")
            db.add(op)
            db.flush()
            fid = op.id
        first = first or fid
    it.status, it.finop_id, it.paid_at, it.amount_fact = "paid", first, day, round(float(amount) - max(left, 0), 2)
    if cashbox:
        it.cashbox = cashbox
    db.commit()
    return db.get(FinOp, first) if first else None


PURCHASE_RE = re.compile(r"закуп", re.I)


def purchases(db: Session, first: date, last: date) -> dict:
    """Закупівельні фонди: скільки витрачено за місяць по статтях «Закупівля …» (окремої структури поки немає)."""
    by: dict = defaultdict(float)
    for op in db.scalars(select(FinOp).where(FinOp.deleted_at.is_(None), FinOp.kind == "expense", FinOp.storno_of.is_(None),
                                             FinOp.day >= first, FinOp.day <= last)):
        if PURCHASE_RE.search(op.category or ""):
            by[op.category] += float(op.amount or 0)
    rows = sorted(((k, round(v, 2)) for k, v in by.items()), key=lambda x: -x[1])
    return {"rows": rows, "total": round(sum(v for _, v in rows), 2)}


# ---------------- налаштування календаря з голосових побажань власника (09.11.2026) ----------------
SETUP2_KEY = "paycal_setup_v2"


def setup_v2(db: Session, today: date | None = None) -> bool:
    from ..models import SyncState
    from .fin_articles import names as _names
    if db.get(SyncState, SETUP2_KEY) or not db.scalar(select(PayPlan.id).limit(1)):
        return False
    today = today or date.today()
    m0 = today.replace(day=1)
    arts = set(_names(db, "expense", active_only=False))

    def art(*c):
        return next((x for x in c if x in arts), c[0])
    plans = {p.name: p for p in db.scalars(select(PayPlan))}

    def add(name, g, a, sch, d1=None, d2=None, amt=None, appr=False, start=None, cp=None, note=None):
        if name in plans:
            return plans[name]
        p = PayPlan(name=name, group=g, article=a, schedule=sch, day=d1, day2=d2, amount=amt, needs_approval=appr,
                    start=start or m0, counterparty=cp, note=note, sort=len(plans) + 1)
        db.add(p)
        plans[name] = p
        return p
    add("Оренда робочих місць", "opex", art("Оренда"), "monthly", 12, amt=650.0, cp="ФОП Бондар")
    add("Інша оренда 2", "opex", art("Оренда", "Інша оренда"), "monthly", 26, amt=3000.0, note="2–3 тис. ₴, готівка")
    add("Checkbox ФОП Колган", "opex", art("Програми Checkbox", "Сервіси та підписки"), "once", amt=2988.0, start=date(2027, 10, 1), note="Оплачено на рік")
    add("Checkbox ФОП Бондар", "opex", art("Програми Checkbox", "Сервіси та підписки"), "once", amt=2988.0, start=date(2027, 10, 1), note="Оплачено на рік")
    add("Ringostat", "opex", art("Сервіси та підписки Ringostat", "Сервіси та підписки"), "once", start=date(2027, 3, 1),
        note="Перевірити залишок балансу й вирішити, чи продовжуємо")
    add("Домени", "opex", art("Сервіси та підписки", "Програми"), "once", start=date(2027, 8, 1), note="Оплата доменів на рік")

    def resched(name_rx, d1, d2, sch="twice", appr=None, amt=..., rename=None):
        for p in list(plans.values()):
            if re.search(name_rx, p.name, re.I):
                p.schedule, p.day, p.day2 = sch, d1, d2
                if appr is not None:
                    p.needs_approval = appr
                if amt is not ...:
                    p.amount = amt
                if rename:
                    p.name = rename
                keep = {d1, d2}
                for it in db.scalars(select(PayItem).where(PayItem.plan_id == p.id, PayItem.due >= m0, PayItem.status.in_(("planned", "approved")))):
                    if it.due.day in keep or (sch == "twice" and d2 and d2 >= 28 and it.due.day == calendar.monthrange(it.due.year, it.due.month)[1]):
                        if amt is not ...:
                            it.amount = amt
                        continue
                    it.status, it.note = "skipped", "Графік змінено"
    resched(r"амортизац\w*\s+(виробн|розхідн)", 1, 16, amt=None)
    resched(r"зарплата\s+спеціаліст|виплати\s+адмін", 1, 16, appr=True, amt=None)
    resched(r"^бухгалтер", 15, 31, amt=9250.0)
    for p in plans.values():
        if re.search(r"хімчистк", p.name, re.I):
            p.active = False
            p.note = "Плаваюча дата — додавайте разовим платежем"
        if p.name == "Військовий збір ФОП":
            p.name = "Військовий збір ФОП Колган"
            for it in db.scalars(select(PayItem).where(PayItem.plan_id == p.id, PayItem.status.in_(("planned", "approved")))):
                it.name = p.name
    add("Військовий збір ФОП Бондар", "tax", art("Податки ВЗ ФОП Бондар", "Податки"), "monthly", 15)
    db.merge(SyncState(key=SETUP2_KEY, value=today.isoformat(), updated_at=datetime.utcnow()))
    db.commit()
    # планшет: 6 500 ₴ щомісяця 15-го, поки не закриємо 30 000
    from . import leasing
    xs = leasing.contracts(db)
    c = next((x for x in xs if x.get("group") == "tablet"), None)
    if c:
        leasing.save(db, c["id"], {**{k: str(c.get(k) or "") for k in ("name", "group", "kind", "total", "paid_before", "bank")},
                                   "monthly": "6500", "next": "2026-11-15" if today <= date(2026, 11, 15) else str(today.replace(day=15)),
                                   "note": "6 500 ₴ щомісяця 15-го"})
    return True


# ---------------- зарплата й % косметики — окремим платежем на кожного співробітника ----------------
SPLIT_KINDS = ("salary_doc", "salary_adm", "cosm")


def _staff(db: Session, kind: str) -> list:
    """Хто отримує: працюючі співробітники цієї ролі з окладом у картці або з боргом на балансі."""
    from ..models import Doctor
    from . import payroll_docs, payroll_share
    want = "doctor" if kind == "salary_doc" else "admin"
    bal = payroll_docs.balances(db)
    out = []
    for d in db.scalars(select(Doctor).order_by(Doctor.name)):
        if payroll_share.role_of(d) != want:
            continue
        if (d.active and (d.extra or {}).get("salary")) or bal.get(d.id, 0) > 0.009:
            out.append(d)
    return out


def split_people(db: Session, items: list[PayItem], today: date) -> bool:
    """Загальний платіж «Зарплата спеціалістів» / «Виплати адміністраторам» / «Відсотки від косметики» → рядок на кожного."""
    made = False
    for it in items:
        k = dyn_kind(it)
        if k not in SPLIT_KINDS or it.status not in ("planned", "approved") or not it.plan_id:
            continue
        if not (today - timedelta(days=45) <= it.due <= today + timedelta(days=40)):
            continue
        if k == "cosm":
            _amt, rows = _cosm_due(db, it.due)
            people = [(n[:40], n, None) for n, _v in rows if n != "Без ініціатора"]
            base = "% косметики"
        else:
            people = [(str(d.id), d.name, d.id) for d in _staff(db, k)]
            base = "Зарплата" if k == "salary_doc" else "Виплата"
        if not people:
            continue
        pre = ("cosm" if k == "cosm" else "sal")
        have = {i.ref for i in db.scalars(select(PayItem).where(PayItem.due == it.due, PayItem.ref.like(f"{pre}:{it.plan_id}:%")))}
        for key, name, _did in people:
            ref = f"{pre}:{it.plan_id}:{key}"
            if ref in have:
                continue
            db.add(PayItem(due=it.due, name=f"{base} · {name}"[:200], group=it.group, article=it.article, cashbox=it.cashbox, counterparty=name[:200],
                           status=it.status, source="plan", ref=ref))
            made = True
        it.ref, it.status, it.amount, it.note = "split-parent", "skipped", None, "Розбито по співробітниках"
        made = True
    if made:
        db.commit()
    return made


def refresh_people(db: Session, items: list[PayItem], today: date) -> None:
    """Суми по кожному: зарплата — баланс співробітника (нараховано − виплачено), % косметики — за попередній місяць."""
    from . import payroll_docs
    bal = None
    by: dict = defaultdict(list)
    for it in items:
        if dyn_kind(it) in ("salary_one", "cosm_one") and it.status in ("planned", "approved"):
            by[it.ref].append(it)
    if not by:
        return
    cosm_cache: dict = {}
    for ref, its in by.items():
        its.sort(key=lambda i: i.due)
        if ref.startswith("cosm:"):
            key = ref.split(":", 2)[2]
            for it in its:
                pm = it.due.replace(day=1) - timedelta(days=1)
                if pm not in cosm_cache:
                    cosm_cache[pm] = {n[:40]: v for n, v in _cosm_due(db, it.due)[1]}
                it.amount = cosm_cache[pm].get(key) or None
                it.note = f"% з продажу косметики за {MONTHS[pm.month - 1].lower()} — після ручного погодження"
            continue
        bal = bal if bal is not None else payroll_docs.balances(db)
        did = int(ref.split(":")[2])
        b = round(bal.get(did, 0.0), 2)
        past = [i for i in its if i.due <= today]
        fut = [i for i in its if i.due > today]
        if past:
            for it in past[:-1]:
                it.status, it.note = "skipped", f"Сума перейшла в платіж {past[-1].due.strftime('%d.%m')}"
            last = past[-1]
            if b > 0.009:
                last.amount, last.note = b, "Баланс: нараховано − виплачено"
            elif _payroll_covered(db, last.due):
                last.status, last.amount_fact, last.paid_at, last.note = "paid", 0.0, today, "Виплачено поза календарем"
            else:
                last.amount, last.note = None, "Нарахування за період ще немає — Фінанси → Нарахування зарплати → «На баланс»"
        for it in fut:
            it.amount, it.note = None, "Сума зʼявиться після нарахування за період"
    db.commit()
