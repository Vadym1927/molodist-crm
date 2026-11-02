"""Звірка залишків з CleverBox на кінець 31.10: рухи після цієї дати (тестові чеки) не впливають."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, StockDoc, StockMove
from app.services import stock as st


def test_reconcile_as_of():
    settings.admin_password = "owner-pass-1"
    d0 = date.today() - timedelta(days=2)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            p = Product(name="Звірка крем", name_norm="звірка крем", warehouse="vitrina", cb_product_id=88001, cost=100)
            db.add(p); db.flush()
            st.start_ledger(db)
            db.add_all([StockMove(product_id=p.id, warehouse="vitrina", qty=10, kind="opening", at=datetime.combine(d0, datetime.min.time())),
                        StockMove(product_id=p.id, warehouse="vitrina", qty=-3, kind="sale", at=datetime.now())])   # тестовий продаж після 31.10
            db.commit(); pid = p.id
        r = c.post("/stock/balances/snapshot-cb", json={"date": d0.isoformat(), "items": [[88001, "vitrina", 12, 1200], [99999, "vitrina", 1, 10]]}).json()
        assert r["rows"] == 1 and r["missing"] == [99999]
        assert "Залишки по складах" in c.get("/stock?tab=balances").text
        c.post("/stock/balances/reconcile", data={"as_of": d0.isoformat()})
        with SessionLocal() as db:
            doc = db.query(StockDoc).filter(StockDoc.kind == "inventory").one()
            assert doc.doc_date == d0 and len(doc.lines) == 1
            st.post(db, doc)
            assert st.balances(db, at=datetime.combine(d0, datetime.max.time()))[(pid, "vitrina")] == 12      # на 31.10 — як у CleverBox
            assert st.balances(db)[(pid, "vitrina")] == 9                                                     # + тестовий продаж після
