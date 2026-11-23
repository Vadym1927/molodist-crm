from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Appointment, Patient, SessionLocal
from app.services import reviews as rv


def test_general_review_link():
    with SessionLocal() as db:
        p = Patient(full_name="Опитана Олена", phone="+380671230099")
        db.add(p); db.flush()
        st = datetime.now() - timedelta(hours=3)
        ap = Appointment(patient_id=p.id, start=st, end=st + timedelta(hours=1), status="done")
        db.add(ap); db.commit()
        r = rv.general(db, "067 123 00 99")
        assert r.patient_id == p.id and r.appointment_id == ap.id and r.channel == "link"
        assert rv.general(db, "+380671230099").id == r.id            # те саме опитування, не дубль
        with pytest.raises(ValueError):
            rv.general(db, "+380990000001")
    with TestClient(app) as c:
        assert "номер телефону" in c.get("/r/new").text
        x = c.post("/r/new", data={"phone": "+380671230099"}, follow_redirects=False)
        assert x.status_code == 303 and x.headers["location"].startswith("/r/")
        assert "Не знайшли" in c.post("/r/new", data={"phone": "+380990000001"}).text


def test_review_by_cleverbox_id_and_phone_forms():
    with SessionLocal() as db:
        p = Patient(full_name="Без Логіна", phone="+380731247411", cleverbox_id=990077)
        old = Patient(full_name="Давній Візит", phone="+380731247412", cleverbox_id=990078)
        db.add_all([p, old]); db.flush()
        st = datetime.now() - timedelta(hours=2)
        db.add(Appointment(patient_id=p.id, start=st, end=st + timedelta(hours=1), status="done"))
        db.commit()
        for ph in ("731247411", "73 124 74 11", "0731247411", "380731247411", "+38 (073) 124-74-11"):
            assert rv.general(db, ph).patient_id == p.id
        with pytest.raises(ValueError):
            rv.general(db, "7312474")
    with TestClient(app) as c:
        x = c.get("/r/c/990077", follow_redirects=False)
        assert x.status_code == 303 and x.headers["location"].startswith("/r/") and x.headers["location"] != "/r/new"
        assert c.get("/r/c/990078", follow_redirects=False).headers["location"] == "/r/new"     # без свіжого візиту — нічого не відкриває
        assert c.get("/r/c/123456789", follow_redirects=False).headers["location"] == "/r/new"
        assert "+380" in c.get("/r/new").text


def test_removed_service_page_is_gone():
    with TestClient(app) as c:
        assert c.get("/_site/trihologiya").status_code == 410


def test_old_papilloma_page_redirects():
    with TestClient(app) as c:
        x = c.get("/_site/udalenie-papilom", follow_redirects=False)
        assert x.status_code == 301 and x.headers["location"].endswith("/vydalennia-rodymok")
        assert c.get("/_site/vydalennia-rodymok").status_code == 200
