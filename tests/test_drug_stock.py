from datetime import date, datetime, timedelta

from app.models import SessionLocal, Appointment, CbService, Patient, Product, ProductPurchase, StockMove, TechCard, TechCardLine
from app.services import drug_stock as ds


def test_drug_need_from_appointments():
    with SessionLocal() as db:
        p = Product(name="Ксеомін тест 100 МО", name_norm="xeo-t", supplier="MERZ ТЕСТ", min_stock=50, warehouse="inject", unit="МО", active=True, cost=60)
        db.add(p); db.flush()
        card = TechCard(name="Ботулінотерапія тест")
        card.lines.append(TechCardLine(cat="Препарат", product_id=p.id, qty=50))
        db.add(card); db.flush()
        sv = CbService(name="Ботулінотерапія лоб тест", techcard_id=card.id, price=5600)
        pt = Patient(full_name="Коваль Тест", phone="+380000000555")
        db.add_all([sv, pt]); db.flush()
        for d in (3, 10):
            st = datetime.combine(date.today() + timedelta(days=d), datetime.min.time()).replace(hour=11)
            db.add(Appointment(patient_id=pt.id, start=st, end=st + timedelta(hours=1), services=[{"id": sv.id, "name": sv.name}]))
        db.add(StockMove(product_id=p.id, warehouse="inject", qty=40, kind="opening", at=datetime(2026, 11, 1)))
        db.add(ProductPurchase(product_id=p.id, bought_on=date(2026, 10, 1), supplier="MERZ ТЕСТ", qty=100, unit_cost=62))
        db.commit()
        v = ds.build(db)
        g = next(x for x in v["groups"] if x["supplier"] == "MERZ ТЕСТ")
        r = g["rows"][0]
        assert r["per"] == 50 and r["n1"] == 2 and r["q1"] == 100 and r["stock"] == 40
        assert r["need"] == 110 and r["price"] == 62 and r["sum"] == 6820 and r["pts"][0][1] == "Коваль"


def test_patient_favorite_services():
    from app.models import Visit
    from app.services import appointments as aps
    with SessionLocal() as db:
        sv = CbService(name="Біоревіталізація улюблена тест", price=4200)
        sv2 = CbService(name="Пілінг рідко тест", price=2700)
        pt = Patient(full_name="Улюблена Тест", phone="+380000000444")
        db.add_all([sv, sv2, pt]); db.flush()
        for i in range(3):
            db.add(Visit(source="cb_visit", external_id=f"fav{i}", patient_id=pt.id, service_id=sv.id, service_name=sv.name,
                         starts_at=datetime(2026, 1 + i, 5, 10), status="done"))
        db.add(Visit(source="cb_visit", external_id="fav9", patient_id=pt.id, service_id=sv2.id, service_name=sv2.name,
                     starts_at=datetime(2026, 5, 5, 10), status="done"))
        db.commit()
        r = aps.services_for(db, None, pt.id)
        assert r["fav"] == [[sv.id, 3]]
