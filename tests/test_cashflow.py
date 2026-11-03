"""Cashflow: імпорт звіту CleverBox «Прибуток», каса від залишку на 01.01, тижні, оцінка з чеків."""
from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CashDay, Check
from app.services import cashflow as cf

HEAD = ["Дата", "Надходження (готівка)", "Надходження (безготівкова)", "Надходження разом", "Витрата", "Дохід"]


def test_cashflow_import_balance_and_autofill():
    settings.admin_password = "owner-pass-1"
    rows = [["31.12.2035", "100.00", "0.00", "100.00", "-50.00", "50.00"],
            ["02.01.2036", "1000.00", "2000.00", "3000.00", "-500.00", "2500.00"],
            ["01.01.2036", "0.00", "0.00", "0.00", "-1000.00", "-1000.00"]]
    with SessionLocal() as db:
        assert cf.is_profit_export(HEAD)
        cf.set_opening(db, date(2036, 1, 1), 377130)
        rep = cf.import_profit(db, rows, HEAD)
        assert rep["rows"] == 3
        rep = cf.import_profit(db, rows, HEAD)
        assert rep["updated"] == 3                          # без дублів
        r = cf.build(db, 2036)
        assert r["start_balance"] == 377130                 # рух 2035 до дати старту не рахується
        assert r["total"].income == 3000 and r["total"].expense == 1500 and r["balance"] == 377130 + 1500
        assert r["months"][0].weeks and r["months"][0].weeks[0].label.startswith("01.01.2036")
        db.add(Check(id=930001, date=datetime(2036, 1, 3, 12), amount=1500,
                     payments=[{"payment_name": "Готівка", "amount": 500}, {"payment_name": "Картка", "amount": 1000}]))
        db.commit()
        assert cf.auto_fill(db, date(2036, 1, 4)) == 2
        d3 = db.get(CashDay, date(2036, 1, 3))
        assert d3.source == "checks" and d3.cash_in == 500 and d3.card_in == 1000 and d3.expense == 0
        cf.import_profit(db, [["03.01.2036", "500", "1000", "1500", "-300", "1200"]], HEAD)
        assert db.get(CashDay, date(2036, 1, 3)).source == "cleverbox"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/reports?r=cashflow&year=2036").text
        assert "Контроль cashflow" in page and "Каса зараз" in page and "377 130" in page
    with SessionLocal() as db:
        cf.set_opening(db, date(2026, 1, 1), 377130)


def test_cleverbox_finance_probe_and_sync():
    from app.integrations import cb_finance
    from app.models import SyncState

    class FakeCB:
        def paged(self, method, body):
            if method != "finance/operations":
                raise RuntimeError("unknown method")
            return iter([
                {"date": "05.02.2037 10:00", "amount": 3000, "type": "income", "cashbox_name": "Каса готівка"},
                {"date": "05.02.2037 12:00", "amount": 5000, "type": "income", "cashbox_name": "Термінал ПриватБанк"},
                {"date": "05.02.2037 18:00", "amount": -1200, "type": "expense", "category_name": "Оренда"},
                {"date": "05.02.2037 19:00", "amount": 2000, "type": "transfer", "cashbox_name": "Каса готівка"},
            ])

    with SessionLocal() as db:
        for k in ("cleverbox_finance_method", "cleverbox_finance_probe"):
            st = db.get(SyncState, k)
            if st:
                db.delete(st)
        db.commit()
        rep = cb_finance.probe(db, FakeCB())
        assert rep["chosen"] == "finance/operations"
        r = cb_finance.sync_cash(db, date(2037, 2, 5), date(2037, 2, 5), FakeCB())
        assert r["transfers"] == 1
        cd = db.get(CashDay, date(2037, 2, 5))
        assert cd.source == "api" and cd.cash_in == 3000 and cd.card_in == 5000 and cd.expense == 1200
        for k in ("cleverbox_finance_method", "cleverbox_finance_probe"):
            db.delete(db.get(SyncState, k))
        db.commit()


def test_days_without_movement_are_not_gaps():
    with SessionLocal() as db:
        cf.set_opening(db, date(2038, 1, 1), 1000)
        cf.import_profit(db, [["02.01.2038", "100", "0", "100", "0", "100"], ["05.01.2038", "0", "50", "50", "-20", "30"],
                              ["03.02.2038", "10", "0", "10", "0", "10"]], HEAD)
        r = cf.build(db, 2038)
        assert r["gaps"] == [] and r["balance"] == 1140
        cf.set_opening(db, date(2026, 1, 1), 377130)


def test_cashflow_from_journal():
    from datetime import date, datetime
    from app.models import SessionLocal, CbFinOp
    from app.services import cashflow as cf
    with SessionLocal() as db:
        db.add_all([CbFinOp(id=990001, at=datetime(2025, 3, 30, 10), kind="income", amount=1000, account="Готівка", article="Оплата послуг, товарів"),
                    CbFinOp(id=990002, at=datetime(2025, 3, 31, 11), kind="income", amount=500, account="Термінал", article="Оплата послуг, товарів"),
                    CbFinOp(id=990003, at=datetime(2025, 4, 1, 12), kind="expense", amount=300, account="Готівка", article="Оренда"),
                    CbFinOp(id=990004, at=datetime(2025, 3, 31, 12), kind="transfer", amount=9999, account="Готівка", article=""),
                    CbFinOp(id=990005, at=datetime(2025, 3, 31, 13), kind="income", amount=777, account="Бонуси", article="Оплата послуг, товарів")])
        db.commit()
        r = cf.build(db, 2025)
        assert r["source"] == "journal"
        # 31.03.2025 — понеділок; тиждень 31.03–06.04 (четвер 03.04) цілком іде у квітень
        mar = next(b for b in r["months"] if b.label == "Бер")
        assert round(mar.income) == 1000 and round(mar.cash_in) == 1000 and mar.expense == 0 and mar.weeks[-1].end == date(2025, 3, 30)
        apr = next(b for b in r["months"] if b.label == "Кві")
        assert apr.weeks[0].start == date(2025, 3, 31) and round(apr.income) == 500 and round(apr.expense) == 300
        assert round(sum(w.net for w in apr.weeks)) == round(apr.net)
        from app.models import CashDay
        db.add(CashDay(day=date(2025, 3, 30), cash_in=4000, card_in=1000, income=5000, expense=200, source="cleverbox")); db.commit()
        mar2 = next(b for b in cf.build(db, 2025)["months"] if b.label == "Бер")
        assert round(mar2.income) == 5000 and round(mar2.expense) == 200          # звіт «Прибуток» має пріоритет до переходу
        db.delete(db.get(CashDay, date(2025, 3, 30))); db.commit()
        jan = r["months"][0]
        assert jan.weeks[0].start == date(2025, 1, 1)          # рік починається з 01.01, тиждень 30.12–05.01 — у січні
        for i in (990001, 990002, 990003, 990004, 990005):
            db.delete(db.get(CbFinOp, i))
        db.commit()


def test_journal_skips_test_sales():
    from datetime import date, datetime
    from app.models import SessionLocal, Sale, SalePayment
    from app.services import finops as fo
    with SessionLocal() as db:
        s = Sale(at=datetime(2024, 5, 5, 12), status="paid", total=1800, fiscal={"test_sale": True})
        db.add(s); db.flush()
        db.add(SalePayment(sale_id=s.id, method="Готівка ФОП Колган", amount=1800)); db.commit()
        rows = [r for r in fo.journal(db, date(2024, 5, 5), date(2024, 5, 5)) if r.sale_id == s.id]
        assert rows and rows[0].test                                         # у журналі видно, позначено «тест»
        from app.services import cashflow as cf
        assert not [x for x in cf.journal_days(db, date(2024, 5, 5), date(2024, 5, 5)) if x.income]   # у Cashflow — ні
