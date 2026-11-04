"""Спільні розхідники: один комплект на візит з кількома послугами; виправлення витратних оплаченого візиту з паролем."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import (SessionLocal, Appointment, CbService, Doctor, Patient, Product, StockDoc, StockMove, TechCard, TechCardLine,
                        User, VisitMaterial)
from app.security import hash_password
from app.services import techcards as tc


def test_shared_materials_and_unlock():
    settings.admin_password = "owner-pass-1"
    start = (datetime.now() + timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        d = Doctor(name="Спільна Лікарка", active=True)
        pt = Patient(full_name="Спільна Пацієнтка", phone="+380501112255")
        glove = Product(name="Спільні Рукавички нітрилові", name_norm="спільні рукавички нітрилові", warehouse="single", unit="шт.", cost=2)
        cotton = Product(name="Спільні Ватні диски", name_norm="спільні ватні диски", warehouse="single", unit="шт.", cost=0.5)
        syr = Product(name="Спільний Шприц інсуліновий", name_norm="спільний шприц інсуліновий", warehouse="single", unit="шт.", cost=5)
        db.add_all([d, pt, glove, cotton, syr]); db.flush()
        c1 = TechCard(name="Спільна ТК 1", cats=list(tc.DEFAULT_CATS), lines=[TechCardLine(cat="Одноразка", product_id=glove.id, qty=2),
                      TechCardLine(cat="Одноразка", product_id=cotton.id, qty=7), TechCardLine(cat="Одноразка", product_id=syr.id, qty=3)])
        c2 = TechCard(name="Спільна ТК 2", cats=list(tc.DEFAULT_CATS), lines=[TechCardLine(cat="Одноразка", product_id=glove.id, qty=2),
                      TechCardLine(cat="Одноразка", product_id=cotton.id, qty=5), TechCardLine(cat="Одноразка", product_id=syr.id, qty=1)])
        db.add_all([c1, c2]); db.flush()
        db.add_all([CbService(id=9611, name="Спільна послуга 1", price=1000, time_min=30, techcard_id=c1.id),
                    CbService(id=9612, name="Спільна послуга 2", price=1000, time_min=30, techcard_id=c2.id)])
        db.commit()
        tc.save_shared(db, True, [glove.id, cotton.id])
        ids = dict(d=d.id, pt=pt.id, glove=glove.id, cotton=cotton.id, syr=syr.id)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            db.query(User).filter(User.login == settings.owner_login).one().delete_pw_hash = hash_password("del-2222"); db.commit()
        assert "один комплект на візит" in c.get("/settings/shared-materials").text
        aid = c.post("/appointments", json={"patient_id": ids["pt"], "doctor_id": ids["d"], "start": start.isoformat(timespec="minutes"), "force": True,
                                            "services": [{"id": 9611, "name": "Спільна послуга 1", "price": 1000, "minutes": 30},
                                                         {"id": 9612, "name": "Спільна послуга 2", "price": 1000, "minutes": 30}]}).json()["id"]
        with SessionLocal() as db:
            assert tc.cost_by_service(db, aid) == {0: 2 * 2 + 7 * 0.5 + 15, 1: 5}       # друга послуга: лише шприц
        assert "у комплекті візиту" in c.get(f"/calendar/visit?appt={aid}&tab=cost").text
        r = c.post("/sales", json={"patient_id": ids["pt"], "appointment_id": aid, "payments": [{"method": "Готівка", "amount": 2000}],
                                   "items": [{"kind": "service", "ref_id": 9611, "name": "Спільна послуга 1", "qty": 1, "price": 1000, "doctor_id": ids["d"]},
                                             {"kind": "service", "ref_id": 9612, "name": "Спільна послуга 2", "qty": 1, "price": 1000, "doctor_id": ids["d"]}]})
        assert r.status_code == 200, r.text

        def moves():
            with SessionLocal() as db:
                docs = db.query(StockDoc).filter(StockDoc.ext_no == f"visit:{aid}", StockDoc.status == "posted").all()
                out = {}
                for m in db.query(StockMove).filter(StockMove.doc_id.in_([x.id for x in docs] or [0])):
                    out[m.product_id] = out.get(m.product_id, 0) + m.qty
                return out
        mv = moves()
        assert mv[ids["glove"]] == -2 and mv[ids["cotton"]] == -7 and mv[ids["syr"]] == -4
        # виправлення після оплати: без пароля — ні
        bad = c.post(f"/appointments/{aid}/materials-unlock", data={"del_password": "x"}, follow_redirects=False)
        assert "delerr=" in bad.headers["location"] and moves()
        ok = c.post(f"/appointments/{aid}/materials-unlock", data={"del_password": "del-2222"}, follow_redirects=False)
        assert ok.status_code == 303 and moves() == {}
        with SessionLocal() as db:
            tc.sync(db)                                   # планувальник не закриває візит, поки йде виправлення
            assert not tc.closed(db, db.get(Appointment, aid))
            sm = db.query(VisitMaterial).filter_by(appt_id=aid, product_id=ids["syr"], svc_idx=0).one().id
        assert "Списати зі складу" in c.get(f"/calendar/visit?appt={aid}&tab=cost").text
        c.post(f"/appointments/{aid}/materials/{sm}", json={"qty": "2"})
        c.post(f"/appointments/{aid}/materials-close", follow_redirects=False)
        assert moves()[ids["syr"]] == -3
        with SessionLocal() as db:
            assert aid not in tc.editing(db)
