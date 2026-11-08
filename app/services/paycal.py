"""Платіжний календар (/finance/calendar) — що і коли треба заплатити, звідки, чи погоджено, чи оплачено, і чи вистачить грошей.

Ідеї з таблиці власника «OPEX Calendar»: залишки по рахунках на початок, список платежів за групами (OPEX, маркетинг, ФОТ,
податки, борги) з платником, плановою сумою, дозволом на оплату, плановою й фактичною датою, відхиленням; плановий прихід.
У CRM це не таблиця на місяць, а шаблони регулярних платежів → платежі на дати (генеруються автоматично), рахунки постачальників
на погодженні (payreq) і разові платежі; оплата = фіноперація CRM (зіставляється автоматично за статтею / рахунком / датою),
залишки — з кас CRM, очікувані надходження — із записів у календарі."""
from __future__ import annotations

import calendar
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
    """Кнопка «Оплачено»: створює витрату в журналі й закриває платіж."""
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


def month_view(db: Session, y: int, m: int, group: str = "", status: str = "", today: date | None = None) -> dict:
    from . import cashboxes as cbx
    today = today or date.today()
    ensure_month(db, y, m)
    first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    items = db.scalars(select(PayItem).where(PayItem.due >= first - timedelta(days=45), PayItem.due <= last).order_by(PayItem.due, PayItem.id)).all()
    match_paid(db, items)
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
            "boxes": boxes, "cash": cash, "left": left, "income": inc, "inc_month": inc_month,
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
        ("Зарплата спеціалістів", "fot", art("Персонал Зарплата", "Зарплата"), "twice", 15, 30, None, True),
        ("Виплати адміністраторам", "fot", art("Персонал Зарплата", "Зарплата"), "twice", 15, 30, None, True),
        ("Відсотки від косметики", "fot", art("Персонал Зарплата", "Зарплата"), "monthly", 1, None, None, True),
        ("Бухгалтер", "tax", art("Бухгалтерія", "Персонал Зарплата"), "monthly", 15, None, 18000.0, False),
        ("Податки з ЗП (ЄСВ, ПДФО, ВЗ)", "tax", art("Податки ЄСВ на ЗП", "Податки"), "twice", 15, 30, None, True),
        ("Єдиний податок ФОП Колган", "tax", art("Податки ЄП ФОП Колган", "Податки"), "monthly", 15, None, 1729.4, False),
        ("Єдиний податок ФОП Бондар", "tax", art("Податки ЄП ФОП Бондар", "Податки"), "monthly", 15, None, 1729.4, False),
        ("ЄСВ ФОП Колган", "tax", art("Податки ЄСВ ФОП Колган", "Податки"), "quarterly", 15, None, 5707.0, False),
        ("Військовий збір ФОП", "tax", art("Податки ВЗ ФОП Колган", "Податки"), "monthly", 15, None, 1729.4, False),
        ("Лізинг обладнання · інвертор", "debt", art("Зобовʼязання Розтермінування MONOBANK", "Зобовʼязання"), "monthly", 8, None, 7904.0, False),
        ("Лізинг обладнання · батареї", "debt", art("Зобовʼязання Розтермінування MONOBANK", "Зобовʼязання"), "monthly", 14, None, 4000.0, False),
        ("Амортизація виробнича → фонд", "capex", art("Амортизація обладнання"), "twice", 7, 22, None, False),
        ("Амортизація розхідника → фонд", "capex", art("Амортизація обладнання"), "twice", 14, 22, None, False),
    ]
    for i, (name, g, a, sch, d1, d2, amt, appr) in enumerate(rows):
        db.add(PayPlan(name=name, group=g, article=a, schedule=sch, day=d1, day2=d2, amount=amt, needs_approval=appr, sort=i, start=date.today().replace(day=1)))
    db.merge(SyncState(key=SEED_KEY, value=date.today().isoformat(), updated_at=datetime.utcnow()))
    db.commit()
    return len(rows)
