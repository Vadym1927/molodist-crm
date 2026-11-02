"""Cashflow клініки: рух коштів по днях → тижні (Пн–Нд) → місяці, каса наростаючим підсумком.

Джерела:
- звіт CleverBox «Прибуток» (Excel Profit_*.xls: Дата, Надходження готівка/безготівка/разом, Витрата, Дохід) —
  повні дані з витратами; вантажиться будь-якими періодами, дні перезаписуються (без дублів);
- автоматично після кожної синхронізації: для днів, яких ще немає у звіті, — надходження з чеків CleverBox
  (готівка / безготівка за способом оплати), витрати 0 — позначаються як «оцінка» до наступного імпорту звіту.
Каса = залишок на дату старту (SyncState cash_opening) + сума чистого руху від цієї дати.

Якщо перенесено журнал фіноперацій CleverBox (CbFinOp) — рух рахується з журналу фіноперацій CRM
(операції CleverBox до дати переходу + чеки каси CRM + операції, внесені в CRM): ті самі цифри, що у «Фінанси → Фіноперації».
Переміщення між рахунками, оплати не грошима (бонуси, сертифікати, баланс) і статті «не в дохід» не рахуються."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from ..models import CashDay, Check, SyncState

MONTHS = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"]
DEFAULT_OPENING = {"date": "2026-01-01", "amount": 377130.0}


def is_profit_export(header: list) -> bool:
    h = [str(x or "").strip().lower() for x in header]
    return "дата" in h and any(x.startswith("надходження") for x in h) and "витрата" in h


def _n(v) -> float:
    try:
        return float(str(v).replace(" ", "").replace(",", ".")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _d(v) -> date | None:
    t = str(v or "").strip()[:10]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def import_profit(db: Session, rows: list[list], header: list) -> dict:
    h = [str(x or "").strip().lower() for x in header]

    def idx(*names):
        for nm in names:
            for i, x in enumerate(h):
                if x == nm or x.startswith(nm):
                    return i
        return None
    i_d, i_c, i_b = idx("дата"), idx("надходження (готівка)", "надходження готівка"), idx("надходження (безготівкова)", "надходження безгот")
    i_t, i_e = idx("надходження разом"), idx("витрата")
    rep = {"type": "CleverBox: прибуток (cashflow)", "rows": 0, "updated": 0, "skipped": 0, "errors": []}
    days = []
    for row in rows:
        d = _d(row[i_d]) if i_d is not None and i_d < len(row) else None
        if not d:
            rep["skipped"] += 1
            continue
        cash = _n(row[i_c]) if i_c is not None else 0.0
        card = _n(row[i_b]) if i_b is not None else 0.0
        tot = _n(row[i_t]) if i_t is not None else cash + card
        exp = abs(_n(row[i_e])) if i_e is not None else 0.0
        cd = db.get(CashDay, d)
        if cd:
            rep["updated"] += 1
        else:
            cd = CashDay(day=d)
            db.add(cd)
        cd.cash_in, cd.card_in, cd.income, cd.expense = cash, card, tot, exp
        cd.source, cd.updated_at = "cleverbox", datetime.utcnow()
        rep["rows"] += 1
        days.append(d)
    if days:
        rep["period"] = f"{min(days):%d.%m.%Y} — {max(days):%d.%m.%Y}"
        rep["errors"].append(f"Період у файлі: {rep['period']}")
    db.commit()
    return rep


def _is_cash(name: str) -> bool:
    n = (name or "").lower()
    return "готів" in n or "cash" in n or "налич" in n


def auto_fill(db: Session, today: date | None = None) -> int:
    """Дні після останнього дня зі звіту CleverBox (до сьогодні) — надходження з чеків, позначка «оцінка»."""
    today = today or date.today()
    last = db.scalar(select(func.max(CashDay.day)).where(CashDay.source == "cleverbox"))
    if not last:
        return 0
    start = last + timedelta(days=1)
    if start > today:
        return 0
    agg = defaultdict(lambda: [0.0, 0.0])
    for c in db.scalars(select(Check).where(Check.deleted.is_(False), Check.date >= datetime.combine(start, datetime.min.time()),
                                            Check.date <= datetime.combine(today, datetime.max.time()))):
        pays = c.payments or []
        if pays:
            for p in pays:
                agg[c.date.date()][0 if _is_cash(p.get("payment_name")) else 1] += float(p.get("amount") or 0)
        else:
            agg[c.date.date()][1] += c.amount or 0
    n = 0
    d = start
    while d <= today:
        cd = db.get(CashDay, d)
        if cd is None or cd.source == "checks":
            cash, card = agg.get(d, [0.0, 0.0])
            cd = cd or CashDay(day=d)
            cd.cash_in, cd.card_in, cd.income, cd.expense = round(cash, 2), round(card, 2), round(cash + card, 2), 0.0
            cd.source, cd.updated_at = "checks", datetime.utcnow()
            db.merge(cd)
            n += 1
        d += timedelta(days=1)
    db.commit()
    return n


def opening(db: Session) -> dict:
    st = db.get(SyncState, "cash_opening")
    try:
        v = json.loads(st.value) if st and st.value else DEFAULT_OPENING
        return {"date": date.fromisoformat(v["date"]), "amount": float(v["amount"])}
    except (ValueError, KeyError, TypeError):
        return {"date": date.fromisoformat(DEFAULT_OPENING["date"]), "amount": DEFAULT_OPENING["amount"]}


def set_opening(db: Session, d: date, amount: float) -> None:
    st = db.get(SyncState, "cash_opening") or SyncState(key="cash_opening")
    st.value, st.updated_at = json.dumps({"date": d.isoformat(), "amount": amount}), datetime.utcnow()
    db.merge(st)
    db.commit()


@dataclass
class Day:
    day: date
    cash_in: float = 0.0
    card_in: float = 0.0
    income: float = 0.0
    expense: float = 0.0
    source: str = "journal"

    @property
    def net(self) -> float:
        return round((self.income or 0) - (self.expense or 0), 2)


def has_journal(db: Session) -> bool:
    from ..models import CbFinOp
    return bool(db.scalar(select(func.count()).select_from(CbFinOp)))


def journal_days(db: Session, d_from: date, d_to: date) -> list[Day]:
    """Рух по днях з журналу фіноперацій (як у «Фінанси → Фіноперації» з тими самими правилами підсумків)."""
    from . import finops as fo, clinic
    ex = clinic.excluded_income(db)
    days: dict[date, Day] = {}
    for r in fo.journal(db, d_from, d_to):
        if r.kind == "transfer" or not r.money or not fo.is_money(r.account) and r.src == "cb":
            continue
        if r.kind == "income" and r.article.strip().lower() in ex:
            continue
        d = r.at.date()
        x = days.setdefault(d, Day(d))
        if r.kind == "income":
            x.income += r.amount
            if _is_cash(r.account):
                x.cash_in += r.amount
            else:
                x.card_in += r.amount
        else:                                   # витрата, сторно, повернення
            x.expense += r.amount
    out = sorted(days.values(), key=lambda x: x.day)
    for x in out:
        x.cash_in, x.card_in, x.income, x.expense = (round(v, 2) for v in (x.cash_in, x.card_in, x.income, x.expense))
    return out


@dataclass
class Bucket:
    label: str
    start: date
    end: date
    cash_in: float = 0.0
    card_in: float = 0.0
    income: float = 0.0
    expense: float = 0.0
    days: int = 0
    estimated: int = 0                 # днів з оцінкою з чеків (без витрат)
    balance: float | None = None       # каса на кінець періоду
    weeks: list = field(default_factory=list)

    @property
    def net(self):
        return round(self.income - self.expense, 2)

    def add(self, cd: CashDay):
        self.cash_in += cd.cash_in or 0
        self.card_in += cd.card_in or 0
        self.income += cd.income or 0
        self.expense += cd.expense or 0
        self.days += 1
        self.estimated += int(getattr(cd, "source", "") == "checks")


def _balance_at(op: dict, alldays: list, d: date) -> float | None:
    """Каса на кінець дня d: залишок на дату старту + чистий рух від старту до d включно."""
    if d < op["date"] - timedelta(days=1):
        return None
    return round(op["amount"] + sum(cd.net for cd in alldays if op["date"] <= cd.day <= d), 2)


def build(db: Session, year: int) -> dict:
    op = opening(db)
    y0, y1 = date(year, 1, 1), date(year, 12, 31)
    src = "journal" if has_journal(db) else "profit"
    if src == "journal":
        # до дати переходу на CRM — звіт CleverBox «Прибуток» (якщо завантажений за ці дні), решта — журнал фіноперацій;
        # від дати переходу — лише CRM (каса й операції CRM)
        from . import autonomy
        cut = autonomy.since(db) or date.max
        jd = {x.day: x for x in journal_days(db, min(op["date"], y0), min(y1, date.today())) if x.income or x.expense}
        for cd in db.scalars(select(CashDay).where(CashDay.source == "cleverbox", CashDay.day < cut, CashDay.day <= y1)):
            jd[cd.day] = Day(cd.day, cd.cash_in or 0, cd.card_in or 0, cd.income or 0, cd.expense or 0, "cleverbox")
        alldays = sorted(jd.values(), key=lambda x: x.day)
    else:
        alldays = db.scalars(select(CashDay).where(CashDay.day <= y1).order_by(CashDay.day)).all()
    start_balance = _balance_at(op, alldays, y0 - timedelta(days=1))
    by_month = defaultdict(list)
    for cd in alldays:
        if cd.day >= y0:
            by_month[cd.day.month].append(cd)
    last_day = max((cd.day for cd in alldays if cd.day >= y0), default=None)
    months, gaps = [], []
    first_day = alldays[0].day if alldays else None
    # Тижні Пн–Нд цілими: тиждень належить тому місяцю, де більше його днів (місяць четверга), і не ділиться між місяцями.
    # Межі року — 01.01 і 31.12 (перший / останній тиждень року обрізаються датами року).
    # Місяць = сума його тижнів; каса на кінець місяця — на кінець його останнього тижня.
    wk_by_month: dict[int, list] = defaultdict(list)
    w = y0 - timedelta(days=y0.weekday())
    while w <= y1:
        ws, we = max(w, y0), min(w + timedelta(days=6), y1)
        thu = w + timedelta(days=3)
        m = thu.month if thu.year == year else (1 if thu < y0 else 12)
        wk_by_month[m].append((ws, we))
        w += timedelta(days=7)
    in_year = [cd for cd in alldays if cd.day >= y0]
    for m in range(1, 13):
        wks = wk_by_month.get(m) or []
        if not wks:
            continue
        ms, me = wks[0][0], wks[-1][1]
        if last_day is None or ms > last_day:
            break
        b = Bucket(MONTHS[m - 1], ms, me)
        upto = min(me, last_day)
        for ws, we in wks:
            if ws > upto:
                break
            wb = Bucket(f"{ws:%d.%m.%Y} — {we:%d.%m.%Y}", ws, we)
            for cd in in_year:
                if ws <= cd.day <= we:
                    wb.add(cd)
                    b.add(cd)
            b.weeks.append(wb)
        b.balance = _balance_at(op, alldays, upto)
        b.partial = me > last_day
        # дні без руху всередині періоду (вихідні, свята) — не пропуск; пропуск — лише цілий місяць без жодного дня даних
        if b.days == 0 and not (first_day and upto < first_day):
            gaps.append(b.label)
        months.append(b)
    # дані починаються пізніше за дату залишку каси — рух між ними невідомий
    if first_day and (first_day - max(op["date"], y0)).days > 7 and first_day >= y0:   # 1–7 січня можуть бути без руху
        gaps.insert(0, f"{max(op['date'], y0):%d.%m} — {first_day - timedelta(days=1):%d.%m.%Y}")
        months = [b for b in months if b.end >= first_day] or months
    tot = Bucket("YTD", y0, y1)
    for b in months:
        for k in ("cash_in", "card_in", "income", "expense", "days", "estimated"):
            setattr(tot, k, getattr(tot, k) + getattr(b, k))
    full = [b for b in months if b.days]
    return {"year": year, "opening": op, "source": src, "start_balance": start_balance, "months": months, "total": tot,
            "balance": months[-1].balance if months else start_balance,
            "best": max(full, key=lambda b: b.net, default=None), "worst": min(full, key=lambda b: b.net, default=None),
            "neg_months": sum(1 for b in full if b.net < 0), "avg_net": round(tot.net / len(full)) if full else 0,
            "cash_share": round(100 * tot.cash_in / tot.income, 1) if tot.income else 0,
            "coverage": (db.scalar(select(func.min(CashDay.day)).where(CashDay.source == "cleverbox")),
                         db.scalar(select(func.max(CashDay.day)).where(CashDay.source == "cleverbox"))),
            "gaps": gaps, "last_day": last_day,
            "max": max([max(b.income, b.expense) for b in months] + [1]),
            "net_max": max([abs(b.net) for b in months] + [1])}
