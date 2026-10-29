"""Фінанси: фіноперації, продажі за день / період, нарахування й взаєморозрахунки з/п, баланс і бонуси."""
from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Check, Doctor, Patient, FinOp
from app.services import finops as fo


def test_finance_sections():
    settings.admin_password = "owner-pass-1"
    today = date.today()
    mk = fo.month_key(today)
    with SessionLocal() as db:
        d = Doctor(name="Зарплатна Лікарка", active=True)
        p = Patient(full_name="Бонусна Пацієнтка", phone="+380500000111", cb_balance=1500, cb_bonus=250)
        db.add_all([d, p]); db.commit()
        db.add(Check(id=990001, date=datetime.now(), amount=3000, discount=100, patient_id=p.id, author="Адмін Тест",
                     items=[{"item_type": "service", "item_name": "Чистка обличчя", "payd_amount": 2000, "initiator_name": "Лікарка Зарплатна"},
                            {"item_type": "product", "item_name": "Крем SPF", "payd_amount": 1000, "initiator_name": "Лікарка Зарплатна"}],
                     payments=[{"payment_name": "Картка", "amount": 2800}, {"payment_name": "Бонуси", "amount": 200}]))
        db.commit(); did, pid = d.id, p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for u in ("/finance", "/finance/ops", "/finance/day", "/finance/sales", "/finance/payroll", "/finance/settle",
                  "/finance/balances", "/finance/bonuses", "/finance/pricing"):
            r = c.get(u)
            assert r.status_code == 200 and "Взаєморозрахунки по з/п" in r.text, u
        assert "Чистка обличчя" in c.get("/finance/day").text and "Бонусна Пацієнтка" in c.get("/finance/sales?q=бонусна").text
        ops = c.get("/finance/ops").text
        assert "2 800,00 ₴" in ops and "не гроші" in ops
        c.post("/finance/payroll/rule", data={"doctor_id": did, "pct_services": "40", "pct_products": "10", "base": "5000", "m": mk})
        c.post("/finance/payroll/adj", data={"doctor_id": did, "month": mk, "amount": "300", "sign": "+", "comment": "план"})
        assert "6 200,00 ₴" in c.get(f"/finance/payroll?m={mk}").text            # 5000 + 40%·2000 + 10%·1000 + 300
        c.post("/finance/ops", data={"kind": "expense", "category": "Зарплата", "doctor_id": str(did), "salary_month": mk,
                                     "amount": "2000", "cashbox": "Готівка", "day": str(today), "back": f"/finance/settle?m={mk}"})
        st = c.get(f"/finance/settle?m={mk}").text
        assert "4 200,00 ₴" in st                                                 # 6200 − 2000
        assert "1 500,00 ₴" in c.get("/finance/balances").text and "200,00 ₴" in c.get("/finance/bonuses").text
    with SessionLocal() as db:
        rows = fo.settlements(db, mk)
        r = next(x for x in rows if x["doctor"].id == did)
        assert r["accrued"] == 6200 and r["paid"] == 2000 and r["closing"] == 4200
        assert db.query(FinOp).filter_by(doctor_id=did, category="Зарплата").count() == 1
