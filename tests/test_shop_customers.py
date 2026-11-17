from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app
from app.models import SessionLocal, Patient, ShopAccount, SiteOrder
from app.services import shop_customers as sc


def test_shop_customers_list_and_page(monkeypatch):
    with SessionLocal() as db:
        p = Patient(full_name="Покупчиня Тест", phone="380631112299")
        db.add(p); db.flush()
        db.add(ShopAccount(patient_id=p.id, created_at=datetime.utcnow(), last_login=datetime.utcnow(), first_name="Оля", ref_code="TSTSC1", wishlist=[1, 2]))
        db.add(SiteOrder(source="crm", ext_id="SC-1", number="SC-1", created_at=datetime.now(), status="Оплачено", paid=True, total=1200, patient_id=p.id))
        db.add(SiteOrder(source="crm", ext_id="SC-2", number="SC-2", created_at=datetime.now(), status="Скасовано", paid=False, total=900, patient_id=p.id))
        db.commit()
        r = sc.rows(db, q="380631112299")
        x = r["rows"][0]
        assert x["paid"] == 1 and x["sum"] == 1200 and x["wish"] == 2 and x["name"] == "Оля"
        assert r["kpi"]["total"] >= 1 and r["kpi"]["buyers"] >= 1
        assert not sc.rows(db, q="380631112299", flt="nobuy")["rows"]
    from app import security
    from app.config import settings
    security.login_limiter.fails.clear(); security.ip_limiter.fails.clear()
    monkeypatch.setattr(settings, "admin_password", "owner-pass-1")
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        res = c.get("/shop-customers?q=380631112299")
        assert res.status_code == 200 and "Оля" in res.text and "зареєстровано всього" in res.text
