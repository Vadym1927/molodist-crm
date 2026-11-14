"""Звіти блоку «Продажі» за касою CRM: використання сертифікатів і абонементів, знижки, крос-продажі догляду після процедур."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Doctor, GiftCertificate, Patient, Sale, Subscription, User
from .reports import Col, Report, _rng

CROSS_DAYS = 14


def _paid(db: Session, a: datetime, b: datetime) -> list[Sale]:
    return list(db.scalars(select(Sale).where(Sale.status == "paid", Sale.at >= a, Sale.at <= b).order_by(Sale.at.desc())))


def _names(db: Session):
    return ({d.id: d.name for d in db.scalars(select(Doctor))}, {u.id: u.name for u in db.scalars(select(User))})


def cert_use(db: Session, d_from: date, d_to: date) -> Report:
    """Оплати сертифікатами й абонементами за період: хто, коли, чим і за що."""
    from .sales import CERT, SUB
    a, b = _rng(d_from, d_to)
    docs, users = _names(db)
    r = Report("certificates", "Використання сертифікатів і абонементів",
               "Оплати сертифікатами й абонементами в касі CRM за період: хто, коли, яким сертифікатом чи абонементом і за що заплатив.",
               empty="За період ніхто не платив сертифікатом чи абонементом.")
    r.cols = [Col("at", "Дата", "date"), Col("check", "Чек"), Col("patient", "Пацієнт"), Col("what", "Чим оплачено"),
              Col("amount", "Сума", "uah", True), Col("items", "За що"), Col("admin", "Провів(ла)")]
    tot = {"cert": 0.0, "sub": 0.0}
    by = defaultdict(float)
    for s in _paid(db, a, b):
        for p in s.payments:
            if p.method not in (CERT, SUB):
                continue
            if p.method == CERT:
                c = db.get(GiftCertificate, p.cert_id) if p.cert_id else None
                what = f"Сертифікат №{c.code or c.id}" + (f" · {c.type_name}" if c and c.type_name else "") if c else "Сертифікат"
                tot["cert"] += p.amount
            else:
                sb = db.get(Subscription, p.sub_id) if p.sub_id else None
                what = f"Абонемент · {sb.type_name}" if sb and sb.type_name else "Абонемент"
                tot["sub"] += p.amount
            by[what.split(" · ")[-1] if " · " in what else what] += p.amount
            r.rows.append({"at": s.at, "check": f"Ч-{s.id}", "patient": s.patient.full_name if s.patient else "—", "what": what,
                           "amount": round(p.amount, 2), "items": ", ".join(i.name for i in s.items)[:160],
                           "admin": users.get(s.created_by, ""), "_href": f"/sales/{s.id}"})
    r.tiles = [("Оплачено сертифікатами", round(tot["cert"], 2), None, "uah", ""), ("Оплачено абонементами", round(tot["sub"], 2), None, "uah", ""),
               ("Оплат", len(r.rows), None, "int", "")]
    r.chart, r.chart_title = sorted(by.items(), key=lambda kv: -kv[1])[:12], "За видами сертифікатів і абонементів"
    return r.finish()


def discounts(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """Знижки в касі CRM: скільки грошей пішло на знижки, за якими правилами, у кого."""
    a, b = _rng(d_from, d_to)
    docs, users = _names(db)
    sales = _paid(db, a, b)
    view = view if view in ("rules", "doctors", "admins") else ""
    r = Report("discounts", "Використання знижок",
               "Знижки в чеках каси CRM за період. Сума знижки — різниця між ціною за прайсом і сумою чека "
               "(оплата бонусами, сертифікатом чи з рахунку сюди не входить). Правила — «Налаштування → Знижки».",
               empty="За період чеків зі знижкою не було.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "Чеки зі знижкою"), ("rules", "За правилами"), ("doctors", "За лікарями"), ("admins", "За адміністраторами")]}]
    sub = sum(float(s.subtotal or 0) for s in sales)
    disc = [s for s in sales if float(s.discount or 0) > 0.005]
    dsum = sum(float(s.discount or 0) for s in disc)
    by_rule = defaultdict(lambda: {"n": 0, "sum": 0.0, "base": 0.0})
    by_doc = defaultdict(lambda: {"n": 0, "sum": 0.0, "base": 0.0})
    by_adm = defaultdict(lambda: {"n": 0, "sum": 0.0, "base": 0.0})
    for s in disc:
        rules = s.rules or ["Без правила"]
        for name in rules:                                   # кілька правил у чеку — знижку ділимо порівну
            g = by_rule[name]
            g["n"] += 1
            g["sum"] += float(s.discount) / len(rules)
            g["base"] += float(s.subtotal or 0) / len(rules)
        g = by_adm[users.get(s.created_by, "—")]
        g["n"] += 1
        g["sum"] += float(s.discount)
        g["base"] += float(s.subtotal or 0)
        seen = set()
        for i in s.items:
            if float(i.discount or 0) <= 0.005:
                continue
            g = by_doc[docs.get(i.doctor_id, "—")]
            g["sum"] += float(i.discount)
            g["base"] += float(i.price or 0) * float(i.qty or 1)
            if i.doctor_id not in seen:
                g["n"] += 1
                seen.add(i.doctor_id)
    r.tiles = [("Знижок на суму", round(dsum, 2), None, "uah", f"{round(100 * dsum / sub, 1) if sub else 0}% від суми за прайсом"),
               ("Чеків зі знижкою", len(disc), None, "int", f"з {len(sales)} — {round(100 * len(disc) / len(sales)) if sales else 0}%"),
               ("Середня знижка в чеку", round(dsum / len(disc), 2) if disc else 0, None, "uah",
                f"{round(100 * dsum / sum(float(s.subtotal or 0) for s in disc), 1) if disc else 0}% від чека")]
    if view:
        src = {"rules": by_rule, "doctors": by_doc, "admins": by_adm}[view]
        r.cols = [Col("name", {"rules": "Правило", "doctors": "Лікар", "admins": "Адміністратор"}[view]), Col("n", "Чеків", "int", True),
                  Col("sum", "Знижка", "uah", True), Col("pct", "% від суми за прайсом", "pct"), Col("share", "Частка всіх знижок", "pct")]
        for k, g in sorted(src.items(), key=lambda kv: -kv[1]["sum"]):
            r.rows.append({"name": k, "n": g["n"], "sum": round(g["sum"], 2), "pct": round(100 * g["sum"] / g["base"], 1) if g["base"] else 0,
                           "share": round(100 * g["sum"] / dsum, 1) if dsum else 0})
    else:
        r.cols = [Col("at", "Дата", "date"), Col("check", "Чек"), Col("patient", "Пацієнт"), Col("rules", "Правило"),
                  Col("subtotal", "За прайсом", "uah", True), Col("discount", "Знижка", "uah", True), Col("pct", "%", "pct"),
                  Col("total", "До сплати", "uah", True), Col("admin", "Провів(ла)")]
        for s in disc:
            r.rows.append({"at": s.at, "check": f"Ч-{s.id}", "patient": s.patient.full_name if s.patient else "—",
                           "rules": " + ".join(s.rules or []) or "—", "subtotal": s.subtotal, "discount": s.discount,
                           "pct": round(100 * s.discount / s.subtotal, 1) if s.subtotal else 0, "total": s.total,
                           "admin": users.get(s.created_by, ""), "_href": f"/sales/{s.id}"})
    r.chart = [(k, round(g["sum"], 2)) for k, g in sorted(by_rule.items(), key=lambda kv: -kv[1]["sum"])[:12]]
    r.chart_title = "Сума знижок за правилами"
    return r.finish()


def cross(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """Крос-продажі: хто після процедури купив домашній догляд (у тому ж чеку або протягом 14 днів), а хто — ні."""
    a, b = _rng(d_from, d_to)
    docs, _ = _names(db)
    sales = [s for s in _paid(db, a, b) if s.patient_id]
    later = [s for s in _paid(db, a, b + timedelta(days=CROSS_DAYS)) if s.patient_id]
    prod = defaultdict(list)                                  # пацієнт → [(час, сума товарів, назви)]
    for s in later:
        items = [i for i in s.items if i.kind == "product"]
        if items:
            prod[s.patient_id].append((s.at, sum(float(i.total or 0) for i in items), ", ".join(i.name for i in items)))
    view = view if view in ("no", "yes", "doctors") else ""
    r = Report("cross", "Крос-продажі: догляд після процедур",
               f"Пацієнти, які за період оплатили процедуру в касі CRM, і чи купили вони домашній догляд — у тому ж чеку або протягом "
               f"{CROSS_DAYS} днів після. Лікар — той, хто надав процедуру.", empty="За період оплачених процедур у касі CRM не було.")
    r.filters = [{"name": "cat", "label": "Показати", "value": view,
                  "options": [("", "Усі пацієнти"), ("no", "Не купили догляд"), ("yes", "Купили догляд"), ("doctors", "За лікарями")]}]
    rows, seen = [], set()
    for s in sorted(sales, key=lambda x: x.at):
        svc = [i for i in s.items if i.kind == "service"]
        if not svc or s.patient_id in seen:
            continue
        seen.add(s.patient_id)
        got = [(t, m, n) for t, m, n in prod.get(s.patient_id, []) if s.at - timedelta(minutes=1) <= t <= s.at + timedelta(days=CROSS_DAYS)]
        doc = docs.get(next((i.doctor_id for i in svc if i.doctor_id), None), "—")
        rows.append({"at": s.at, "patient": s.patient.full_name if s.patient else "—", "doctor": doc,
                     "services": ", ".join(i.name for i in svc)[:140], "svc_sum": round(sum(float(i.total or 0) for i in svc), 2),
                     "care": round(sum(m for _, m, _ in got), 2), "care_items": "; ".join(n for _, _, n in got)[:160] or "—",
                     "bought": "так" if got else "ні", "_href": f"/patients/{s.patient_id}", "_ok": bool(got)})
    n, ok = len(rows), sum(1 for x in rows if x["_ok"])
    care = sum(x["care"] for x in rows)
    r.tiles = [("Пацієнтів після процедур", n, None, "int", ""), ("Купили догляд", ok, None, "int", f"{round(100 * ok / n) if n else 0}%"),
               ("Не купили", n - ok, None, "int", "потенціал для рекомендації"), ("Продано догляду", round(care, 2), None, "uah",
                                                                                 f"у середньому {round(care / ok):,} ₴ на покупця".replace(",", " ") if ok else "")]
    by = defaultdict(lambda: {"n": 0, "ok": 0, "care": 0.0})
    for x in rows:
        g = by[x["doctor"]]
        g["n"] += 1
        g["ok"] += x["_ok"]
        g["care"] += x["care"]
    if view == "doctors":
        r.cols = [Col("doctor", "Лікар"), Col("n", "Пацієнтів", "int", True), Col("ok", "Купили догляд", "int", True), Col("pct", "Конверсія", "pct"),
                  Col("care", "Продано догляду", "uah", True), Col("per", "Догляду на пацієнта", "uah")]
        for k, g in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
            r.rows.append({"doctor": k, "n": g["n"], "ok": g["ok"], "pct": round(100 * g["ok"] / g["n"]) if g["n"] else 0,
                           "care": round(g["care"], 2), "per": round(g["care"] / g["n"], 2) if g["n"] else 0})
    else:
        r.cols = [Col("patient", "Пацієнт"), Col("at", "Дата процедури", "date"), Col("doctor", "Лікар"), Col("services", "Процедури"),
                  Col("svc_sum", "Сума процедур", "uah", True), Col("bought", "Догляд"), Col("care", "Сума догляду", "uah", True),
                  Col("care_items", "Що купив(ла)")]
        r.rows = [x for x in rows if not view or (view == "yes") == x["_ok"]]
        r.rows.sort(key=lambda x: (x["_ok"], -x["svc_sum"]))
    r.chart = [(k, round(100 * g["ok"] / g["n"])) for k, g in sorted(by.items(), key=lambda kv: -kv[1]["n"]) if g["n"]][:12]
    r.chart_title = "Конверсія в догляд за лікарями, %"
    return r.finish()


# ---------------- LTV, джерела, ABC (чеки CleverBox + каса CRM) ----------------

def _year_checks(db: Session, d_to: date):
    from .reports import _checks
    return [c for c in _checks(db, d_to - timedelta(days=365), d_to) if c.patient_id]


def ltv(db: Session, d_from: date, d_to: date) -> Report:
    """Повторні продажі й LTV: пацієнти, що платили в періоді, — скільки принесли за 6 і 12 місяців, чи повертаються."""
    cs = _year_checks(db, d_to)
    a, b = _rng(d_from, d_to)
    six = datetime.combine(d_to - timedelta(days=182), datetime.min.time())
    by = defaultdict(lambda: {"p": 0.0, "m6": 0.0, "m12": 0.0, "days": set(), "first": None, "last": None})
    for c in cs:
        g = by[c.patient_id]
        amt = float(c.amount or 0)
        g["m12"] += amt
        g["m6"] += amt if c.date >= six else 0
        g["p"] += amt if a <= c.date <= b else 0
        g["days"].add(c.date.date())
        g["first"] = min(g["first"] or c.date, c.date)
        g["last"] = max(g["last"] or c.date, c.date)
    act = {pid: g for pid, g in by.items() if g["p"] > 0 or any(d_from <= d <= d_to for d in g["days"])}
    names = {p.id: p.full_name for p in db.scalars(select(Patient).where(Patient.id.in_(act)))} if act else {}
    r = Report("ltv", "Повторні продажі та LTV пацієнтів",
               "Пацієнти, які платили в обраному періоді. LTV 6 / 12 міс — скільки пацієнт приніс за останні 6 і 12 місяців до кінця періоду "
               "(чеки CleverBox і каса CRM). «Повторний» — платив щонайменше у два різні дні за 12 місяців.",
               empty="За період оплат від пацієнтів не було.")
    r.cols = [Col("patient", "Пацієнт"), Col("p", "За період", "uah", True), Col("m6", "LTV 6 міс", "uah", True), Col("m12", "LTV 12 міс", "uah", True),
              Col("visits", "Днів з оплатою за 12 міс", "int"), Col("first", "Перша оплата за 12 міс", "date"), Col("last", "Остання оплата", "date"),
              Col("kind", "Тип")]
    for pid, g in act.items():
        r.rows.append({"patient": names.get(pid, "—"), "p": round(g["p"], 2), "m6": round(g["m6"], 2), "m12": round(g["m12"], 2),
                       "visits": len(g["days"]), "first": g["first"], "last": g["last"],
                       "kind": "повторний" if len(g["days"]) > 1 else "один візит", "_href": f"/patients/{pid}"})
    r.rows.sort(key=lambda x: -x["m12"])
    n = len(r.rows)
    rep = sum(1 for x in r.rows if x["visits"] > 1)
    s12 = sum(x["m12"] for x in r.rows)
    top = sum(x["m12"] for x in r.rows[:max(1, n // 10)]) if n else 0
    r.tiles = [("Пацієнтів з оплатою", n, None, "int", ""), ("Повторні", rep, None, "int", f"{round(100 * rep / n) if n else 0}% повертаються"),
               ("Середній LTV 6 міс", round(sum(x["m6"] for x in r.rows) / n, 2) if n else 0, None, "uah", ""),
               ("Середній LTV 12 міс", round(s12 / n, 2) if n else 0, None, "uah", ""),
               ("Топ-10% пацієнтів дають", round(100 * top / s12) if s12 else 0, None, "pct", "від виручки за 12 міс")]
    bk = [("1 візит", 1, 1), ("2–3", 2, 3), ("4–6", 4, 6), ("7+", 7, 10 ** 6)]
    r.chart = [(k, sum(1 for x in r.rows if lo <= x["visits"] <= hi)) for k, lo, hi in bk]
    r.chart_title = "Пацієнти за кількістю днів з оплатою за 12 міс"
    return r.finish()


def src_sales(db: Session, d_from: date, d_to: date) -> Report:
    """Продажі за джерелами залучення пацієнта (поле «Джерело» в картці)."""
    from .reports import _checks
    cs = [c for c in _checks(db, d_from, d_to) if c.patient_id]
    year = _year_checks(db, d_from - timedelta(days=1))
    old = {c.patient_id for c in year}                       # платили за 12 міс до періоду — не нові
    pats = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_({c.patient_id for c in cs})))} if cs else {}
    NONE = "Не вказано"
    by = defaultdict(lambda: {"pats": set(), "new": set(), "sum": 0.0, "new_sum": 0.0, "n": 0})
    for c in cs:
        p = pats.get(c.patient_id)
        g = by[(p.source or "").strip() or NONE if p else NONE]
        amt = float(c.amount or 0)
        g["pats"].add(c.patient_id)
        g["sum"] += amt
        g["n"] += 1
        if c.patient_id not in old:
            g["new"].add(c.patient_id)
            g["new_sum"] += amt
    r = Report("src_sales", "Продажі за джерелами",
               "Звідки прийшли пацієнти, які платили в періоді, і скільки принесли. Джерело — поле «Джерело» в картці пацієнта. "
               "«Нові» — не мали оплат за 12 місяців до періоду. Порівняйте «Виручку від нових» з витратами на рекламу цього джерела.",
               empty="За період оплат від пацієнтів не було.")
    r.cols = [Col("src", "Джерело"), Col("pats", "Пацієнтів", "int", True), Col("new", "Нових", "int", True), Col("sum", "Виручка", "uah", True),
              Col("share", "Частка виручки", "pct"), Col("new_sum", "Виручка від нових", "uah", True), Col("avg", "Середній чек", "uah"),
              Col("per", "Виручка на пацієнта", "uah")]
    tot = sum(g["sum"] for g in by.values())
    for k, g in sorted(by.items(), key=lambda kv: -kv[1]["sum"]):
        r.rows.append({"src": k, "pats": len(g["pats"]), "new": len(g["new"]), "sum": round(g["sum"], 2),
                       "share": round(100 * g["sum"] / tot, 1) if tot else 0, "new_sum": round(g["new_sum"], 2),
                       "avg": round(g["sum"] / g["n"], 2) if g["n"] else 0, "per": round(g["sum"] / len(g["pats"]), 2) if g["pats"] else 0})
    none = by.get(NONE)
    r.tiles = [("Виручка", round(tot, 2), None, "uah", ""), ("Пацієнтів", sum(len(g["pats"]) for g in by.values()), None, "int", ""),
               ("Нових пацієнтів", sum(len(g["new"]) for g in by.values()), None, "int", ""),
               ("Без джерела", round(100 * none["sum"] / tot) if none and tot else 0, None, "pct", "виручки — заповнюйте «Джерело» в картці")]
    r.chart = [(x["src"], x["sum"]) for x in r.rows[:12]]
    r.chart_title = "Виручка за джерелами"
    return r.finish()


def abc(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """ABC-аналіз: A — позиції, що дають 80% виручки, B — наступні 15%, C — решта 5%."""
    from .reports import _positions
    kind = "product" if view == "product" else "service"
    agg = _positions(db, d_from, d_to, kind)
    r = Report("abc", "ABC-аналіз " + ("товарів" if kind == "product" else "послуг"),
               "A — позиції, що разом дають 80% виручки; B — наступні 15%; C — решта 5%. "
               "Група C — кандидати прибрати з прайсу чи складу або переглянути ціну.", empty="За період продажів не було.")
    r.filters = [{"name": "cat", "label": "Що аналізуємо", "value": "product" if kind == "product" else "",
                  "options": [("", "Послуги"), ("product", "Товари")]}]
    r.cols = [Col("cls", "Клас"), Col("name", "Позиція"), Col("qty", "К-сть", "num", True), Col("sum", "Виручка", "uah", True),
              Col("share", "Частка", "pct"), Col("cum", "Накопичено", "pct"), Col("patients", "Пацієнтів", "int")]
    tot = sum(x["sum"] for x in agg.values())
    cum = 0.0
    cnt = defaultdict(lambda: [0, 0.0])
    for name, x in sorted(agg.items(), key=lambda kv: -kv[1]["sum"]):
        before = 100 * cum / tot if tot else 0
        cum += x["sum"]
        cls = "A" if before < 80 else ("B" if before < 95 else "C")
        cnt[cls][0] += 1
        cnt[cls][1] += x["sum"]
        r.rows.append({"cls": cls, "name": name, "qty": round(x["qty"], 2), "sum": round(x["sum"], 2),
                       "share": round(100 * x["sum"] / tot, 1) if tot else 0, "cum": round(100 * cum / tot, 1) if tot else 0,
                       "patients": len(x["patients"])})
    n = len(r.rows)
    r.tiles = [(f"Клас {k}", cnt[k][0], None, "int",
                f"{round(100 * cnt[k][0] / n) if n else 0}% позицій · {round(100 * cnt[k][1] / tot) if tot else 0}% виручки") for k in "ABC"]
    r.tiles.append(("Виручка", round(tot, 2), None, "uah", f"{n} позицій"))
    r.chart = [(f"Клас {k}", round(cnt[k][1], 2)) for k in "ABC"]
    r.chart_title = "Виручка за класами"
    return r.finish()


# ---------------- утримання: хто не повернувся ----------------

def _activity(db: Session):
    """→ (останній візит/оплата, сума за весь час, чеків, {записані на майбутнє}) по пацієнтах: чеки CleverBox + каса CRM + записи."""
    from sqlalchemy import func
    from ..models import Appointment, Check
    from . import autonomy
    now = datetime.now()
    last: dict[int, datetime] = {}
    total = defaultdict(float)
    cnt = defaultdict(int)
    for pid, mx, sm, n in db.execute(select(Check.patient_id, func.max(Check.date), func.sum(Check.amount), func.count(Check.id))
                                     .where(Check.deleted.is_(False), Check.patient_id.is_not(None)).group_by(Check.patient_id)):
        last[pid], total[pid], cnt[pid] = mx, float(sm or 0), n
    since = autonomy.since(db)
    q = select(Sale.patient_id, func.max(Sale.at), func.sum(Sale.total), func.count(Sale.id)).where(Sale.status == "paid", Sale.patient_id.is_not(None))
    if since:
        q = q.where(Sale.at >= datetime.combine(since, datetime.min.time()))
    for pid, mx, sm, n in db.execute(q.group_by(Sale.patient_id)):
        last[pid] = max(last.get(pid) or mx, mx)
        total[pid] += float(sm or 0)
        cnt[pid] += n
    for pid, mx in db.execute(select(Appointment.patient_id, func.max(Appointment.start)).where(
            Appointment.start <= now, Appointment.status.in_(("done", "paid", "arrived")), Appointment.patient_id.is_not(None)).group_by(Appointment.patient_id)):
        last[pid] = max(last.get(pid) or mx, mx)
    booked = {pid for (pid,) in db.execute(select(Appointment.patient_id).where(Appointment.start > now,
                                                                                Appointment.status.notin_(("cancelled", "no_show"))))}
    return last, total, cnt, booked


def lost(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """Пацієнти без візиту й оплати понад N днів і без майбутнього запису — на сьогодні (період не впливає)."""
    days = int(view) if view in ("60", "180", "365") else 90
    now = datetime.now()
    last, total, cnt, booked = _activity(db)
    docs = {d.id: d.name for d in db.scalars(select(Doctor))}
    r = Report("lost", "Утримання: хто не повернувся",
               f"Пацієнти, які були в клініці, але не мають візиту чи оплати {days}+ днів і не записані на майбутнє — станом на сьогодні "
               "(обраний період не впливає). Відсортовано за тим, скільки пацієнт приніс за весь час: згори — ті, кого найважливіше повернути.",
               empty="Таких пацієнтів немає.")
    r.filters = [{"name": "cat", "label": "Без візиту", "value": str(days) if view else "",
                  "options": [("60", "60+ днів"), ("", "90+ днів"), ("180", "180+ днів"), ("365", "рік і більше")]}]
    r.cols = [Col("patient", "Пацієнт"), Col("phone", "Телефон"), Col("doctor", "Лікар"), Col("last", "Останній візит", "date"),
              Col("idle", "Днів без візиту", "int"), Col("checks", "Чеків", "int"), Col("total", "Приніс за весь час", "uah", True),
              Col("avg", "Середній чек", "uah")]
    cand = {pid: lt for pid, lt in last.items() if pid not in booked and (now - lt).days >= days}
    pats = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_(cand)))} if cand else {}
    bk = {"до пів року": 0, "пів року – рік": 0, "понад рік": 0}
    for pid, lt in cand.items():
        p = pats.get(pid)
        if not p:
            continue
        idle = (now - lt).days
        bk["до пів року" if idle < 180 else ("пів року – рік" if idle < 365 else "понад рік")] += 1
        r.rows.append({"patient": p.full_name, "phone": p.phone or "", "doctor": docs.get(p.doctor_id, ""), "last": lt, "idle": idle,
                       "checks": cnt.get(pid, 0), "total": round(total.get(pid, 0), 2),
                       "avg": round(total[pid] / cnt[pid], 2) if cnt.get(pid) else 0, "_href": f"/patients/{pid}"})
    r.rows.sort(key=lambda x: -x["total"])
    n = len(r.rows)
    top = r.rows[:max(1, n // 10)] if n else []
    r.tiles = [("Не повернулись", n, None, "int", f"без візиту {days}+ днів і без запису"),
               ("До пів року", bk["до пів року"], None, "int", "ще «теплі» — повертати першими"),
               ("Понад рік", bk["понад рік"], None, "int", ""),
               ("Топ-10% з них принесли", round(sum(x["total"] for x in top), 2), None, "uah", f"{len(top)} пацієнтів — дзвонити особисто")]
    r.chart, r.chart_title = list(bk.items()), "За давністю останнього візиту"
    return r.finish()


# ---------------- когорти нових, RFM, дні народження ----------------

MON = ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]


def _first_pay(db: Session) -> dict[int, datetime]:
    from sqlalchemy import func
    from ..models import Check
    first = dict(db.execute(select(Check.patient_id, func.min(Check.date)).where(Check.deleted.is_(False), Check.patient_id.is_not(None))
                            .group_by(Check.patient_id)).all())
    for pid, at in db.execute(select(Sale.patient_id, func.min(Sale.at)).where(Sale.status == "paid", Sale.patient_id.is_not(None)).group_by(Sale.patient_id)):
        if pid not in first or at < first[pid]:
            first[pid] = at
    return first


def cohorts(db: Session, d_from: date, d_to: date) -> Report:
    """Когорти нових пацієнтів за місяцем першої оплати: скільки повернулись за 30 / 60 / 90 днів."""
    from .reports import _checks
    today = date.today()
    y, m = d_to.year, d_to.month
    months = []
    for _ in range(12):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    start = date(months[0][0], months[0][1], 1)
    first = {pid: at for pid, at in _first_pay(db).items() if at.date() >= start}
    pays = defaultdict(list)
    for c in _checks(db, start, today):
        if c.patient_id in first:
            pays[c.patient_id].append((c.date, float(c.amount or 0)))
    r = Report("cohorts", "Когорти нових пацієнтів",
               "Нові пацієнти за місяцем найпершої оплати (12 місяців до кінця періоду) і частка тих, хто заплатив удруге протягом "
               "30 / 60 / 90 днів. «—» — стільки днів від кінця місяця ще не минуло. LTV 90 — у середньому приніс новий пацієнт за перші 90 днів.",
               empty="Нових пацієнтів за 12 місяців немає.")
    r.cols = [Col("month", "Місяць"), Col("new", "Нових", "int", True), Col("first", "Перший візит, сер.", "uah"), Col("r30", "Повернулись за 30 дн", "pct"),
              Col("r60", "за 60 дн", "pct"), Col("r90", "за 90 дн", "pct"), Col("ltv90", "LTV 90 дн", "uah"), Col("rev", "Принесли всього", "uah", True)]
    all_new = all_r90 = 0
    for yy, mm in months:
        ids = [pid for pid, at in first.items() if (at.year, at.month) == (yy, mm)]
        if not ids:
            continue
        end = (date(yy + 1, 1, 1) if mm == 12 else date(yy, mm + 1, 1)) - timedelta(days=1)

        def back(n):
            if (today - end).days < n:
                return None
            k = sum(1 for pid in ids if any(first[pid].date() < d.date() <= first[pid].date() + timedelta(days=n) for d, _ in pays[pid]))
            return round(100 * k / len(ids))
        fsum = sum(a for pid in ids for d, a in pays[pid] if d.date() == first[pid].date())
        l90 = sum(a for pid in ids for d, a in pays[pid] if d.date() <= first[pid].date() + timedelta(days=90))
        r90 = back(90)
        if r90 is not None:
            all_new += len(ids)
            all_r90 += round(r90 * len(ids) / 100)
        r.rows.append({"month": f"{MON[mm - 1]} {yy}", "new": len(ids), "first": round(fsum / len(ids), 2), "r30": back(30), "r60": back(60), "r90": r90,
                       "ltv90": round(l90 / len(ids), 2) if (today - end).days >= 90 else None,
                       "rev": round(sum(a for pid in ids for _, a in pays[pid]), 2)})
    n = sum(x["new"] for x in r.rows)
    r.tiles = [("Нових за 12 міс", n, None, "int", f"у середньому {round(n / len(r.rows))} на місяць" if r.rows else ""),
               ("Повертаються за 90 днів", round(100 * all_r90 / all_new) if all_new else 0, None, "pct", "за когортами, де 90 днів уже минуло"),
               ("Принесли нові за 12 міс", round(sum(x["rev"] for x in r.rows), 2), None, "uah", "")]
    r.chart, r.chart_title = [(x["month"], x["new"]) for x in r.rows], "Нових пацієнтів за місяцями"
    return r.finish()


SEGMENTS = [("new", "Нові", "перша оплата до 60 днів тому — довести до другого візиту"),
            ("loyal", "Постійні", "були за останні 60 днів і мають 4+ візити за рік — берегти, пропонувати нове"),
            ("active", "Активні", "були за останні 90 днів"),
            ("risk", "Під загрозою", "не були 3–6 місяців — зателефонувати зараз"),
            ("sleep", "Сплячі", "не були 6–12 місяців — акція на повернення"),
            ("lost", "Втрачені", "не були понад рік")]


def rfm(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """Сегменти бази за давністю, частотою й сумою оплат (на кінець періоду)."""
    from .reports import _checks
    end = datetime.combine(d_to, datetime.max.time())
    y1 = end - timedelta(days=365)
    first = _first_pay(db)
    P = defaultdict(lambda: {"last": None, "days": set(), "m12": 0.0})
    for c in _checks(db, d_to - timedelta(days=730), d_to):
        if not c.patient_id:
            continue
        g = P[c.patient_id]
        g["last"] = max(g["last"] or c.date, c.date)
        if c.date >= y1:
            g["days"].add(c.date.date())
            g["m12"] += float(c.amount or 0)

    def seg(pid, g):
        rec = (end - g["last"]).days
        if rec > 365:
            return "lost"
        if rec > 180:
            return "sleep"
        if rec > 90:
            return "risk"
        if pid in first and (end - first[pid]).days <= 60:
            return "new"
        return "loyal" if rec <= 60 and len(g["days"]) >= 4 else "active"
    segs = {pid: seg(pid, g) for pid, g in P.items()}
    view = view if view in dict((k, 1) for k, _, _ in SEGMENTS) else ""
    r = Report("rfm", "Сегменти пацієнтів (RFM)",
               "База за давністю останньої оплати, частотою й сумою за 12 місяців — на кінець періоду. Оберіть сегмент, щоб побачити список "
               "пацієнтів з телефонами для дзвінків чи розсилки.", empty="Оплат за два роки немає.")
    r.filters = [{"name": "cat", "label": "Сегмент", "value": view, "options": [("", "Усі сегменти")] + [(k, l) for k, l, _ in SEGMENTS]}]
    agg = defaultdict(lambda: {"n": 0, "m12": 0.0})
    for pid, sg in segs.items():
        agg[sg]["n"] += 1
        agg[sg]["m12"] += P[pid]["m12"]
    tot_n, tot_m = len(segs), sum(g["m12"] for g in P.values())
    if view:
        ids = [pid for pid, sg in segs.items() if sg == view]
        pats = {p.id: p for p in db.scalars(select(Patient).where(Patient.id.in_(ids)))} if ids else {}
        r.cols = [Col("patient", "Пацієнт"), Col("phone", "Телефон"), Col("last", "Остання оплата", "date"), Col("idle", "Днів тому", "int"),
                  Col("visits", "Візитів за рік", "int"), Col("m12", "Приніс за 12 міс", "uah", True)]
        for pid in ids:
            p, g = pats.get(pid), P[pid]
            if p:
                r.rows.append({"patient": p.full_name, "phone": p.phone or "", "last": g["last"], "idle": (end - g["last"]).days,
                               "visits": len(g["days"]), "m12": round(g["m12"], 2), "_href": f"/patients/{pid}"})
        r.rows.sort(key=lambda x: -x["m12"])
    else:
        r.cols = [Col("seg", "Сегмент"), Col("n", "Пацієнтів", "int", True), Col("share", "Частка бази", "pct"), Col("m12", "Виручка за 12 міс", "uah", True),
                  Col("rev_share", "Частка виручки", "pct"), Col("avg", "На пацієнта", "uah"), Col("todo", "Що робити")]
        for k, l, todo in SEGMENTS:
            g = agg.get(k)
            if g:
                r.rows.append({"seg": l, "n": g["n"], "share": round(100 * g["n"] / tot_n, 1) if tot_n else 0, "m12": round(g["m12"], 2),
                               "rev_share": round(100 * g["m12"] / tot_m, 1) if tot_m else 0, "avg": round(g["m12"] / g["n"], 2), "todo": todo,
                               "_href": f"/reports?r=rfm&cat={k}"})
    r.tiles = [("У базі з оплатами за 2 роки", tot_n, None, "int", ""),
               ("Постійні + активні", agg["loyal"]["n"] + agg["active"]["n"] + agg["new"]["n"], None, "int", "були за останні 90 днів"),
               ("Під загрозою", agg["risk"]["n"], None, "int", f"торік принесли {round(agg['risk']['m12']):,} ₴".replace(",", " ")),
               ("Сплячі й втрачені", agg["sleep"]["n"] + agg["lost"]["n"], None, "int", "")]
    r.chart, r.chart_title = [(l, agg[k]["n"]) for k, l, _ in SEGMENTS if k in agg], "Пацієнтів за сегментами"
    return r.finish()


def bdays(db: Session, d_from: date, d_to: date, view: str = "") -> Report:
    """Дні народження в періоді — привід зателефонувати; окремо ті, хто давно не був."""
    last, total, cnt, booked = _activity(db)
    now = datetime.now()
    view = "idle" if view == "idle" else ""
    r = Report("bdays", "Дні народження", "Пацієнти, в яких день народження припадає на обраний період. «Давно не були» — без візиту 90+ днів "
               "і без запису: привітання — найкращий привід повернути.", empty="У періоді немає днів народження (або дати народження не заповнені в картках).")
    r.filters = [{"name": "cat", "label": "Показати", "value": view, "options": [("", "Усі іменинники"), ("idle", "Давно не були")]}]
    r.cols = [Col("day", "Дата"), Col("patient", "Пацієнт"), Col("age", "Виповнюється", "int"), Col("phone", "Телефон"), Col("last", "Останній візит", "date"),
              Col("idle", "Днів без візиту", "int"), Col("state", "Запис"), Col("total", "Приніс за весь час", "uah", True)]
    span = (d_to - d_from).days
    n_idle = 0
    rows = []
    for p in db.scalars(select(Patient).where(Patient.birthday.is_not(None))):
        b = p.birthday
        hit = None
        for yr in {d_from.year, d_to.year}:
            try:
                d = date(yr, b.month, b.day)
            except ValueError:
                d = date(yr, 2, 28)
            if d_from <= d <= d_to:
                hit = d
        if not hit or span > 370:
            continue
        lt = last.get(p.id)
        idle = (now - lt).days if lt else None
        is_idle = p.id not in booked and (idle is None or idle >= 90)
        n_idle += is_idle
        if view and not is_idle:
            continue
        rows.append({"_d": hit, "day": f"{hit:%d.%m}", "patient": p.full_name, "age": hit.year - b.year, "phone": p.phone or "", "last": lt,
                     "idle": idle, "state": "записаний" if p.id in booked else ("давно не був" if is_idle else "—"),
                     "total": round(total.get(p.id, 0), 2), "_href": f"/patients/{p.id}"})
    rows.sort(key=lambda x: x["_d"])
    r.rows = rows
    r.tiles = [("Іменинників у списку", len(rows), None, "int", ""),
               ("Давно не були", n_idle, None, "int", "без візиту 90+ днів і без запису"),
               ("З телефоном", sum(1 for x in rows if x["phone"]), None, "int", "")]
    return r.finish()
