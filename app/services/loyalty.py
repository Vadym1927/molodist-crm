"""Знижки й бонусна програма: правила знижок, налаштування бонусів, журнал бонусів (FIFO-згоряння), розрахунок для продажу."""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import BonusTxn, DiscountRule, Patient, SyncState

KEY = "bonus_program"
DEFAULT = {"enabled": False, "pct_services": 5.0, "pct_products": 3.0, "max_pay_pct": 30.0, "expire_days": 365,
           "welcome": 0.0, "birthday": 0.0, "accrue_on_bonus_paid": False, "min_check": 0.0}
KIND_LABELS = {"accrual": "Нарахування за покупку", "spend": "Оплата бонусами", "manual": "Вручну", "expire": "Згоріли",
               "import": "Стартовий залишок (CleverBox)", "birthday": "До дня народження", "welcome": "Вітальні"}
SEGMENTS = {"": "усім пацієнтам", "birthday": "іменинникам (±7 днів)", "new": "новим (перший візит)"}


def settings(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    d = copy.deepcopy(DEFAULT)
    if st and st.value:
        try:
            d.update(json.loads(st.value))
        except ValueError:
            pass
    return d


def save_settings(db: Session, d: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False), datetime.utcnow()
    db.merge(st)
    db.commit()


def balance(db: Session, patient_id: int) -> float:
    return round(float(db.scalar(select(func.sum(BonusTxn.amount)).where(BonusTxn.patient_id == patient_id)) or 0), 2)


def balances(db: Session) -> dict[int, float]:
    return {pid: round(float(s or 0), 2) for pid, s in db.execute(select(BonusTxn.patient_id, func.sum(BonusTxn.amount)).group_by(BonusTxn.patient_id))}


def add(db: Session, patient_id: int, amount: float, kind: str, comment: str | None = None, ref: str | None = None,
        user_id: int | None = None, expire_days: int | None = None) -> BonusTxn:
    if amount < 0 and balance(db, patient_id) + amount < -0.005:
        raise ValueError("Недостатньо бонусів")
    exp = None
    if amount > 0:
        days = expire_days if expire_days is not None else int(settings(db).get("expire_days") or 0)
        exp = date.today() + timedelta(days=days) if days else None
    t = BonusTxn(patient_id=patient_id, amount=round(amount, 2), kind=kind, comment=(comment or None), ref=ref, user_id=user_id,
                 expires_at=exp, at=datetime.now())
    db.add(t)
    db.commit()
    return t


def import_cleverbox(db: Session, user_id: int | None = None) -> int:
    """Стартові залишки з карток CleverBox (cb_bonus) — один раз для пацієнтів без руху бонусів."""
    have = {pid for (pid,) in db.execute(select(BonusTxn.patient_id).distinct())}
    n = 0
    for p in db.scalars(select(Patient).where(Patient.cb_bonus.is_not(None), Patient.cb_bonus > 0)):
        if p.id in have:
            continue
        db.add(BonusTxn(patient_id=p.id, amount=round(p.cb_bonus, 2), kind="import", comment="залишок з CleverBox", user_id=user_id,
                        at=datetime.now(), expires_at=None))
        n += 1
    db.commit()
    return n


def expire(db: Session, today: date | None = None) -> int:
    """FIFO: нарахування з минулим терміном, не витрачені до того, — згоряють (запис expire)."""
    today = today or date.today()
    by_p = defaultdict(list)
    for t in db.scalars(select(BonusTxn).order_by(BonusTxn.at, BonusTxn.id)):
        by_p[t.patient_id].append(t)
    n = 0
    for pid, txns in by_p.items():
        lots = []                                   # [залишок, термін]
        for t in txns:
            if t.amount > 0:
                lots.append([t.amount, t.expires_at])
            else:
                need = -t.amount
                for lot in lots:
                    if need <= 0:
                        break
                    take = min(lot[0], need)
                    lot[0] -= take
                    need -= take
        burn = round(sum(l[0] for l in lots if l[1] and l[1] < today and l[0] > 0.005), 2)
        if burn > 0:
            db.add(BonusTxn(patient_id=pid, amount=-burn, kind="expire", comment="термін дії минув", at=datetime.now()))
            n += 1
    db.commit()
    return n


def birthday_bonuses(db: Session, today: date | None = None) -> int:
    s = settings(db)
    amt = float(s.get("birthday") or 0)
    if not s.get("enabled") or amt <= 0:
        return 0
    today = today or date.today()
    n = 0
    for p in db.scalars(select(Patient).where(Patient.birthday.is_not(None))):
        if (p.birthday.month, p.birthday.day) != (today.month, today.day):
            continue
        ref = f"bday:{today.year}"
        if db.scalar(select(BonusTxn.id).where(BonusTxn.patient_id == p.id, BonusTxn.ref == ref)):
            continue
        add(db, p.id, amt, "birthday", "з днем народження", ref, expire_days=30)
        n += 1
    return n


# ---------------- знижки ----------------

def rule_applies(r: DiscountRule, day: date, patient: Patient | None = None) -> bool:
    if not r.active:
        return False
    if r.date_from and day < r.date_from or r.date_to and day > r.date_to:
        return False
    if r.weekdays and day.weekday() not in [int(x) for x in r.weekdays]:
        return False
    if r.segment == "birthday":
        if not patient or not patient.birthday:
            return False
        b = patient.birthday.replace(year=day.year) if not (patient.birthday.month == 2 and patient.birthday.day == 29) else date(day.year, 3, 1)
        if abs((b - day).days) > 7:
            return False
    if r.segment == "new" and patient is not None and (patient.visits or []):
        if any(v.status == "done" for v in patient.visits):
            return False
    return True


def discount_for(db: Session, items: list[dict], patient: Patient | None = None, day: date | None = None,
                 rule_ids: list[int] | None = None) -> dict:
    """items: [{type: service|product, category, price, qty}] → знижка по рядках і підсумок.
    Беремо авто-правила (+ вибрані вручну rule_ids) і персональну знижку пацієнта; без combine — найвигідніша одна."""
    day = day or date.today()
    total = sum(float(i["price"]) * float(i.get("qty") or 1) for i in items)
    rules = [r for r in db.scalars(select(DiscountRule).where(DiscountRule.active.is_(True)))
             if (r.auto or (rule_ids and r.id in rule_ids)) and rule_applies(r, day, patient) and total + 0.005 >= (r.min_sum or 0)]

    def line_disc(r: DiscountRule, it: dict) -> float:
        if r.applies_to == "services" and it["type"] != "service" or r.applies_to == "products" and it["type"] != "product":
            return 0.0
        if r.categories and (it.get("category") or "") not in r.categories:
            return 0.0
        base = float(it["price"]) * float(it.get("qty") or 1)
        return base * r.value / 100 if r.kind == "percent" else 0.0

    options = []
    for r in rules:
        per = [line_disc(r, it) for it in items]
        if r.kind == "amount":
            elig = [i for i, it in enumerate(items) if line_disc(DiscountRule(kind="percent", value=100, applies_to=r.applies_to,
                                                                                categories=r.categories), it) > 0]
            base = sum(float(items[i]["price"]) * float(items[i].get("qty") or 1) for i in elig) or 1
            amt = min(r.value, base)
            per = [amt * float(items[i]["price"]) * float(items[i].get("qty") or 1) / base if i in elig else 0.0 for i in range(len(items))]
        options.append((r, per))
    if patient and patient.discount_pct:
        options.append((DiscountRule(id=0, name=f"Персональна {patient.discount_pct:g}%", kind="percent", value=patient.discount_pct,
                                     applies_to="all", combine=False),
                        [float(it["price"]) * float(it.get("qty") or 1) * patient.discount_pct / 100 for it in items]))
    combo = [o for o in options if o[0].combine]
    single = max([o for o in options if not o[0].combine], key=lambda o: sum(o[1]), default=None)
    chosen = ([single] if single else []) + combo
    per = [0.0] * len(items)
    for _r, p in chosen:
        per = [a + b for a, b in zip(per, p)]
    per = [round(min(d, float(it["price"]) * float(it.get("qty") or 1)), 2) for d, it in zip(per, items)]
    return {"lines": per, "total": round(sum(per), 2), "rules": [r.name for r, _ in chosen], "subtotal": round(total, 2)}


def bonus_accrual(db: Session, items: list[dict], paid_by_bonus: float = 0.0) -> float:
    s = settings(db)
    if not s.get("enabled"):
        return 0.0
    tot = 0.0
    for it in items:
        net = float(it.get("net", it["price"] * (it.get("qty") or 1)))
        pct = s["pct_services"] if it["type"] == "service" else (s["pct_products"] if it["type"] == "product" else 0)
        tot += net * float(pct or 0) / 100
    if paid_by_bonus and not s.get("accrue_on_bonus_paid"):
        gross = sum(float(it.get("net", 0)) for it in items) or 1
        tot *= max(0.0, 1 - paid_by_bonus / gross)
    return round(tot, 2)


def max_bonus_pay(db: Session, total: float, patient_id: int) -> float:
    s = settings(db)
    if not s.get("enabled"):
        return 0.0
    return round(min(balance(db, patient_id), total * float(s.get("max_pay_pct") or 0) / 100), 2)
