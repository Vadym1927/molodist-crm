"""Засоби домашнього догляду: бренди, постачальники, закупівлі (сер./остання ціна), термін, фільтри, картка."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, ProductPurchase, StockLevel, ProductSale, Patient
from app.services import catalog


def test_products_flow():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        a = Product(name="ZO Daily Power Defense 50 мл", name_norm="zo daily", cb_product_id=501, price=4200,
                    brand="ZO Skin Health", sku="ZO-DPD50", min_stock=3)
        b = Product(name="AG Azeloroza сироватка", name_norm="ag azeloroza", cb_product_id=502, price=2200)
        db.add_all([a, b]); db.flush()
        soon = date.today() + timedelta(days=40)
        db.add_all([ProductPurchase(product_id=a.id, bought_on=date.today() - timedelta(days=60), qty=4, unit_cost=2000,
                                    supplier="Дистрибʼютор А", expiry_date=date.today() + timedelta(days=400)),
                    ProductPurchase(product_id=a.id, bought_on=date.today() - timedelta(days=5), qty=2, unit_cost=2300,
                                    supplier="Дистрибʼютор Б", expiry_date=soon),
                    StockLevel(cb_product_id=501, name=a.name, qty=2), StockLevel(cb_product_id=502, name=b.name, qty=0)])
        p = Patient(full_name="Покупниця Тест"); db.add(p); db.flush()
        db.add(ProductSale(external_id="s-501", patient_id=p.id, product_id=501, product_name=a.name, quantity=3,
                           amount=12600, sold_at=datetime.now() - timedelta(days=10)))
        db.commit()
        rows = {r.product.id: r for r in catalog.product_rows(db, [])}
        ra = rows[a.id]
        assert ra.cost_avg == round((4 * 2000 + 2 * 2300) / 6, 2) and ra.cost_last == 2300
        assert ra.expiry == soon and ra.margin == round(100 * (4200 - ra.cost_avg) / ra.cost_avg)
        assert ra.sold_90 == 3 and ra.days_left == 60
        aid, bid = a.id, b.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/products").text
        assert "ZO Skin Health" in page and "Без бренду" in page and "Термін ≤ 90 дн" in page
        assert "ZO Daily" in c.get("/products?f=expiring").text
        assert "AG Azeloroza" in c.get("/products?f=out").text and "ZO Daily" not in c.get("/products?f=out").text
        assert "AG Azeloroza" not in c.get("/products?brand=ZO+Skin+Health").text
        card = c.get(f"/products/{aid}").text
        assert "ZO-DPD50" in card and "Дистрибʼютор Б" in card and "Покупниця Тест" in card
        c.post(f"/products/{aid}/purchases", data={"qty": "1", "unit_cost": "2500", "supplier": "Дистрибʼютор Б"})
        c.post("/products/bulk", data={"ids": [str(bid)], "field": "brand", "value": "AG Skin"})
        c.post(f"/products/{bid}", data={"sku": "AG-AZ", "supplier": "Дистрибʼютор А", "expiry_date": "2027-05-01",
                                          "price_manual": "", "brand": "AG Skin"})
        assert "Дистрибʼютор Б" in c.get("/products?tab=suppliers").text
    with SessionLocal() as db:
        assert db.get(Product, bid).brand == "AG Skin" and db.get(Product, bid).sku == "AG-AZ"
        assert db.query(ProductPurchase).filter_by(product_id=aid).count() == 3
