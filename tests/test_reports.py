"""Розділ «Звіти»: усі вкладки відкриваються, цифри з чеків, порівняння з попереднім періодом, Excel."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Check, Patient, Product, ProductPurchase
from app.services import reports as rp


def test_reports_all_tabs():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        a = Patient(full_name="Звітна Пацієнтка"); b = Patient(full_name="Звітна Друга")
        pr = Product(name="Звітний крем 50 мл", name_norm="звітний крем 50 мл", brand="RepBrand")
        db.add_all([a, b, pr]); db.flush()
        db.add(ProductPurchase(product_id=pr.id, bought_on=date(2026, 1, 1), qty=10, unit_cost=400))
        d1 = datetime(2031, 3, 5, 12)
        db.add_all([
            Check(id=910001, date=d1, amount=5000, patient_id=a.id, items=[
                {"item_type": "service", "item_name": "Звітна чистка", "payd_amount": 3000, "quantity": 1, "initiator_name": "Лікар Звітний"},
                {"item_type": "product", "item_name": "Звітний крем 50 мл", "payd_amount": 2000, "quantity": 2, "initiator_name": "Лікар Звітний"}]),
            Check(id=910002, date=d1 + timedelta(days=3), amount=3000, patient_id=b.id, items=[
                {"item_type": "service", "item_name": "Звітна чистка", "payd_amount": 3000, "quantity": 1, "initiator_name": "Лікар Звітний"}]),
            Check(id=910003, date=datetime(2031, 2, 10), amount=1000, patient_id=a.id, items=[
                {"item_type": "service", "item_name": "Звітна чистка", "payd_amount": 1000, "quantity": 1}])])
        db.commit()
        m_from, m_to = date(2031, 3, 1), date(2031, 3, 31)
        assert rp.prev_period(m_from, m_to) == (date(2031, 2, 1), date(2031, 2, 28))
        rev = rp.revenue(db, m_from, m_to)
        assert rev.tiles[0][1] == 8000 and rev.tiles[0][2] == 1000 and rev.total["revenue"] == 8000
        prod = rp.products(db, m_from, m_to)
        row = prod.rows[0]
        assert row["sum"] == 2000 and row["cost"] == 800 and row["profit"] == 1200 and row["margin"] == 60.0
        pat = rp.patients(db, m_from, m_to)
        types = {r["name"]: r["type"] for r in pat.rows}
        assert types == {"Звітна Пацієнтка": "повторна", "Звітна Друга": "нова"}
        doc = rp.doctors(db, m_from, m_to)
        assert doc.rows[0]["name"] == "Лікар Звітний" and doc.rows[0]["total"] == 8000
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for k in rp.REPORTS:
            page = c.get(f"/reports?r={k}&df=2031-03-01&dt_=2031-03-31")
            assert page.status_code == 200, k
        assert "Звітна чистка" in c.get("/reports?r=services&df=2031-03-01&dt_=2031-03-31").text
        assert c.get("/reports?period=prev_month").status_code == 200
        x = c.get("/reports?r=products&df=2031-03-01&dt_=2031-03-31&fmt=xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"
        assert 'href="/reports"' in c.get("/").text
