from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import (SessionLocal, Appointment, Check, Patient, Product, ProductPurchase, Sale, SaleItem, VisitMaterial)
from app.services import reports as rp


def test_brand_margin_vitrina_and_procedures():
    with SessionLocal() as db:
        cream = Product(name="Крем БрендТест", name_norm="КРЕМ БРЕНДТЕСТ", unit="шт", cost=900, warehouse="vitrina", brand="БрендА")
        drug = Product(name="Препарат БрендТест", name_norm="ПРЕПАРАТ БРЕНДТЕСТ", unit="мл", cost=2000, warehouse="inject", brand="БрендБ")
        syr = Product(name="Шприц БрендТест", name_norm="ШПРИЦ БРЕНДТЕСТ", unit="шт", cost=5, warehouse="single", brand="БрендВ")
        pat = Patient(full_name="Маржа Тест", phone="+380501110099")
        db.add_all([cream, drug, syr, pat])
        db.flush()
        # остання поставка дешевша за картку — береться вона
        db.add(ProductPurchase(product_id=cream.id, bought_on=date(2021, 2, 1), qty=5, unit_cost=1000))
        db.add(ProductPurchase(product_id=cream.id, bought_on=date(2021, 2, 20), qty=5, unit_cost=800))
        db.add(Check(id=990001, date=datetime(2021, 3, 5, 12), amount=4000, patient_id=pat.id,
                     items=[{"item_type": "product", "item_name": "Крем БрендТест", "quantity": 2, "payd_amount": 4000}]))
        ap = Appointment(patient_id=pat.id, start=datetime(2021, 3, 6, 10), end=datetime(2021, 3, 6, 11), status="done",
                         services=[{"id": 77001, "name": "Біоревіталізація Т", "price": 6000}])
        db.add(ap)
        db.flush()
        db.add_all([VisitMaterial(appt_id=ap.id, svc_idx=0, cat="Ін'єкції", product_id=drug.id, qty=1, unit_cost=2000, warehouse="inject"),
                    VisitMaterial(appt_id=ap.id, svc_idx=0, cat="Одноразка", product_id=syr.id, qty=2, unit_cost=5, warehouse="single")])
        sl = Sale(at=datetime(2021, 3, 6, 11), patient_id=pat.id, status="paid", subtotal=6000, total=6000, appointment_id=ap.id)
        sl.items = [SaleItem(kind="service", ref_id=77001, name="Біоревіталізація Т", price=6000, total=6000)]
        db.add(sl)
        db.commit()
        r = rp.build(db, "brands", date(2021, 3, 1), date(2021, 3, 31))
        by = {x["brand"]: x for x in r.rows}
        assert by["БрендА"]["v_rev"] == 4000 and by["БрендА"]["v_cost"] == 1600 and by["БрендА"]["v_pct"] == 60.0
        b = by["БрендБ"]
        assert b["p_cost"] == 2000 and round(b["p_rev"]) == round(6000 * 2000 / 2010)
        assert round(by["БрендВ"]["p_rev"] + b["p_rev"]) == 6000
        assert rp.build(db, "brands", date(2021, 3, 1), date(2021, 3, 31), cat="vitrina").rows[0]["brand"] == "БрендА"
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get("/reports?r=brands&df=2021-03-01&dt_=2021-03-31")
        assert h.status_code == 200 and "Маржа по брендах" in h.text and "БрендА" in h.text
        assert c.get("/reports?r=brands&fmt=xlsx").status_code == 200
