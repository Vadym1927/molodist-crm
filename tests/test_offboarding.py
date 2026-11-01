"""Звільнення: CRM питає, кому передати візити й задачі, передає їх і звільняє; РРО з панелі в тестовому режимі — заборонено."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appointment, Doctor, Patient, StaffShift, Task, User
from app.security import hash_password


def test_fire_with_handover():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            d1, d2 = Doctor(name="Звільняєма Лікарка", active=True), Doctor(name="Нова Лікарка", active=True)
            p = Patient(full_name="Пацієнт Передачі")
            db.add_all([d1, d2, p]); db.flush()
            u1 = User(login="fired-doc", name="Звільняєма Лікарка", role="doctor", doctor_id=d1.id, password_hash=hash_password("x-pass-123"), active=True)
            u2 = User(login="admin-2", name="Адмін Друга", role="admin", password_hash=hash_password("x-pass-123"), active=True)
            db.add_all([u1, u2]); db.flush()
            t = datetime.now() + timedelta(days=3)
            db.add_all([Appointment(patient_id=p.id, doctor_id=d1.id, start=t, end=t + timedelta(hours=1), status="booked"),
                        Task(title="Передзвонити", assignee_id=u1.id, status="open"),
                        StaffShift(doctor_id=d1.id, day=date.today() + timedelta(days=2), start="10:00", end="18:00")])
            db.commit()
            d1id, d2id, u1id, u2id = d1.id, d2.id, u1.id, u2.id
        r = c.post(f"/specialists/{d1id}", data={"name": "Звільняєма Лікарка", "status": "fired"}, follow_redirects=False)
        assert r.headers["location"] == f"/specialists/{d1id}/fire"                       # з форми — у майстер
        h = c.get(f"/specialists/{d1id}/fire").text
        assert "Майбутні візити" in h and "Пацієнт Передачі" in h and "Передзвонити" in h and "Нова Лікарка" in h
        r = c.post(f"/specialists/{d1id}/fire", data={"to_doctor": str(d2id), "to_user": str(u2id), "block_login": "1", "drop_schedule": "1"},
                   follow_redirects=False)
        assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(Doctor, d1id).active is False and db.get(Doctor, d1id).fired_at == date.today()
        assert db.query(Appointment).filter(Appointment.doctor_id == d2id).count() == 1
        assert db.query(Task).filter(Task.assignee_id == u2id).count() == 1
        assert db.get(User, u1id).active is False
        assert db.query(StaffShift).filter(StaffShift.doctor_id == d1id).count() == 0


def test_rro_blocked_in_test_mode():
    from app.services import autonomy, fiscal
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            rid = fiscal.save_rro(db, None, {"name": "Каса Колган", "fop": "Колган", "license_key": "k", "password": "p", "login": "l"})
            autonomy.save(db, test=True)
        assert "Каси Checkbox (РРО)" in c.get("/me/panel").text
        assert c.get(f"/rro/{rid}/status").json()["state"] == "test"
        r = c.post(f"/rro/{rid}/in", data={"amount": "500"})
        assert r.status_code == 400 and "Тестовий режим" in r.json()["error"]
