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
