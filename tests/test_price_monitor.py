from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbService, PriceAlert, ProcedureNorm, Product, ProductPurchase
from app.services import price_monitor as pm


def test_price_monitor(monkeypatch):
    sent = []
    from app.services import alerts
    monkeypatch.setattr(alerts, "send", lambda t: sent.append(t) or 1)
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Product(name="Моніторна сироватка", name_norm="моніторна сироватка", warehouse="inject", cost=10.0)
        g = Product(name="Моніторний гель", name_norm="моніторний гель", warehouse="single", cost=2.0)
        db.add_all([p, g]); db.flush()
        db.add(ProductPurchase(product_id=p.id, bought_on=date(2026, 9, 1), qty=5, unit_cost=1000))
        db.add_all([CbService(id=9401, name="Моніторна процедура", price=3000), ProcedureNorm(service_id=9401, product_id=g.id, qty=10)])
        db.commit()
        pm.scan(db)
        r = next(x for x in pm.products(db) if x["id"] == p.id)
        assert r["base"] == 1000 and r["zone"] == 0
        s = next(x for x in pm.services(db) if x["id"] == 9401)
        assert s["base"] == 20 and s["zone"] == 0                      # 10 × 2 ₴ — стартовий знімок
        db.add(ProductPurchase(product_id=p.id, bought_on=date(2026, 10, 1), qty=5, unit_cost=1250))   # +250 → зона 2
        db.add(ProductPurchase(product_id=g.id, bought_on=date(2026, 10, 1), qty=100, unit_cost=12))    # 10×12=120 → +100 → зона 1
        db.commit()
        assert pm.scan(db) == 2 and pm.scan(db) == 0                    # повторно не сповіщає
        al = {a.kind: a for a in db.query(PriceAlert)}
        assert al["product"].zone == 2 and al["service"].zone == 1 and sent and "Моніторна сироватка" in sent[0]
        db.add(ProductPurchase(product_id=p.id, bought_on=date(2026, 10, 20), qty=5, unit_cost=1450)); db.commit()   # +450 → зона 4
        assert pm.scan(db) == 1
        pm.accept(db, "product", p.id)
        r = next(x for x in pm.products(db) if x["id"] == p.id)
        assert r["base"] == 1450 and r["zone"] == 0
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/prices").text
        assert "Центр моніторингу цін" in t and "Моніторна сироватка" in t
        assert "Моніторна процедура" in c.get("/prices?tab=services").text
        assert "Зона 4" in c.get("/prices?tab=alerts").text and "pbadge" in c.get("/prices?tab=alerts").text
        c.post("/prices/settings", data={**{f"z_{k}_{i}": str(50 * i) for k in pm.WAREHOUSES for i in range(1, 5)},
                                         **{f"z_svc_{i}": str(10 * i) for i in range(1, 5)}, "tg": "1"})
        with SessionLocal() as db:
            assert pm.cfg(db)["zones"]["inject"] == [50, 100, 150, 200] and pm.cfg(db)["svc_zones"] == [10, 20, 30, 40]
        c.post("/prices/alerts/seen")
        with SessionLocal() as db:
            assert pm.unseen(db) == 0


def test_price_monitor_units_and_window():
    from datetime import timedelta
    t = date.today()
    with SessionLocal() as db:
        j = Product(name="Стіки нормалізація", name_norm="стіки нормалізація", warehouse="vitrina", cost=70.0, coef=30)
        e = Product(name="Крем у євро", name_norm="крем у євро", warehouse="vitrina", cost=2775.0)
        db.add_all([j, e]); db.flush()
        # один запис за стік (67.7 × 30 = 2030), інші — ціна упаковки, записана як ціна одиниці
        for days, u in ((900, 67.7), (500, 1672), (200, 1956), (30, 2201)):
            db.add(ProductPurchase(product_id=j.id, bought_on=t - timedelta(days=days), qty=1, unit_cost=u))
        for days, u in ((800, 55.74), (300, 2600), (20, 2775)):      # перший запис — у валюті
            db.add(ProductPurchase(product_id=e.id, bought_on=t - timedelta(days=days), qty=1, unit_cost=u))
        db.commit()
        rows = {r["id"]: r for r in pm.products(db)}
        rj, re_ = rows[j.id], rows[e.id]
        assert rj["cur"] == 2201 and rj["base"] == 1956 and rj["zone"] == 2          # без ×30 і з вікном у рік
        assert all(h["cost"] < 3000 for h in rj["hist"])
        assert re_["skipped"] == 1 and re_["base"] == 2600 and re_["cur"] == 2775


def test_price_monitor_brand_and_item_rules(monkeypatch):
    from app.services import alerts
    monkeypatch.setattr(alerts, "send", lambda t: 1)
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        a = Product(name="Правило А", name_norm="правило а", warehouse="inject", cost=1.0, brand="ПравБренд")
        b = Product(name="Правило Б", name_norm="правило б", warehouse="inject", cost=1.0, brand="ПравБренд")
        db.add_all([a, b]); db.flush()
        for p in (a, b):
            db.add(ProductPurchase(product_id=p.id, bought_on=date.today().replace(day=1), qty=1, unit_cost=5000))
            db.add(ProductPurchase(product_id=p.id, bought_on=date.today(), qty=1, unit_cost=5300))      # +300 ₴ = +6%
        db.commit()
        ids = (a.id, b.id)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post("/prices/rule", data={"kind": "brand", "key": "ПравБренд", "unit": "pct", "z1": "3", "z2": "5", "z3": "10", "z4": "15"})
        c.post("/prices/rule", data={"kind": "item", "key": str(ids[1]), "unit": "uah", "z1": "1000", "z2": "2000", "z3": "3000", "z4": "4000", "off": "1"})
        with SessionLocal() as db:
            rows = {r["id"]: r for r in pm.products(db) if r["id"] in ids}
            assert rows[ids[0]]["rule"]["src"] == "brand" and rows[ids[0]]["zone"] == 2          # 6% ≥ 5%
            assert rows[ids[1]]["rule"]["off"] and rows[ids[1]]["zone"] == 0
            assert pm.scan(db) == 0                                                         # нові межі — без сповіщень
        t = c.get("/prices?tab=brands").text
        assert "ПравБренд" in t and "правило бренду" in t
        t = c.get("/prices?brand=ПравБренд").text
        assert "Правило А" in t and "Не моніториться" in t
        c.post("/prices/rule", data={"kind": "brand", "key": "ПравБренд", "action": "reset"})
        with SessionLocal() as db:
            assert "правбренд" not in pm.cfg(db)["brand_rules"]


def test_price_monitor_uses_planned_supplier_price():
    from datetime import datetime, timedelta
    from app.models import PriceSnapshot
    from app.services import pricing
    with SessionLocal() as db:
        pricing.save_rates(db, 40.0, 45.0, 10)
        p = Product(name="Планова сироватка", name_norm="планова сироватка", warehouse="vitrina", cost=1.0, brand="ПланБренд")
        db.add(p); db.flush()
        db.add(ProductPurchase(product_id=p.id, bought_on=date.today() - timedelta(days=30), qty=1, unit_cost=2000))
        p.cost_currency, p.cost_fx = "EUR", 50.0                     # прайс: 50 € × 45 = 2250 ₴
        db.add(PriceSnapshot(kind="price", created_at=datetime.now(), items=[{"pid": p.id, "before": {"cost_fx": None, "cost_currency": None},
                                                                             "after": {"cost_fx": 50.0, "cost_currency": "EUR"}}]))
        db.commit()
        r = next(x for x in pm.products(db) if x["id"] == p.id)
        assert r["src"] == "plan" and r["cur"] == 2250 and r["diff"] == 250
        db.add(ProductPurchase(product_id=p.id, bought_on=date.today() + timedelta(days=1), qty=1, unit_cost=2100)); db.commit()
        r = next(x for x in pm.products(db) if x["id"] == p.id)
        assert r["src"] == "supply" and r["cur"] == 2100             # прихід новіший за прайс
