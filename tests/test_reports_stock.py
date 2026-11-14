from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import (SessionLocal, Appointment, Doctor, Patient, Product, ProductPurchase, StockDoc, StockDocLine, StockMove,
                        TechCard, TechCardLine, VisitMaterial, CbService)
from app.services import reports as rp


def _p(db, name, wh="single", cost=10.0, brand=None):
    p = Product(name=name, name_norm=name.upper(), unit="шт", cost=cost, warehouse=wh, brand=brand)
    db.add(p)
    db.flush()
    return p


def test_stock_reports():
    t = date.today()
    now = datetime.now()
    with SessionLocal() as db:
        a, b, c = _p(db, "Шприц Т", cost=10, brand="Б1"), _p(db, "Маска Т", cost=20, brand="Б2"), _p(db, "Лежень Т", "vitrina", 100, "Б2")
        for p, q in ((a, 100), (b, 50), (c, 5)):
            db.add(StockMove(product_id=p.id, warehouse=p.warehouse, qty=q, kind="opening", at=now - timedelta(days=200)))
        db.add(StockMove(product_id=a.id, warehouse="single", qty=-30, kind="procedure", at=now - timedelta(days=1)))
        db.add(StockMove(product_id=b.id, warehouse="single", qty=-49, kind="sale", at=now - timedelta(days=2)))
        doc = Doctor(name="Лікар С")
        pat = Patient(full_name="Пацієнт С")
        db.add_all([doc, pat])
        db.flush()
        ap = Appointment(patient_id=pat.id, doctor_id=doc.id, start=now - timedelta(hours=2), end=now - timedelta(hours=1), status="done",
                         services=[{"id": None, "name": "Чистка Т", "price": 1000}])
        db.add(ap)
        db.flush()
        db.add(VisitMaterial(appt_id=ap.id, svc_idx=0, cat="Одноразка", product_id=a.id, qty=3, unit_cost=10, warehouse="single"))
        inv = StockDoc(kind="inventory", number="ІН-1", doc_date=t, status="posted", wh_to="single")
        db.add(inv)
        db.flush()
        db.add(StockDocLine(doc_id=inv.id, product_id=a.id, qty=68, sys_qty=70, unit_cost=10))
        db.commit()
        d0 = t - timedelta(days=29)
        m = rp.build(db, "mat_use", d0, t)
        assert m.tiles[0][1] == 1 and m.tiles[2][1] == 30 and m.rows[0]["fact"] == 3
        assert rp.build(db, "mat_use", d0, t, cat="doctors").rows[0]["name"] == "Лікар С"
        i = rp.build(db, "inventory", d0, t)
        assert i.tiles[1][1] == -20 and i.rows[0]["diff"] == -2
        dd = rp.build(db, "dead", d0, t)
        assert [x["name"] for x in dd.rows] == ["Лежень Т"] and dd.tiles[0][1] == 500
        v = rp.build(db, "stock_value", d0, t)
        assert v.tiles[0][1] == 70 * 10 + 1 * 20 + 500
        assert rp.build(db, "stock_value", d0, t, cat="brand").rows[0]["name"] == "Б1"
        tv = rp.build(db, "turnover", d0, t)
        assert tv.rows[0]["name"] == "Маска Т" and tv.rows[0]["state"] == "впритул" and tv.tiles[1][1] == 1
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for k in ("mat_use&cat=services", "inventory", "dead&cat=180", "stock_value&cat=products", "turnover&cat=low", "dead&fmt=xlsx"):
            assert cl.get("/reports?r=" + k).status_code == 200, k
