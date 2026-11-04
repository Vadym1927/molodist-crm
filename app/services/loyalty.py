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

TAGS = ["Постійні акції", "Сезонні акції", "ЛЕ", "STRATO", "Співробітник"]
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def tags(db: Session) -> list[str]:
    """Розділи знижок: стандартні + ті, що вже є в правилах."""
    have = [t for (t,) in db.execute(select(DiscountRule.tag).distinct()) if t]
    return TAGS + sorted({t for t in have if t not in TAGS}, key=str.lower)


def uses(db: Session, r: DiscountRule, patient_id: int | None = None) -> int:
    """Скільки разів знижку застосовано в оплачених продажах (Sale.rules зберігає назви)."""
    from ..models import Sale
    q = select(Sale.rules).where(Sale.status.in_(("paid", "awaiting")), Sale.rules.is_not(None))
    if patient_id:
        q = q.where(Sale.patient_id == patient_id)
    return sum(1 for rl in db.scalars(q) if rl and r.name in rl)


def expired(r: DiscountRule, day: date | None = None) -> bool:
    day = day or date.today()
    return bool(r.date_to and day > r.date_to)


def rule_applies(r: DiscountRule, day: date, patient: Patient | None = None, db: Session | None = None,
                 at: datetime | None = None) -> bool:
    if not r.active:
        return False
    if r.date_from and day < r.date_from or r.date_to and day > r.date_to:
        return False
    if r.weekdays and day.weekday() not in [int(x) for x in r.weekdays]:
        return False
    tw = (r.time_windows or {}).get(str(day.weekday())) if r.time_windows else None
    if r.time_windows and at is not None:
        if not tw:
            return False
        hm = at.strftime("%H:%M")
        if tw[0] and hm < tw[0] or tw[1] and hm > tw[1]:
            return False
    elif r.time_windows and str(day.weekday()) not in r.time_windows:
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
    if r.client_groups:
        from .patient_tags import group_name
        mine = {group_name(g).lower() for g in (patient.client_groups or [])} if patient else set()
        if not mine & {str(g).lower() for g in r.client_groups}:
            return False
    if r.patient_statuses:
        if not patient or db is None:
            return False
        from . import patient_tags as ptg
        st = ptg.tag_for(ptg.tags(db), ptg.visit_days(db, [patient.id]).get(patient.id, 0))
        if st not in r.patient_statuses:
            return False
    if db is not None and r.uses_limit and uses(db, r) >= r.uses_limit:
        return False
    if db is not None and r.per_client_limit and patient and uses(db, r, patient.id) >= r.per_client_limit:
        return False
    return True


def item_ok(r: DiscountRule, it: dict) -> bool:
    """Чи діє правило на рядок продажу."""
    t = it.get("type")
    if t == "certificate":
        return bool(r.on_certs)
    if t == "subscription":
        return bool(r.on_subs)
    if r.services or r.products:                       # точний перелік послуг / товарів — головний
        nm = (it.get("name") or "").strip().lower()
        pool = r.services if t == "service" else r.products if t == "product" else None
        return bool(pool) and nm in {str(x).strip().lower() for x in pool}
    if r.applies_to == "services" and t != "service" or r.applies_to == "products" and t != "product":
        return False
    if r.categories and (it.get("category") or "") not in r.categories:
        return False
    return t in ("service", "product")


def available(db: Session, patient: Patient | None = None, day: date | None = None, at: datetime | None = None) -> list[DiscountRule]:
    """Ручні знижки, які зараз можна вибрати в касі (діють за датою, днем, лімітами, групою)."""
    day = day or date.today()
    rs = db.scalars(select(DiscountRule).where(DiscountRule.active.is_(True), DiscountRule.auto.is_(False))
                    .order_by(DiscountRule.tag, DiscountRule.name)).all()
    return [r for r in rs if rule_applies(r, day, patient, db, at)]


def discount_for(db: Session, items: list[dict], patient: Patient | None = None, day: date | None = None,
                 rule_ids: list[int] | None = None, at: datetime | None = None) -> dict:
    """items: [{type: service|product|certificate|subscription, name, category, price, qty}] → знижка по рядках і підсумок.
    Беремо авто-правила (+ вибрані вручну rule_ids) і персональну знижку пацієнта; без combine — найвигідніша одна."""
    day = day or date.today()
    total = sum(float(i["price"]) * float(i.get("qty") or 1) for i in items)
    rules = [r for r in db.scalars(select(DiscountRule).where(DiscountRule.active.is_(True)))
             if (r.auto or (rule_ids and r.id in rule_ids)) and rule_applies(r, day, patient, db, at) and total + 0.005 >= (r.min_sum or 0)]

    def base(it: dict) -> float:
        return float(it["price"]) * float(it.get("qty") or 1)

    options = []
    for r in rules:
        elig = [i for i, it in enumerate(items) if item_ok(r, it)]
        if r.kind == "amount":
            b = sum(base(items[i]) for i in elig) or 1
            amt = min(r.value, b)
            per = [amt * base(items[i]) / b if i in elig else 0.0 for i in range(len(items))]
        else:
            per = [base(it) * r.value / 100 if i in elig else 0.0 for i, it in enumerate(items)]
        options.append((r, per))
    from . import personal_discounts as pdc
    if patient and pdc.has(patient):
        pc = pdc.cfg(db)
        if pc["auto"] or (rule_ids and 0 in rule_ids):        # без автозастосування — лише коли обрали в касі
            pr, sv = pdc.pcts(patient)
            lbl = " / ".join(x for x in ((f"товари {pr:g}%" if pr else ""), (f"послуги {sv:g}%" if sv else "")) if x)
            options.append((DiscountRule(id=0, name=f"{pc['name']} ({lbl})", kind="percent", value=max(pr, sv),
                                         applies_to="all", combine=bool(pc["combine"])),
                            [float(it["price"]) * float(it.get("qty") or 1) *
                             (pr if it["type"] == "product" else sv if it["type"] == "service" else 0) / 100 for it in items]))
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


# ---------------- перенесення активних знижок CleverBox (з переліку на 04.11.2026) ----------------
# (назва, розмір, %|₴, з, по, розділ, обмеження на пацієнта, на що)
CB_DISCOUNTS = [
    ("-10% БАРБ (діє лише 1 РАЗ)", 10, "percent", "2026-01-01", "2026-12-31", "Постійні акції", 1, "all"),
    ("-10% ДР Пацієнти", 10, "percent", "2026-01-01", "2026-12-31", "Постійні акції", None, "all"),
    ("-15% ЗСУ", 15, "percent", "2026-01-01", "2026-12-31", "Постійні акції", None, "all"),
    ("-5% без пакування", 5, "percent", "2026-01-01", "2026-12-31", "Постійні акції", None, "products"),
    ("-5% Фото відгук за QR", 5, "percent", "2026-01-01", "2026-12-31", "Постійні акції", None, "all"),
    ("5% sorry", 5, "percent", "2026-01-01", "2026-12-31", "Постійні акції", None, "all"),
    ("300 STRATOSPHERE 1 ВІЗИТ", 300, "amount", "2026-01-01", "2026-11-30", "STRATO", None, "services"),
    ("-15 % Лазер Епіляція", 15, "percent", "2025-03-26", "2026-12-28", "ЛЕ", None, "services"),
    ("-30% Лазер ТІЛЬКИ ДЛЯ ПОСТІЙНИХ від 2 зон (крім комплексів)", 30, "percent", "2025-01-01", "2026-12-31", "ЛЕ", None, "services"),
    ("-50% Перший Візит Лазер", 50, "percent", "2025-01-01", "2026-12-31", "ЛЕ", 1, "services"),
    ("-15% Косметика (Співробітник)", 15, "percent", "2026-01-01", "2026-12-31", "Співробітник", None, "products"),
    ("-20% Додаткові Послуги (Співробітники)", 20, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-30% Біоревіталізація/мезотерапія (Співробітник)", 30, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-40% Біоревіталізація/мезотерапія (Співробітники)", 40, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-40% Ботулінотерапія (Співробітники)", 40, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-40% Видалення (Співробітники)", 40, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-40% Доглядові процедури (Співробітники)", 40, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-40% Пілінги (Співробітники)", 40, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-40% Трихологія (Співробітники)", 40, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-50% MOLODIST RESET (Співробітники)", 50, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-50% Очищення шкіри (Співробітники)", 50, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
    ("-60% Лазерна Епіляція (Співробітники)", 60, "percent", "2025-07-01", "2027-06-30", "Співробітник", None, "services"),
    ("-60% Неінвазивний рф (Співробітники)", 60, "percent", "2026-01-23", "2026-12-31", "Співробітник", None, "services"),
]


def import_cb_discounts(db: Session) -> tuple[int, int]:
    """Створює відсутні (за назвою) активні знижки CleverBox як ручні; наявні лише дописує розділ. → (створено, оновлено)."""
    have = {r.name.strip().lower(): r for r in db.scalars(select(DiscountRule))}
    new = upd = 0
    for name, val, kind, d1, d2, tag, lim, to in CB_DISCOUNTS:
        r = have.get(name.lower())
        if r:
            if not r.tag:
                r.tag, upd = tag, upd + 1
            continue
        db.add(DiscountRule(name=name, value=val, kind=kind, date_from=date.fromisoformat(d1), date_to=date.fromisoformat(d2),
                            tag=tag, per_client_limit=lim, applies_to=to, auto=False, combine=False, active=True,
                            note="Перенесено з CleverBox — уточніть послуги/товари"))
        new += 1
    db.commit()
    return new, upd
