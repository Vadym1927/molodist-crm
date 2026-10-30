from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import (SessionLocal, Appointment, BonusTxn, CbService, Doctor, Patient, Product, Sale, StockMove, SyncState, Visit,
                        GiftCertificate, User)
from app.services import loyalty as ly, stock as st, finops as fo


def test_sales_pos():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        d = Doctor(name="Касова Лікарка", active=True)
        p = Patient(full_name="Касова Пацієнтка", phone="+380671230099")
        s1 = CbService(id=990201, name="Пілінг касовий", price=2000, time_min=60, category="Пілінги")
        pr = Product(name="Крем касовий", name_norm="крем касовий", warehouse="vitrina", price=800)
        cert = GiftCertificate(id=990301, code="GC-1", balance=500, nominal=500, price=500, paid=500)
        db.add_all([d, p, s1, pr, cert]); db.commit()
        db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()
        st.start_ledger(db)
        doc = st.new_doc(db, "supply", wh_to="vitrina", supplier="Кас")
        doc.lines.append(st.StockDocLine(product_id=pr.id, qty=5, unit_cost=300, expiry_date=date.today() + timedelta(days=200)))
        db.commit(); st.post(db, doc)
        ly.save_settings(db, {**ly.settings(db), "enabled": True, "pct_services": 5, "pct_products": 3, "max_pay_pct": 50})
        ly.add(db, p.id, 100, "manual")
        a = Appointment(patient_id=p.id, doctor_id=d.id, start=datetime.now(), end=datetime.now() + timedelta(hours=1),
                        services=[{"id": 990201, "name": "Пілінг касовий", "price": 2000, "minutes": 60}])
        db.add(a); db.commit()
        did, pid, prid, aid = d.id, p.id, pr.id, a.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        nw = c.get(f"/sales/new?appt={aid}").text
        assert "За записом №" in nw and "Касова Пацієнтка" in nw and "990201" in nw
        items = [{"kind": "service", "ref_id": 990201, "name": "Пілінг касовий", "qty": 1, "price": 2000, "doctor_id": did},
                 {"kind": "product", "ref_id": prid, "name": "Крем касовий", "qty": 2, "price": 800, "doctor_id": did, "warehouse": "vitrina"}]
        q = c.post("/sales/quote", json={"patient_id": pid, "items": items, "manual_discount": 100, "bonus_pay": 100}).json()
        assert q["subtotal"] == 3600 and q["discount"] == 100 and q["to_pay"] == 3400 and q["bonus_pay"] == 100
        bad = c.post("/sales", json={"patient_id": pid, "appointment_id": aid, "items": items, "manual_discount": 100, "bonus_pay": 100,
                                     "payments": [{"method": "Готівка", "amount": 100}]})
        assert bad.status_code == 400
        nodoc = [{**items[0], "doctor_id": None}]
        assert "спеціаліста" in c.post("/sales", json={"items": nodoc, "payments": [{"method": "Готівка", "amount": 2000}]}).json()["error"]
        j = c.post("/sales", json={"patient_id": pid, "appointment_id": aid, "items": items, "manual_discount": 100, "bonus_pay": 100,
                                   "payments": [{"method": "Готівка", "amount": 3000}, {"method": "Сертифікат", "amount": 500, "cert_id": 990301}]}).json()
        assert j["ok"]; sid = j["id"]
        assert "Ч-" in c.get(f"/sales/{sid}").text and "Касова Пацієнтка" in c.get("/sales").text
        ops = c.get("/finance/ops").text
        assert f"Ч-{sid}" in ops
    with SessionLocal() as db:
        s = db.get(Sale, sid)
        assert s.total == 3600 and s.discount == 0 and s.bonus_spent == 100      # ручна знижка з каси ігнорується and s.bonus_accrued > 0
        assert db.get(Appointment, aid).status == "done" and db.get(Appointment, aid).sale_id == sid
        assert db.get(GiftCertificate, 990301).balance == 0
        assert round(sum(m.qty for m in db.query(StockMove).filter(StockMove.ref.like(f"crmsale:{sid}:%"))), 3) == -2
        assert db.query(Visit).filter(Visit.external_id.like(f"crm:{sid}:%")).count() == 1
        bal = ly.balance(db, pid)
        assert round(bal, 2) == round(100 - 100 + s.bonus_accrued, 2)
        rows = fo.payroll(db, fo.month_key(date.today()))
        r = next(x for x in rows if x["doctor"].id == did)
        assert r["m"]["services"] > 0 and r["m"]["products"] > 0
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            u = db.query(User).filter(User.login == settings.owner_login).one()
            from app.security import hash_password
            u.delete_pw_hash = hash_password("del-9999"); db.commit()
        c.post(f"/sales/{sid}/refund", data={"reason": "помилка", "del_password": "del-9999"})
        with SessionLocal() as db:
            assert db.get(Sale, sid).status == "refunded" and db.get(GiftCertificate, 990301).balance == 500
            assert not db.query(StockMove).filter(StockMove.ref.like(f"crmsale:{sid}:%")).count()
            assert round(ly.balance(db, pid), 2) == 100
            db.query(User).filter(User.login == settings.owner_login).one().delete_pw_hash = None
            db.query(SyncState).filter(SyncState.key.in_(("stock_ledger_start", "bonus_program"))).delete(); db.commit()


def test_pos_catalog():
    from app.models import Product
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add_all([CbService(id=990901, name="Каталог послуга", category="Пілінги", direction="Класика", price=500, time_min=30),
                    Product(name="Каталог крем", name_norm="каталог крем", brand="Hikari", warehouse="vitrina", price=900)])
        d = Doctor(name="Каталог Лікар", active=True); p = Patient(full_name="Каталог Пацієнт", phone="+380671230777")
        db.add_all([d, p]); db.flush()
        db.add(Appointment(patient_id=p.id, doctor_id=d.id, start=datetime.now().replace(hour=10, minute=0), end=datetime.now().replace(hour=11, minute=0),
                           services=[{"id": 990901, "name": "Каталог послуга", "price": 500}])); db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert any(r["patient"] == "Каталог Пацієнт" and r["services"][0]["price"] == 500 for r in c.get("/pos/visits").json()["rows"])
        g = c.get("/pos/services?group=direction").json()["groups"]
        assert any(x["key"] == "Класика" for x in g)
        assert c.get("/pos/services?group=category&key=Пілінги").json()["items"][0]["name"] == "Каталог послуга"
        assert any(x["key"] == "Вітрина" for x in c.get("/pos/products").json()["groups"])
        assert c.get("/pos/products?group=brand&key=Hikari").json()["items"][0]["price"] == 900
        assert "Візити" in c.get("/sales/new").text


def test_pos_patient_pick_and_create():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        j = c.post("/pos/patient", json={"name": "Нова Касова", "phone": "+380 67 999 11 22"}).json()
        assert j["id"]
        again = c.post("/pos/patient", json={"name": "Інше імʼя", "phone": "0679991122"}).json()
        assert again["id"] == j["id"]                                  # той самий телефон — той самий пацієнт
        info = c.get(f"/pos/patient/{j['id']}").json()
        assert info["name"] == "Нова Касова" and info["tag"] == "Первинний пацієнт" and info["bonus"] == 0
        assert c.post("/pos/patient", json={"name": ""}).status_code == 400
        t = c.get("/sales/new").text
        assert "Продаж на пацієнта" in t and "Без пацієнта" in t and "Оберіть пацієнта" in t


def test_lookup_phone_from_first_digit_and_sku():
    from app.models import Product
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add(Patient(full_name="Телефонна Пацієнтка", phone="380981112233"))
        db.add(Product(name="Артикульний крем", name_norm="артикульний крем", sku="AB-777", warehouse="vitrina", price=10)); db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for q in ("0", "098", "0981112233", "+38 098 111"):
            assert any(r["name"] == "Телефонна Пацієнтка" for r in c.get("/lookup/patients", params={"q": q}).json()), q
        assert c.get("/lookup/patients", params={"q": "Т"}).json() == []
        it = c.get("/pos/products", params={"q": "ab-777"}).json()["items"]
        assert it and it[0]["sku"] == "AB-777"
        assert c.get("/lookup/sale-products", params={"q": "AB-777"}).json()[0]["sku"] == "AB-777"


def test_pos_patient_card_extras_and_initiator():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Мітки Пацієнтка", phone="+380671230888", client_groups=["ЛЄ розсилка"], cb_balance=301)
        db.add(p); db.flush()
        db.add(GiftCertificate(code="GC-X", balance=700, nominal=700, price=700, paid=700, patient_id=p.id))
        dn, dy = Doctor(name="Не Продавець", active=True, sale_initiator=False), Doctor(name="Так Продавець", active=True)
        db.add_all([dn, dy]); db.commit()
        pid, no_id, yes_id = p.id, dn.id, dy.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        j = c.get(f"/pos/patient/{pid}").json()
        assert j["labels"] == ["ЛЄ розсилка"] and j["certs"] == {"n": 1, "sum": 700} and j["subs"]["n"] == 0 and j["balance"] == 301
        t = c.get("/sales/new").text
        import re
        ids = re.search(r"var DOCS=(\[[^\]]*\])", t).group(1)
        assert str(yes_id) in ids and str(no_id) not in ids.replace(str(yes_id), "")
