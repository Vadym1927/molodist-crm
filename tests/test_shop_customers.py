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


def test_shop_ads_roi():
    """Окупність Google Ads магазину: витрати кампанії магазину проти замовлень із gclid, маржа за закупівлею."""
    from datetime import date
    from app.models import AdsStat, Product
    from app.services import shop_kpi as sk
    with SessionLocal() as db:
        pr = Product(name="Крем ROI", name_norm="крем roi", cost=600, price=1500); db.add(pr); db.flush()
        t = date.today()
        db.add(AdsStat(source="google", level="campaign", day=t, key="c1", name="MOLODIST Store — PMax товари", cost=500, clicks=100, impressions=4000, conversions=2, conv_value=3000))
        db.add(AdsStat(source="google", level="campaign", day=t, key="c2", name="Ботулінотерапія Київ", cost=900, clicks=50))
        for n in range(2):
            db.add(SiteOrder(source="crm", ext_id=f"ROI-{n}", number=f"ROI-{n}", created_at=datetime.now(), status="Оплачено", paid=True, total=1500,
                             items=[{"product_id": pr.id, "name": "Крем ROI", "qty": 1, "total": 1500}], raw={"utm": {"gclid": "x"}}))
        db.add(SiteOrder(source="crm", ext_id="ROI-d", number="ROI-d", created_at=datetime.now(), status="Оплачено", paid=True, total=800, raw={}))
        db.commit()
        ok = [o for o in db.query(SiteOrder).filter(SiteOrder.ext_id.like("ROI-%"))]
        a = sk.ads(db, t, t, ok)
        assert a["cost_net"] == 500 and a["cost"] == 600 and a["orders"] == 2 and a["revenue"] == 3000
        assert a["gross"] == 1800 and a["cpa"] == 300 and a["roas"] == 5 and a["be_cpa"] == 900 and a["profit"] == 1200 and a["verdict"] == "ok"
