from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, Sale, SaleItem
from app.services import salary_plans as spl, finops as fo


def test_salary_plan_settings_and_payroll():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/settings/salary").text
        assert "Дарина 2026 NEW" in t and "ставка 1200 ₴ за день" in t
        r = c.post("/settings/salary", data={"name": "Тест Лікар", "comm_on": "1", "comm_services": "20", "comm_services_mode": "paid",
                                             "comm_products": "10", "comm_products_mode": "cost", "fixed_on": "1", "fixed_amount": "5000",
                                             "fixed_period": "month", "it_kind": "service", "it_name": "Консультації лікарів",
                                             "it_val": "40", "it_mode": "paid"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            p = spl.by_name(db, "Тест Лікар")
            assert p["comm"]["items"][0]["val"] == 40 and p["fixed"]["period"] == "month"
            d = Doctor(name="Зарплатна Лікарка", extra={"salary": "Тест Лікар"}); db.add(d); db.commit()
            s = Sale(status="paid", at=datetime(2026, 9, 10, 12), subtotal=3000, total=3000)
            s.items = [SaleItem(kind="service", name="Консультації лікарів", qty=1, price=1000, total=1000, doctor_id=d.id),
                       SaleItem(kind="service", name="Пілінг", qty=1, price=1500, total=1500, doctor_id=d.id),
                       SaleItem(kind="product", name="Крем", qty=1, price=500, total=500, doctor_id=d.id)]
            db.add(s); db.commit(); did = d.id
            row = next(r for r in fo.payroll(db, "2026-09") if r["doctor"].id == did)
            # 5000 + 40% від 1000 + 20% від 1500 + 10% від 500
            assert row["m"]["accrued"] == 5000 + 400 + 300 + 50
        assert "Оклад: Тест Лікар" in c.get("/finance/payroll?m=2026-09").text
        nid = None
        r = c.post(f"/settings/salary/{p['id']}/copy", follow_redirects=False)
        assert "open=" in r.headers["location"]


def test_accrual_articles_and_payroll_adj():
    from app.models import SalaryAdj
    from app.services import accrual_articles as aa
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/settings/accrual-articles").text
        assert "Лікарняні" in t and "Виплата прибутку в ЗП" in t
        with SessionLocal() as db:
            fine = next(x for x in aa.items(db) if x["name"] == "Штраф")
            d = Doctor(name="Штрафна Лікарка", extra={"salary": "CSD"}); db.add(d); db.commit(); did = d.id
        assert "Штраф (−)" in c.get("/finance/payroll?m=2026-10").text
        c.post("/finance/payroll/adj", data={"doctor_id": did, "month": "2026-10", "amount": "300", "article": str(fine["id"]), "comment": "запізнення"})
        with SessionLocal() as db:
            a = db.query(SalaryAdj).filter_by(doctor_id=did).one()
            assert a.amount == -300 and a.article == "Штраф"
