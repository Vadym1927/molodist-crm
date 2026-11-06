"""Картка послуги: планувальник ціни (дві ціни матеріалів, два валових, дві маржі), історія, захист ціни від синку."""
from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbService, Product, ProductPurchase, TechCard, TechCardLine, ServiceLog
from app.services import service_card as sc


def _mk(db):
    drug = Product(name="Тест-філер 1 мл", name_norm="тест-філер 1 мл", warehouse="inject", unit="мл", cost=4000)
    glove = Product(name="Тест-рукавички", name_norm="тест-рукавички", warehouse="single", unit="шт", cost=10)
    db.add_all([drug, glove]); db.flush()
    db.add_all([ProductPurchase(product_id=drug.id, bought_on=date(2026, 1, 10), qty=5, unit_cost=4000),
                ProductPurchase(product_id=drug.id, bought_on=date(2026, 9, 1), qty=5, unit_cost=5000, supplier="Тест-пост"),
                ProductPurchase(product_id=glove.id, bought_on=date(2026, 1, 10), qty=100, unit_cost=10)])
    card = TechCard(name="Тест-губи", cats=[{"name": "Препарат", "wh": "inject"}, {"name": "Одноразка", "wh": "single"}])
    db.add(card); db.flush()
    db.add_all([TechCardLine(card_id=card.id, cat="Препарат", product_id=drug.id, qty=1),
                TechCardLine(card_id=card.id, cat="Одноразка", product_id=glove.id, qty=2)])
    sv = CbService(id=8_800_001, name="Тест губи 1 мл", price=10000, techcard_id=card.id, price_set_on=date(2026, 3, 1))
    db.add(sv); db.commit()
    return sv.id


def test_planner_two_prices():
    with SessionLocal() as db:
        sid = _mk(db)
        pl = sc.planner(db, db.get(CbService, sid))
        assert pl["cost_then"] == 4020 and pl["cost_now"] == 5020 and pl["cost_diff"] == 1000
        assert pl["drug_then"] == 4000 and pl["drug_now"] == 5000 and pl["cons_now"] == 20
        assert pl["gross_then"] == 5980 and pl["gross_now"] == 4980
        assert pl["margin_then"] == 59.8 and pl["margin_now"] == 49.8
        assert pl["rows"][0]["drug"] and pl["rows"][0]["now_sup"] == "Тест-пост"
        assert pl["keep_margin_price"] == round(5020 / (1 - 0.598))


def test_card_pages_save_price_and_sync_lock():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        sid = db.scalar(__import__("sqlalchemy").select(CbService.id).where(CbService.id == 8_800_001)) or _mk(db)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for t in ("about", "price", "staff", "history"):
            r = c.get(f"/services/{sid}?tab={t}"); assert r.status_code == 200, t
        assert "Планувальник ціни" in c.get(f"/services/{sid}?tab=price").text
        r = c.post(f"/services/{sid}/card", data={"tab": "price", "price": "12000", "price_set_on": ""}, follow_redirects=False)
        assert r.status_code == 303
        r = c.post(f"/services/{sid}/card", data={"tab": "about", "name": "Тест губи 1 мл", "duration": "60", "active": "1",
                                                  "repeat_days": "180", "color": sc.COLORS[0]}, follow_redirects=False)
        assert r.status_code == 303
        assert "Тест губи" in c.get(f"/services/{sid}?tab=history").text
    with SessionLocal() as db:
        sv = db.get(CbService, sid)
        assert sv.price == 12000 and sv.price_locked and sv.price_set_on == date.today() and sv.repeat_days == 180
        assert db.scalar(__import__("sqlalchemy").select(ServiceLog.id).where(ServiceLog.service_id == sid, ServiceLog.field == "price"))
        from app.integrations import cleverbox as cbm

        class FakeCB:
            def services(self): return [{"id": sid, "name": "Тест губи 1 мл", "price": "10200", "time": 60}]
        cbm.sync_services(db, FakeCB())
        sv = db.get(CbService, sid)
        assert sv.price == 12000 and sv.cb_price == 10200
