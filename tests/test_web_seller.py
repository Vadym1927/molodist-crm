from datetime import date, datetime

from app.models import Patient, Product, SessionLocal, SiteOrder, User


def test_web_sale_initiator_is_order_responsible():
    from app.services import daybook as dbk, reports as rp, sales as sl
    with SessionLocal() as db:
        u = User(login="packer_t", name="Пакувальниця Тест", role="admin", password_hash="x", active=True)
        p = Patient(full_name="Покупець Сайту", phone="380671230055")
        pr = Product(name="Крем сайт тест", name_norm="крем сайт тест", price=1500, warehouse="vitrina", active=True)
        db.add_all([u, p, pr]); db.flush()
        o = SiteOrder(source="crm", ext_id="WS1", number="WS1", created_at=datetime.now(), status="Оплачено", paid=True, total=1200,
                      phone=p.phone, raw={"resp": u.id})
        db.add(o); db.flush()
        s = sl.create(db, {"patient_id": p.id, "no_discounts": True, "no_accrual": True,
                           "items": [{"kind": "product", "ref_id": pr.id, "name": pr.name, "qty": 1, "price": 1200}],
                           "payments": [{"method": "monobank", "amount": 1200}], "comment": "Інтернет-магазин, замовлення WS1"})
        db.commit()
        assert dbk.is_web(s) and dbk.web_seller(db, s) == "Пакувальниця Тест"
        r = rp.cosmetics(db, date.today(), date.today())
        g = next(x for x in r.extra["groups"] if x["staff"] == "Пакувальниця Тест")
        row = g["rows"][0]
        assert row["price"] == 1500 and row["disc_pct"] == 20.0 and "сайт" in row["ref"]
        o.raw = {}; db.commit()
        assert dbk.web_seller(db, s) == dbk.WEB_SELLER
