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
        r = c.post("/products/new", data={"name": "OBSERV амортизація", "warehouse": "equip", "purchase_unit": "шт.", "cost": "50", "qty": "100"},
                   follow_redirects=False)
        assert r.status_code == 303 and "wh=equip" in r.headers["location"]
        r2 = c.post("/products/new", data={"name": "OBSERV амортизація", "warehouse": "equip"}, follow_redirects=False)
        assert "/products/" in r2.headers["location"] and "err=" in r2.headers["location"]                # дубль — у картку
        c.post("/products/new", data={"name": "Крем ручний", "warehouse": "vitrina", "price": "1200", "for_sale": "1", "short_name": "Крем",
                                      "multi_unit": "1", "purchase_unit": "фл.", "unit": "мл.", "coef": "50", "tags": "МАГАЗИН, ДЕРМАТОЛОГ",
                                      "direction": "Догляд", "category": "Креми", "uktzed": "3304 99 00 00", "tare_g": "20", "min_stock": "2"})
        with SessionLocal() as db:
            pid = db.query(Product).filter(Product.name == "Крем ручний").one().id
        h = c.get(f"/products/{pid}").text
        assert "Редагувати товар" in h and "ДЕРМАТОЛОГ" in h and "Коротка назва" in h
        r = c.post(f"/products/{pid}/full", data={"name": "Крем ручний 2", "warehouse": "vitrina", "for_sale": "1", "for_expense": "1",
                                                  "purchase_unit": "шт.", "price": "1300"}, follow_redirects=False)
        assert r.status_code == 303
    with SessionLocal() as db:
        p = db.query(Product).filter(Product.name == "OBSERV амортизація").one()
        assert p.warehouse == "equip" and p.cost == 50 and p.for_sale is False and p.unit == "шт"
        assert sum(m.qty for m in db.query(StockMove).filter(StockMove.product_id == p.id, StockMove.warehouse == "equip")) == 100
        k = db.get(Product, pid)
        assert k.name == "Крем ручний 2" and k.for_sale and k.for_expense and k.price_manual == 1300 and k.unit == "шт" and k.coef is None
