"""Звіт «Отримано грошей від пацієнтів»: частки, ABC, повернення, пошук, Excel, сторінка."""
from datetime import datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbFinOp, Patient, Sale, SaleItem, SalePayment
from app.services import patient_money as pm


def test_patient_money():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            p1 = Patient(full_name="Тестова Анна Петрівна", phone="+380000000101")
            p2 = Patient(full_name="Тестовий Богдан Іванович", phone="+380000000102")
            db.add_all([p1, p2]); db.flush()
            rows = [(1, "income", "Оплата послуг, товарів", 8000, "Тестова Анна Петрівна", "Готівка"),
                    (2, "income", "Оплата послуг, товарів", 2000, "Тестовий Богдан Іванович", "Термінал"),
                    (3, "storno", "Оплата послуг, товарів", 500, "Тестовий Богдан Іванович", "Термінал"),
                    (4, "income", "Внесення коштів в касу", 999, "Внесення", "Готівка"),
                    (5, "income", "Оплата послуг, товарів", 300, "Невідома Особа Хтось", "Готівка"),
                    (6, "transfer", "Переміщення", 777, "", "Готівка")]
            for i, (n, k, art, amt, who, acc) in enumerate(rows):
                db.add(CbFinOp(id=880000 + n, at=datetime(2024, 3 + (i % 2), 10, 12), kind=k, article=art, amount=amt, counterparty=who,
                               account=acc, source="import"))
            s = Sale(total=1000, subtotal=1000, status="paid", at=datetime(2024, 5, 2, 12), patient_id=p1.id)
            s.items.append(SaleItem(kind="service", name="x", qty=1, price=1000, total=1000))
            s.payments.append(SalePayment(method="Готівка", amount=700)); s.payments.append(SalePayment(method="Бонуси", amount=300))
            db.add(s); db.commit()
            r = pm.build(db, 2024)
            by = {x["name"]: x for x in r["rows"]}
            assert by["Тестова Анна Петрівна"]["sum"] == 8700 and by["Тестова Анна Петрівна"]["pid"] == p1.id and by["Тестова Анна Петрівна"]["rank"] == 1
            assert by["Тестовий Богдан Іванович"]["sum"] == 1500 and by["Тестовий Богдан Іванович"]["refund"] == 500
            assert by["Невідома Особа Хтось"]["pid"] is None
            assert r["total"] == 8700 + 1500 + 300 and r["skipped"]["not_patient"] == 0 and r["patients"] == 3
            assert abs(by["Тестова Анна Петрівна"]["pct"] - 100 * 8700 / 10500) < 0.01 and by["Тестова Анна Петрівна"]["abc"] == "B"
            assert by["Невідома Особа Хтось"]["abc"] == "C"
            rm = pm.build(db, 2024, 3)
            assert rm["total"] == 8000 + 300 - 500 and rm["label"] == "березень 2024"
            assert pm.build(db, None)["total"] >= r["total"]
            assert pm.build(db, 2024, q="богдан")["rows"][0]["name"] == "Тестовий Богдан Іванович"
            rc = pm.build(db, 2024, abc_f="c")
            assert [x["name"] for x in rc["rows"]] == ["Тестовий Богдан Іванович", "Невідома Особа Хтось"] and rc["abc_f"] == "C" and rc["shown_sum"] == 1800
            assert len(pm.to_xlsx(r)) > 1000
        t = c.get("/reports?r=patient_money&year=2024").text
        assert "Отримано грошей від пацієнтів" in t and "Тестова Анна Петрівна" in t and "ABC-сегменти" in t
        assert c.get("/reports?r=patient_money&year=2024&month=3&fmt=xlsx").status_code == 200
        tb = c.get("/reports?r=patient_money&year=2024&abc=B").text
        assert "Тестова Анна Петрівна" in tb and "Невідома Особа Хтось" not in tb
        assert 'href="/reports?r=patient_money"' in c.get("/reports").text
