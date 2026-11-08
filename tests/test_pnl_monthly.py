"""P&L по місяцях: виручка за чеками, собівартість товарів, ЗП, опекс за групами, довідкові рядки, Excel, сторінка."""
from datetime import datetime, date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbFinOp, Check, Product, ProductPurchase
from app.services import pnl_monthly as pm


def test_pnl_monthly():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            pr = Product(name="Крем PNL Тест", name_norm="крем pnl тест", warehouse="vitrina", price=2000, cost=800, active=True)
            db.add(pr); db.flush()
            db.add(ProductPurchase(product_id=pr.id, bought_on=date(2022, 1, 5), qty=10, unit_cost=800))
            db.add_all([Check(id=7900001, date=datetime(2022, 7, 3, 12), amount=12000, items=[{"item_type": "service", "item_name": "Послуга PNL", "quantity": 1, "payd_amount": 10000, "initiator_name": "X"},
                                                                                           {"item_type": "product", "item_name": "Крем PNL Тест", "quantity": 1, "payd_amount": 2000, "initiator_name": "X"}], payments=[]),
                        CbFinOp(id=860001, at=datetime(2022, 7, 20, 12), kind="expense", article="Зарплата", amount=3000, counterparty="Хтось", account="Готівка", source="import"),
                        CbFinOp(id=860002, at=datetime(2022, 7, 21, 12), kind="expense", article="Оренда приміщення", amount=2500, account="Готівка", source="import"),
                        CbFinOp(id=860003, at=datetime(2022, 7, 22, 12), kind="expense", article="Закупка товара", amount=5000, counterparty="MERZ", account="Готівка", source="import"),
                        CbFinOp(id=860004, at=datetime(2022, 7, 23, 12), kind="expense", article="Дивіденди", amount=4000, account="Готівка", source="import"),
                        CbFinOp(id=860005, at=datetime(2022, 7, 24, 12), kind="expense", article="Реклама Google", amount=700, account="Готівка", source="import"),
                        CbFinOp(id=860006, at=datetime(2022, 7, 25, 12), kind="expense", article="Податки ЄСВ на ЗП ФОП Колган", amount=660, account="Готівка", source="import"),
                        CbFinOp(id=860009, at=datetime(2022, 7, 25, 12), kind="expense", article="Зарплата Керівник", amount=2000, account="Готівка", source="import"),
                        CbFinOp(id=860007, at=datetime(2022, 7, 26, 12), kind="expense", article="Зобовʼязання Розтермінування MONOBANK", amount=7904, account="Готівка", source="import"),
                        CbFinOp(id=860008, at=datetime(2022, 7, 27, 12), kind="income", article="Поповнення балансу", amount=1500, counterparty="Пацієнт", account="Готівка", source="import"),
                        Check(id=7900002, date=datetime(2022, 7, 4, 12), amount=3000, items=[{"item_type": "certificate", "item_name": "Сертифікат", "quantity": 1, "payd_amount": 3000}], payments=[])])
            db.commit()
            r = pm.build(db, 2022)
            m7 = next(x for x in r["rows"] if x["m"] == "2022-07")
            assert m7["rev"] == 12000 and m7["rev_s"] == 10000 and m7["rev_p"] == 2000 and m7["cogs_p"] == 800
            assert m7["gross"] == 12000 - 800 - m7["cogs_s"] - m7["amort"]
            assert m7["opex_g"]["rent"] == 2500 and m7["opex_g"]["ads"] == 700 and m7["opex_g"]["salary"] == 5660 and m7["opex"] == 8860   # податок з ЗП → до ЗП адмінів (лікарів немає)
            assert m7["purchase"] == 5000 and m7["owner"] == 4000 and m7["labor"] == 0 and m7["adm_tax"] == 660
            assert m7["finance"] == 7904 and m7["capex"] == 0 and m7["prepaid"] == 4500 and m7["rev"] == 12000   # кредит і передоплати — поза P&L
            assert m7["op"] == m7["gross"] - 8860 and m7["net"] == m7["op"] - m7["capex"] - m7["oneoff"] and m7["net_pct"] is not None
            assert m7["cash_result"] == m7["net"] - 7904 - 4000
            assert m7["owner_pay"] == 2000 and m7["net_before_owner"] == m7["net"] + 2000 and m7["nbo_pct"] is not None
            assert m7["k_pat"] == 0 and m7["gross_p"] == 1200
            assert r["t"]["rev"] >= 12000 and r["has_data"] and r["prev"] is None
            r23 = pm.build(db, 2023)
            assert r23["prev"] is not None and r23["pt"]["full_rev"] >= 12000
            pm.plan_save(db, 2022, {"2022-07": {"rev": 10000, "opex": 7000, "net": 1000}})
            r = pm.build(db, 2022)
            m7 = next(x for x in r["rows"] if x["m"] == "2022-07")
            assert m7["plan"]["rev"] == 10000 and m7["plan_rev_pct"] == 20.0 and r["t"]["plan"]["rev"] == 10000
            assert len(pm.to_xlsx(r)) > 1000
        rp_ = c.post("/reports/pnlm/plan", data={"year": "2022", "rev_7": "11 000", "net_7": "", "opex_7": "6500"}, follow_redirects=False)
        assert rp_.status_code == 303
        with SessionLocal() as db:
            assert pm.plan_get(db, 2022)["2022-07"] == {"rev": 11000.0, "opex": 6500.0}
        t = c.get("/reports?r=pnlm&year=2022").text
        assert "план доходу" in t and "Cashflow · поза P&L" in t and "Прибуток до зарплати власника" in t and "Валовий внесок напрямків" in t
        assert "P&L по місяцях" in t and "Валовий прибуток" in t and "Довідково" in t and "KPI" in t
        assert c.get("/reports?r=pnlm&year=2022&fmt=xlsx").status_code == 200
        assert 'href="/reports?r=pnlm"' in c.get("/reports").text


def test_classify():
    from app.services.pnl_monthly import classify
    assert classify("Оренда прим.Клініки", False, None) == ("opex", "rent")
    assert classify("Персонал Зарплата", True, "doctor") == ("cogs_labor", "doctor")
    assert classify("Персонал Зарплата", True, "admin") == ("opex", "salary")
    assert classify("Персонал Копоратив", True, "admin") == ("opex", "staff")
    assert classify("Реклама Google Ads", False, None) == ("opex", "ads")
    assert classify("Маркетолог", False, None) == ("opex", "marketing")
    assert classify("Програми CleverBox", False, None) == ("opex", "software")
    assert classify("Комісія банку Еквайринг MonoBank", False, None) == ("opex", "bank")
    assert classify("Господарські Кава ,Чай", False, None) == ("opex", "household")
    assert classify("Податки ЄСВ ФОП Колган", False, None) == ("opex", "tax")
    assert classify("Капітальні витрати Ремонтні роботи прим. клініки", False, None) == ("capex", "capex")
    assert classify("Зобовʼязання Розтермінування MONOBANK", False, None) == ("finance", "finance")
    assert classify("Податки ЄСВ на ЗП ФОП Колган", False, None) == ("payroll_tax", "payroll_tax")
    assert classify("Податки ЄП ФОП Колган", False, None) == ("opex", "tax")
    assert classify("Навчання персоналу", False, None) == ("oneoff", "oneoff")
    assert classify("Допомога ЗСУ", False, None) == ("oneoff", "oneoff")
    assert classify("Девіденди", False, None) == ("owner", "owner")
    assert classify("Закупка косметика вітрина", False, None) == ("purchase", "purchase")
    assert classify("Щось дивне", False, None) == ("opex", "other")


def test_history_from_visits():
    from app.models import Visit, ProductSale, Patient, Product
    with SessionLocal() as db:
        p = Patient(full_name="Історична Пацієнтка", phone="+380000000777"); db.add(p); db.flush()
        pr = Product(name="Крем Історія", name_norm="крем історія", warehouse="vitrina", price=1000, cost=400, active=True); db.add(pr); db.flush()
        db.add_all([Visit(source="cb_service_sale", external_id="h1", patient_id=p.id, service_name="Пілінг Історичний", starts_at=datetime(2022, 2, 10, 12), status="done", amount=2500),
                    Visit(source="cb_service_sale", external_id="h2", patient_id=p.id, service_name="Пілінг Історичний", starts_at=datetime(2022, 2, 20, 12), status="done", amount=2500),
                    Visit(source="cb_service_sale", external_id="h3", patient_id=p.id, service_name="Скасований", starts_at=datetime(2022, 2, 21, 12), status="cancelled", amount=9999),
                    ProductSale(external_id="hp1", patient_id=p.id, product_id=pr.id, product_name="Крем Історія", quantity=2, amount=2000, sold_at=datetime(2022, 2, 11, 12))])
        db.commit()
        r = pm.build(db, 2022)
        m2 = next(x for x in r["rows"] if x["m"] == "2022-02")
        assert m2["from_visits"] and m2["rev_s"] == 5000 and m2["rev_p"] == 2000 and m2["cogs_p"] == 800
        assert m2["k_pat"] == 1 and m2["k_vis"] == 2 and m2["rev_cat"].get("Без категорії") == 5000
