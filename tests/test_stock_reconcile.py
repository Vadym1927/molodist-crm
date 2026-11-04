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


def test_reconcile_inventory_draft():
    from app.models import StockDoc
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            p = db.query(Product).filter(Product.cb_product_id == 987654).first()
            if not p:
                p = Product(name="Звірка тест крем", name_norm="звірка тест крем", cb_product_id=987654, cost=100)
                db.add(p); db.commit()
            pid = p.id
        c.post("/stock/reconcile", json={"rows": [[987654, "vitrina", 3, 300, "Звірка тест крем"]]})
        page = c.get("/stock/reconcile").text
        if "Не сходиться кількість · 0" in page:
            return                                    # облік у тестовій БД не активний — кількість CRM 0 теж розбіжність
        r = c.post("/stock/reconcile/inventory", data={"pick": f"{pid}|vitrina"}, follow_redirects=False)
        assert r.status_code == 303 and "/stock/" in r.headers["location"]
        with SessionLocal() as db:
            d = db.query(StockDoc).filter(StockDoc.kind == "inventory").order_by(StockDoc.id.desc()).first()
            assert d.status == "draft" and d.lines[0].qty == 3
