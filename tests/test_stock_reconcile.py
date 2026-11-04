from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product


def test_stock_reconcile():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Product(name="Звірка тест крем", name_norm="звірка тест крем", cb_product_id=987654, cost=100)
        db.add(p); db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.post("/stock/reconcile", json={"rows": [[987654, "vitrina", 3, 300, "Звірка тест крем"], [1, "single", 2, 10, "Невідомий товар xyz"]]})
        assert r.json()["n"] == 2
        page = c.get("/stock/reconcile").text
        assert "Звірка тест крем" in page and "Невідомий товар xyz" in page
