from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Check
from app.services import reports as rp


def test_cosmetics_commission():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add(Check(id=881001, date=datetime(2032, 2, 3, 12), amount=0, deleted=False, payments=[],
                     items=[{"item_type": "product", "item_name": "Крем дорогий", "price": 2000, "payd_amount": 2000, "quantity": 1,
                             "initiator_name": "Косметична Лікарка"},
                            {"item_type": "product", "item_name": "Сироватка", "price": 1500, "payd_amount": 1350, "quantity": 1,
                             "initiator_name": "Косметична Лікарка"},
                            {"item_type": "product", "item_name": "Тонік", "price": 900, "payd_amount": 900, "quantity": 2,
                             "initiator_name": "Косметична Лікарка"},
                            {"item_type": "service", "item_name": "Пілінг", "payd_amount": 3000, "initiator_name": "Косметична Лікарка"}]))
        db.commit()
        r = rp.build(db, "cosmetics", date(2032, 2, 1), date(2032, 2, 28))
        g = next(z for z in r.rows if z.get("_group"))
        assert g["name"] == "Косметична Лікарка" and g["sum"] == 200 + 67.5
        items = {z["name"].split(" · ")[0]: z for z in r.rows if not z.get("_group")}
        assert items["Крем дорогий"]["pct"] == 10 and items["Сироватка"]["pct"] == 5 and items["Тонік"]["pct"] == 0
        assert "Пілінг" not in items
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/reports?r=cosmetics&df=2032-02-01&dt_=2032-02-28").text
        assert "Бонус за продаж косметики" in t and "Косметична Лікарка" in t and "ціна &lt; 1000" in t
        assert 'href="/reports?r=cosmetics"' in c.get("/reports").text


def test_cosmetics_pct_settings():
    settings.admin_password = "owner-pass-1"
    from app.models import Product
    with SessionLocal() as db:
        db.add(Product(name="Сироватка", name_norm="сироватка", brand="Medik8"))
        db.add(Check(id=881002, date=datetime(2032, 3, 3, 12), amount=0, deleted=False, payments=[],
                     items=[{"item_type": "product", "item_name": "Крем дорогий", "price": 2000, "payd_amount": 2000, "quantity": 1, "initiator_name": "Лікарка А"},
                            {"item_type": "product", "item_name": "Сироватка", "price": 1500, "payd_amount": 1350, "quantity": 1, "initiator_name": "Лікарка А"},
                            {"item_type": "product", "item_name": "Тонік", "price": 900, "payd_amount": 900, "quantity": 1, "initiator_name": "Лікарка А"},
                            {"item_type": "product", "item_name": "Крем дорогий", "price": 2000, "payd_amount": 2000, "quantity": 1, "initiator_name": "Адмін Б"}]))
        db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Бонус за продаж косметики" in c.get("/settings").text
        assert "Загальне правило" in c.get("/settings/cosmetics-pct").text
        r = c.post("/settings/cosmetics-pct", data={"full": "12", "disc": "6", "disc_from": "0", "min_price": "800", "low": "2", "base": "paid",
                   "staff_name": ["Адмін Б", "Лікарка А"], "staff_full": ["", "15"], "staff_disc": ["", ""], "staff_min_price": ["", ""], "staff_off": ["0"],
                   "brand_name": ["Medik8"], "brand_full": [""], "brand_disc": ["8"], "prod_name": ["Тонік"], "prod_full": ["3"], "prod_disc": [""]},
                   follow_redirects=False)
        assert r.status_code == 303
    with SessionLocal() as db:
        rep = rp.build(db, "cosmetics", date(2032, 3, 1), date(2032, 3, 31))
        it = {(z["name"].split(" · ")[0]): z for z in rep.rows if not z.get("_group")}
        grp = {z["name"]: z for z in rep.rows if z.get("_group")}
        assert grp["Адмін Б"]["sum"] == 0
        assert grp["Лікарка А"]["sum"] == 300 + 108 + 27             # 15% лікаря, 8% бренду зі знижкою, 3% товару
