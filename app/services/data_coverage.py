"""Повнота даних CRM: що є по роках (з 2022) і чого бракує — щоб довантажити з CleverBox (API або звітами) чи вручну.

Блоки:
  • history — кількість записів і суми по роках для кожного типу даних + місяці без даних (дірки);
  • quality — незаповнені / некоректні дані (пацієнти, послуги, товари, лікарі, склад, чеки);
Для кожного рядка — звідки довантажити."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import (Appointment, BankTxn, BeautyPlan, CbFinOp, CbService, Check, Doctor, FinOp, GiftCertificate, Patient,
                      PayrollDoc, Product, ProductPurchase, ProductSale, Sale, StaffShift, StockDoc, StockMove, Subscription,
                      TechCard, Visit)
from . import autonomy

START_YEAR = 2022
API = "CleverBox API: «Дані → Перенос з CleverBox», історія з 01.01.2022"


def _years() -> list[int]:
    return list(range(START_YEAR, date.today().year + 1))


def _ym(v) -> tuple[int, int] | None:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = date.fromisoformat(v[:10])
        except ValueError:
            return None
    return (v.year, v.month)


def _series(db: Session, col, amount=None, where=()) -> dict:
    """{(рік, місяць): [к-сть, сума]}"""
    out: dict = defaultdict(lambda: [0, 0.0])
    cols = [col] + ([amount] if amount is not None else [])
    for row in db.execute(select(*cols).where(*where)):
        k = _ym(row[0])
        if not k or k[0] < START_YEAR:
            continue
        out[k][0] += 1
        if amount is not None:
            out[k][1] += float(row[1] or 0)
    return out


def _gaps(ser: dict, until: date) -> list[str]:
    """Місяці без жодного запису між першим місяцем з даними і `until` (дірки в історії)."""
    if not ser:
        return []
    y, m = min(ser)
    out = []
    while (y, m) <= (until.year, until.month):
        if (y, m) not in ser and (y, m) != (until.year, until.month):
            out.append(f"{m:02d}.{y}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def history(db: Session) -> dict:
    since = autonomy.since(db)
    cb_until = since or date.today()
    rows = []

    def add(key, name, ser, src, money=False, cb=True, expect_from=START_YEAR):
        yrs = {y: [0, 0.0] for y in _years()}
        for (y, _m), (n, s) in ser.items():
            if y in yrs:
                yrs[y][0] += n
                yrs[y][1] += s
        first = min(ser) if ser else None
        miss_years = [y for y in _years() if y >= expect_from and (not cb or y <= cb_until.year) and not yrs[y][0]]
        gaps = _gaps(ser, cb_until) if cb else []
        rows.append({"key": key, "name": name, "years": yrs, "money": money, "src": src, "first": f"{first[1]:02d}.{first[0]}" if first else None,
                     "miss_years": miss_years, "gaps": gaps, "status": "bad" if not ser else ("warn" if miss_years or len(gaps) > 0 else "ok")})

    add("checks", "Чеки CleverBox (виручка)", _series(db, Check.date, Check.amount, (Check.deleted.is_(False),)), API, True)
    add("services", "Надані послуги (візити CleverBox)", _series(db, Visit.starts_at, Visit.amount, (Visit.source == "cb_service_sale",)), API, True)
    add("visits", "Записи CleverBox (календар)", _series(db, Visit.starts_at, None, (Visit.source == "cb_visit",)),
        "CleverBox API (майбутні записи) — історія записів API не віддає; минулі візити видно з наданих послуг", cb=True)
    add("product_sales", "Продажі товарів CleverBox", _series(db, ProductSale.sold_at, ProductSale.amount), API, True)
    add("cb_finops", "Фіноперації CleverBox (журнал)", _series(db, CbFinOp.at, CbFinOp.amount),
        "Звіт CleverBox «Фінанси → Журнал фінансових операцій» (.xls) → «Фінанси → Фіноперації → Імпорт»", True)
    add("patients", "Нові пацієнти (дата реєстрації в CleverBox)", _series(db, Patient.cb_registered_at), API + " (оновлює картки пацієнтів)")
    add("purchases", "Закупівлі товарів (ціни приходу)", _series(db, ProductPurchase.bought_on, None),
        "Звіт CleverBox «Склад → Рух товарів / Оборотна відомість» (.xls) → «Продукти → Імпорт обороту»")
    add("certs", "Сертифікати (продані)", _series(db, GiftCertificate.sold_at, GiftCertificate.price),
        "Звіт CleverBox «Сертифікати» (.xls) → «Сертифікати й абонементи → Імпорт»", True)
    add("subs", "Абонементи (продані)", _series(db, Subscription.sold_at, Subscription.price),
        "Звіт CleverBox «Абонементи» (.xls) → «Сертифікати й абонементи → Імпорт»", True)
    add("plans", "Бюті-плани (консультації OBSERV)", _series(db, BeautyPlan.session_at, BeautyPlan.total_planned),
        "PDF планів з апарата OBSERV → «Завантажити план» або архівом ZIP («Дані → Імпорт»)", True)
    add("bank", "Банк monobank (виписки)", _series(db, BankTxn.at, BankTxn.amount), "monobank API — автоматично за 2 доби; історію — «Банк monobank → Синхронізувати»", True,
        cb=False, expect_from=date.today().year)
    add("payroll", "Нарахування зарплати (документи CRM)", _series(db, PayrollDoc.d_from, PayrollDoc.total, (PayrollDoc.deleted_at.is_(None),)),
        "Ведеться в CRM; минулі виплати — фіноперації CleverBox (стаття «Зарплата»)", True, cb=False, expect_from=date.today().year)
    add("crm_sales", "Каса CRM (чеки)", _series(db, Sale.at, Sale.total, (Sale.status == "paid",)), "Ведеться в CRM", True, cb=False,
        expect_from=date.today().year)
    add("crm_appts", "Записи CRM (календар)", _series(db, Appointment.start), "Ведеться в CRM", cb=False, expect_from=date.today().year)
    add("crm_finops", "Фіноперації CRM", _series(db, FinOp.day, FinOp.amount, (FinOp.deleted_at.is_(None),)), "Ведеться в CRM", True, cb=False,
        expect_from=date.today().year)
    add("stock_docs", "Документи складу CRM", _series(db, StockDoc.doc_date), "Ведеться в CRM (з дати старту обліку)", cb=False,
        expect_from=date.today().year)
    return {"rows": rows, "years": _years(), "since": since}


def quality(db: Session) -> list[dict]:
    out = []

    def add(group, name, n, total, hint, level="warn", sample=None):
        out.append({"group": group, "name": name, "n": n, "total": total, "hint": hint, "level": level if n else "ok",
                    "sample": (sample or [])[:8]})

    pts = db.execute(select(Patient.id, Patient.full_name, Patient.phone, Patient.birthday, Patient.cleverbox_id, Patient.cb_registered_at)).all()
    tp = len(pts)
    add("Пацієнти", "Без телефону", sum(1 for p in pts if not p.phone), tp, "Звіт CleverBox «Клієнти» (.xls) → «Дані → Імпорт пацієнтів»")
    add("Пацієнти", "Без дати народження", sum(1 for p in pts if not p.birthday), tp, "CleverBox API (перенос) або звіт «Клієнти»", "info")
    add("Пацієнти", "Без зв'язку з CleverBox (немає id)", sum(1 for p in pts if not p.cleverbox_id), tp,
        "Створені вручну / з планів — зіставляються при переносі за телефоном і ПІБ", "info")
    add("Пацієнти", "Без дати реєстрації в CleverBox", sum(1 for p in pts if p.cleverbox_id and not p.cb_registered_at), tp,
        "CleverBox API (перенос) — потрібно для сегмента «Нові за 30 днів»")
    phones = defaultdict(list)
    for p in pts:
        k = "".join(ch for ch in (p.phone or "") if ch.isdigit())[-9:]
        if len(k) == 9:
            phones[k].append(p.full_name)
    dup = [v for v in phones.values() if len(v) > 1]
    add("Пацієнти", "Дублікати (однаковий телефон)", sum(len(v) for v in dup), tp, "Картка пацієнта → «Об'єднати»", "warn",
        [" / ".join(v[:3]) for v in dup])

    svs = db.scalars(select(CbService)).all()
    act = [s for s in svs if s.active is not False]
    ta = len(act)
    add("Послуги (прайс)", "Без ціни", sum(1 for s in act if not s.price), ta, "«Продукти → Послуги (прайс)» або перенос з CleverBox", "warn",
        [s.name for s in act if not s.price])
    add("Послуги (прайс)", "Без категорії", sum(1 for s in act if not s.category), ta, "«Послуги (прайс)» → Змінити", "info",
        [s.name for s in act if not s.category])
    add("Послуги (прайс)", "Без тривалості", sum(1 for s in act if not (s.duration or s.time_min)), ta, "«Послуги (прайс)» — потрібна для календаря", "warn",
        [s.name for s in act if not (s.duration or s.time_min)])
    add("Послуги (прайс)", "Без техкарти (немає собівартості й списання)", sum(1 for s in act if not s.techcard_id), ta,
        "«Склад → Техкарти»: створити або імпортувати з CleverBox", "warn", [s.name for s in act if not s.techcard_id])
    cards = db.scalars(select(TechCard).where(TechCard.active.is_not(False))).all()
    add("Послуги (прайс)", "Порожні техкарти (без матеріалів)", sum(1 for c in cards if not c.lines), len(cards), "«Техкарти» → додати матеріали",
        "warn", [c.name for c in cards if not c.lines])

    prods = db.scalars(select(Product).where(Product.active.is_not(False))).all()
    tpr = len(prods)
    bought = {pid for (pid,) in db.execute(select(ProductPurchase.product_id).distinct())}
    add("Товари", "Без собівартості й без жодної закупівлі", sum(1 for p in prods if not p.cost and p.id not in bought), tpr,
        "Імпорт обороту з CleverBox або поставка в «Склад → Поставки»", "warn", [p.name for p in prods if not p.cost and p.id not in bought])
    add("Товари", "Без складу", sum(1 for p in prods if not p.warehouse), tpr, "«Продукти» → склад", "warn", [p.name for p in prods if not p.warehouse])
    add("Товари", "Без одиниці обліку", sum(1 for p in prods if not p.unit), tpr, "«Продукти» → одиниця (шт., мл…)", "info")
    add("Товари", "Вітрина без ціни продажу", sum(1 for p in prods if p.warehouse == "vitrina" and not (p.price or p.price_manual)),
        sum(1 for p in prods if p.warehouse == "vitrina"), "«Продукти» → ціна", "warn",
        [p.name for p in prods if p.warehouse == "vitrina" and not (p.price or p.price_manual)])

    bal: dict = defaultdict(float)
    for pid, wh, q in db.execute(select(StockMove.product_id, StockMove.warehouse, func.sum(StockMove.qty)).group_by(StockMove.product_id, StockMove.warehouse)):
        bal[(pid, wh)] = float(q or 0)
    names = {p.id: p.name for p in prods}
    neg = [(names.get(pid, f"#{pid}"), wh, q) for (pid, wh), q in bal.items() if q < -1e-6]
    add("Склад", "Від'ємні залишки", len(neg), len(bal), "Поставка або інвентаризація; часто — немає приходу на склад", "bad",
        [f"{n} ({wh}: {q:g})" for n, wh, q in neg])
    eq = [p for p in prods if p.warehouse == "equip"]
    eq_pos = sum(1 for p in eq if bal.get((p.id, "equip"), 0) > 0)
    add("Склад", "Обладнання (амортизація) без залишку", len(eq) - eq_pos, len(eq),
        "Без залишку на складі «Обладнання» списання амортизації з техкарт іде в мінус — внесіть стартовий залишок", "bad",
        [p.name for p in eq if bal.get((p.id, "equip"), 0) <= 0])

    docs = db.scalars(select(Doctor).where(Doctor.active.is_(True))).all()
    td = len(docs)
    add("Команда", "Без схеми зарплати", sum(1 for d in docs if not (d.extra or {}).get("salary")), td,
        "«Команда» → картка → Зарплата (схема з «Налаштування → Оклади»)", "warn", [d.name for d in docs if not (d.extra or {}).get("salary")])
    m0 = date.today().replace(day=1)
    with_shift = {x for (x,) in db.execute(select(StaffShift.doctor_id).where(StaffShift.day >= m0).distinct())}
    shown = [d for d in docs if d.in_calendar is not False]
    add("Команда", "Без графіку на поточний місяць (не з'являться в календарі)", sum(1 for d in shown if d.id not in with_shift), len(shown),
        "«Команда → Графік роботи»", "bad", [d.name for d in shown if d.id not in with_shift])

    ch = db.execute(select(Check.items).where(Check.deleted.is_(False))).all()
    svc_lines = zero = 0
    for (items,) in ch:
        for it in items or []:
            if it.get("item_type") == "service":
                svc_lines += 1
                if not float(it.get("payd_amount") or it.get("total") or 0):
                    zero += 1
    add("Чеки й візити", "Послуги в чеках із сумою 0 ₴", zero, svc_lines, "Перевірити знижки 100% / сертифікати / абонементи; інакше — повторний перенос чеків", "info")
    vz = db.execute(select(func.count()).select_from(Visit).where(Visit.source == "cb_service_sale", (Visit.amount.is_(None)) | (Visit.amount == 0))).scalar() or 0
    vt = db.execute(select(func.count()).select_from(Visit).where(Visit.source == "cb_service_sale")).scalar() or 0
    add("Чеки й візити", "Надані послуги без суми («Надано послуг 0 ₴»)", vz, vt, "Суми беруться з чеків CleverBox — повторний перенос історії", "warn")
    keys = defaultdict(int)
    for r in db.execute(select(ProductSale.patient_id, ProductSale.product_name, ProductSale.sold_at, ProductSale.amount)):
        keys[(r[0], r[1], r[2].date() if r[2] else None, round(float(r[3] or 0), 2))] += 1
    dups = sum(n - 1 for n in keys.values() if n > 1)
    add("Чеки й візити", "Можливі дублі продажів товарів", dups, sum(keys.values()), "Перенос частинами міг задвоїти рядки — перевірити й прибрати (з дозволу)", "warn")
    return out


def summary(db: Session) -> dict:
    h, q = history(db), quality(db)
    return {"history": h, "quality": q, "at": datetime.now(), "bad": sum(1 for r in h["rows"] if r["status"] == "bad") + sum(1 for x in q if x["level"] == "bad"),
            "warn": sum(1 for r in h["rows"] if r["status"] == "warn") + sum(1 for x in q if x["level"] == "warn")}
