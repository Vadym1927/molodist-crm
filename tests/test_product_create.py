from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, StockMove
from app.services import stock as st


def test_product_create_on_warehouse():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            st.start_ledger(db)
        h = c.get("/products?tab=care&wh=equip").text
        assert "Додати товар на склад «Обладнання»" in h
        r = c.post("/products/new", data={"name": "OBSERV амортизація", "warehouse": "equip", "unit": "шт.", "cost": "50", "qty": "100"},
                   follow_redirects=False)
        assert r.status_code == 303 and "wh=equip" in r.headers["location"]
        r2 = c.post("/products/new", data={"name": "OBSERV амортизація", "warehouse": "equip"}, follow_redirects=False)
        assert "/products/" in r2.headers["location"] and "err=" in r2.headers["location"]                # дубль — у картку
        c.post("/products/new", data={"name": "Крем ручний", "warehouse": "vitrina", "price": "1200"})
    with SessionLocal() as db:
        p = db.query(Product).filter(Product.name == "OBSERV амортизація").one()
        assert p.warehouse == "equip" and p.cost == 50 and p.for_sale is False
        assert sum(m.qty for m in db.query(StockMove).filter(StockMove.product_id == p.id, StockMove.warehouse == "equip")) == 100
        assert db.query(Product).filter(Product.name == "Крем ручний").one().for_sale is True
