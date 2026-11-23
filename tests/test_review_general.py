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
