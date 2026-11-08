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
                        CbFinOp(id=860005, at=datetime(2022, 7, 24, 12), kind="expense", article="Реклама Google", amount=700, account="Готівка", source="import")])
            db.commit()
            r = pm.build(db, 2022)
            m7 = next(x for x in r["rows"] if x["m"] == "2022-07")
            assert m7["rev"] == 12000 and m7["rev_s"] == 10000 and m7["rev_p"] == 2000 and m7["cogs_p"] == 800
            assert m7["gross"] == 12000 - 800 - m7["cogs_s"] and m7["salary"] == 3000
            assert m7["opex_g"]["rent"] == 2500 and m7["opex_g"]["marketing"] == 700 and m7["opex"] == 3200
            assert m7["below"]["goods"] == 5000 and m7["below"]["owner"] == 4000
            assert m7["op"] == m7["gross"] - 3000 - 3200 - m7["amort"] and m7["op_pct"] is not None
            assert r["t"]["rev"] >= 12000 and r["has_data"] and r["prev"] is None
            r23 = pm.build(db, 2023)
            assert r23["prev"] is not None and r23["pt"]["full_rev"] >= 12000
            assert len(pm.to_xlsx(r)) > 1000
        t = c.get("/reports?r=pnlm&year=2022").text
        assert "P&L по місяцях" in t and "Валовий прибуток" in t and "Довідково" in t
        assert c.get("/reports?r=pnlm&year=2022&fmt=xlsx").status_code == 200
        assert 'href="/reports?r=pnlm"' in c.get("/reports").text
