from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, Patient, Visit, Appointment, VisitNote


def test_visit_drawer():
    settings.admin_password = "owner-pass-1"
    day = datetime.now().replace(hour=13, minute=30, second=0, microsecond=0)
    with SessionLocal() as db:
        d = Doctor(name="Вікно Лікарка", active=True, in_calendar=True, position="Дерматолог")
        p = Patient(full_name="Вікно Пацієнтка", phone="+380671112233", cb_note="любить зелений чай", cb_anamnesis="СЕКРЕТНИЙ АНАМНЕЗ")
        db.add_all([d, p]); db.commit()
        v1 = Visit(source="cb_visit", external_id="777001-1", patient_id=p.id, service_name="Консультація Beauty plan Anti Acne",
                   master_name="Вікно Лікарка", starts_at=day, ends_at=day + timedelta(minutes=90), status="booked", amount=2500)
        a = Appointment(patient_id=p.id, doctor_id=d.id, start=day + timedelta(hours=3), end=day + timedelta(hours=4),
                        services=[{"id": 1, "name": "Пілінг", "price": 1200, "minutes": 60}], status="booked", comment="перший раз")
        db.add_all([v1, a]); db.commit(); vid, aid, pid = v1.id, a.id, p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        cal = c.get(f"/calendar?view=day&start={day.date()}").text
        assert f"visitOpen(event,'v={vid}')" in cal and f"visitOpen(event,'appt={aid}')" in cal
        h = c.get(f"/calendar/visit?v={vid}").text
        assert "CleverBox 777001" in h and "Консультація Beauty plan Anti Acne" in h and "Вікно Лікарка" in h
        assert "13:30 – 15:00" in h and "любить зелений чай" in h and "СЕКРЕТНИЙ АНАМНЕЗ" not in h
        h = c.get(f"/calendar/visit?appt={aid}").text
        assert "Пілінг" in h and "Перенести" in h and f"/sales/new?appt={aid}" in h and "перший раз" in h
        r = c.post("/calendar/visit/note", json={"key": f"cb:{vid}", "text": "Нагадати про SPF"})
        assert r.json()["ok"]
        assert "Нагадати про SPF" in c.get(f"/calendar/visit?v={vid}&tab=note").text
        assert c.post("/calendar/visit/note", json={"key": "cb:999999", "text": "x"}).status_code == 404
        assert c.get("/calendar/visit?appt=999999").status_code == 404
    with SessionLocal() as db:
        assert db.query(VisitNote).filter_by(patient_id=pid).count() == 1
