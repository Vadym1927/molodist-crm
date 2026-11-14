from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, Doctor, Appointment
from app.services import rebook_day


def test_rebook_day():
    d = date(2026, 11, 14)
    with SessionLocal() as db:
        doc = Doctor(name="Лікар Р")
        a, b, c = Patient(full_name="Пацієнт А"), Patient(full_name="Пацієнт Б"), Patient(full_name="Пацієнт В")
        db.add_all([doc, a, b, c])
        db.flush()
        t = datetime(2026, 11, 14, 10)
        for p in (a, b, c):
            db.add(Appointment(patient_id=p.id, doctor_id=doc.id, start=t, end=t + timedelta(hours=1), status="done"))
        db.add(Appointment(patient_id=a.id, doctor_id=doc.id, start=t + timedelta(days=14), end=t + timedelta(days=14, hours=1), status="booked",
                           created_at=datetime(2026, 11, 14, 9)))
        db.add(Appointment(patient_id=b.id, doctor_id=doc.id, start=t + timedelta(days=3), end=t + timedelta(days=3, hours=1), status="cancelled"))
        db.commit()
        v = rebook_day.build(db, d)
        assert (v["total"], v["rebooked"], v["not_rebooked"], v["same_day"], v["pct"]) == (3, 1, 2, 1, 33)
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = cl.get("/reports/rebook-day?d=2026-11-14")
        assert r.status_code == 200 and "Пацієнт Б" in r.text and "33%" in r.text
