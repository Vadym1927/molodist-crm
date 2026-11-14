from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, Doctor
from app.services import sales as sl, reports as rp


def _seed(db):
    doc = Doctor(name="Лікар К")
    a, b = Patient(full_name="Купила Догляд"), Patient(full_name="Не Купила")
    db.add_all([doc, a, b])
    db.commit()
    svc = {"kind": "service", "name": "Чистка", "qty": 1, "price": 1000, "doctor_id": doc.id}
    sl.create(db, {"patient_id": a.id, "items": [svc, {"kind": "other", "name": "x", "qty": 1, "price": 0}], "payments": [{"method": "Готівка", "amount": 1000}]})
    sl.create(db, {"patient_id": a.id, "items": [{"kind": "product", "name": "Крем", "qty": 1, "price": 500, "doctor_id": doc.id}],
                   "payments": [{"method": "Готівка", "amount": 500}]})
    sl.create(db, {"patient_id": b.id, "items": [svc], "payments": [{"method": "Готівка", "amount": 1000}]})
    return doc


def test_cross_and_discounts():
    t = date.today()
    with SessionLocal() as db:
        _seed(db)
        r = rp.build(db, "cross", t - timedelta(days=1), t)
        assert [x[1] for x in r.tiles[:3]] == [2, 1, 1] and r.tiles[3][1] == 500
        assert len(rp.build(db, "cross", t, t, cat="no").rows) == 1
        assert rp.build(db, "cross", t, t, cat="doctors").rows[0]["pct"] == 50
        d = rp.build(db, "discounts", t, t)
        assert d.tiles[1][1] == 0 and d.rows == []
        assert rp.build(db, "certificates", t, t, cat="use").rows == []
        from app.services import autonomy
        autonomy.save(db, autonomous=True, since=(t - timedelta(days=2)).isoformat())
        l = rp.build(db, "ltv", t, t)
        assert l.tiles[0][1] == 2 and l.rows[0]["patient"] == "Купила Догляд" and l.rows[0]["m12"] == 1500
        assert rp.build(db, "src_sales", t, t).rows[0]["src"] == "Не вказано"
        pr = rp.build(db, "patients", t, t)
        assert pr.tiles[1][1] == 2                           # перша оплата в касі CRM — «нові»
        assert rp.build(db, "lost", t, t).rows == [] and len(rp.build(db, "lost", t, t, cat="60").filters) == 1
        ab = rp.build(db, "abc", t, t)
        assert ab.rows[0]["cls"] == "A" and ab.rows[0]["sum"] == 2000
        assert rp.build(db, "abc", t, t, cat="product").rows[0]["name"] == "Крем"
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for u in ("/reports?r=cross", "/reports?r=discounts&cat=rules", "/reports?r=certificates&cat=use", "/reports?r=cross&fmt=xlsx",
                  "/reports?r=ltv", "/reports?r=src_sales", "/reports?r=lost&cat=180", "/reports", "/reports?r=abc&cat=product"):
            assert c.get(u).status_code == 200, u
