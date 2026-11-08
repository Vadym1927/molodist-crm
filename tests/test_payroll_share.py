"""Звіт «Фонд оплати праці»: виплати по ролях і співробітниках проти виручки, %, Excel, сторінка."""
from datetime import datetime, date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbFinOp, Check, Doctor, FinOp
from app.services import payroll_share as ps


def test_payroll_share():
    settings.admin_password = "owner-pass-1"
    assert ps.is_salary("Персонал Зарплата", set()) and ps.is_salary("Аванс", set()) and not ps.is_salary("Податки ЄСВ", set())
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            doc = Doctor(name="Лікарка Тест Зп", position="Лікар-косметолог", active=True)
            adm = Doctor(name="Адмін Тест Зп", position="Адміністратор", active=True)
            db.add_all([doc, adm]); db.flush()
            db.add_all([Check(id=7700001, date=datetime(2023, 5, 3, 12), amount=10000, items=[{"item_type": "service", "item_name": "A", "quantity": 1, "payd_amount": 8000, "initiator_name": "Лікарка Тест Зп"},
                                                                                           {"item_type": "product", "item_name": "B", "quantity": 1, "payd_amount": 2000, "initiator_name": "Лікарка Тест Зп"}], payments=[]),
                        Check(id=7700002, date=datetime(2023, 6, 3, 12), amount=5000, items=[{"item_type": "service", "item_name": "A", "quantity": 1, "payd_amount": 5000, "initiator_name": "Невідомий Майстер"}], payments=[]),
                        CbFinOp(id=870001, at=datetime(2023, 5, 20, 12), kind="expense", article="Зарплата", amount=3000, counterparty="Лікарка Тест Зп", account="Готівка", source="import"),
                        CbFinOp(id=870002, at=datetime(2023, 5, 21, 12), kind="expense", article="Аванс", amount=1000, counterparty="Адмін Тест Зп", account="Готівка", source="import"),
                        CbFinOp(id=870003, at=datetime(2023, 6, 21, 12), kind="expense", article="Зарплата", amount=700, counterparty="Хтось Невідомий", account="Готівка", source="import"),
                        CbFinOp(id=870004, at=datetime(2023, 6, 22, 12), kind="expense", article="Закупка товара", amount=9999, counterparty="MERZ", account="Готівка", source="import"),
                        FinOp(day=date(2023, 6, 5), kind="expense", category="Персонал Зарплата", amount=500, cashbox="Готівка", doctor_id=adm.id)])
            db.commit()
            r = ps.build(db, 2023)
            m5 = next(x for x in r["months"] if x["m"] == "2023-05")
            assert m5["rev"] == 10000 and m5["doctor"] == 3000 and m5["admin"] == 1000 and m5["fot"] == 4000 and m5["pct"] == 40.0
            m6 = next(x for x in r["months"] if x["m"] == "2023-06")
            assert m6["rev"] == 5000 and m6["admin"] == 500 and m6["other"] == 700 and m6["doctor"] == 0
            assert r["rev"] == 15000 and r["fot"] == 5200 and r["rev_unassigned"] == 5000
            e = {x["name"]: x for x in r["emps"]}
            assert e["Лікарка Тест Зп"]["rev"] == 10000 and e["Лікарка Тест Зп"]["paid"] == 3000 and e["Лікарка Тест Зп"]["pct"] == 30.0 and e["Лікарка Тест Зп"]["per_uah"] == 3.33
            assert e["Адмін Тест Зп"]["paid"] == 1500 and e["Адмін Тест Зп"]["role"] == "admin" and e["Адмін Тест Зп"]["pct"] is None
            assert {g["key"] for g in r["groups"]} == {"doctor", "admin"} and dict(r["other_names"])["Хтось Невідомий"] == 700
            rm = ps.build(db, 2023, 5)
            assert rm["fot"] == 4000 and len(rm["months"]) == 1
            assert ps.build(db, None)["fot"] >= 5200
            assert len(ps.to_xlsx(r)) > 1000
        t = c.get("/reports?r=payroll_share&year=2023").text
        assert "Фонд оплати праці" in t and "Лікарка Тест Зп" in t and "Хто скільки коштує" in t
        assert c.get("/reports?r=payroll_share&year=2023&month=5&fmt=xlsx").status_code == 200
        assert 'href="/reports?r=payroll_share"' in c.get("/reports").text
