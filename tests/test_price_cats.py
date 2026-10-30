from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, CbService
from app.services import price_cats as pc


def test_price_categories_flow():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            d = Doctor(name="Колган Дарʼя Тест"); s = CbService(id=987654, name="Консультація головного лікаря тест", category="Консультації", price=1500)
            db.add_all([d, s]); db.commit(); did = d.id
        assert "Головний лікар" in c.get("/settings/price-cats").text
        c.post("/settings/price-cats", data={"tab": "cats", "on": "1", "base_name": "Лікар", "cat_id": ["1", ""], "cat_name": ["Головний лікар", "Стажер"], "cat_on": ["1", "1"]})
        with SessionLocal() as db:
            assert [x["name"] for x in pc.get(db)["cats"]] == ["Головний лікар", "Стажер"]
        c.post("/settings/price-cats", data={"tab": "staff", f"doc_{did}": "1"})
        c.post("/settings/price-cats", data={"tab": "prices", "p_1_987654": "2500"})
        assert "987654" in c.get("/settings/price-cats?tab=prices").text
        with SessionLocal() as db:
            assert pc.doctor_prices(db)[did] == {"987654": 2500}
        assert '"987654": 2500' in c.get("/sales/new").text
