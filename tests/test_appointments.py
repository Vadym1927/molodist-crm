from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appointment, CbService, Doctor, Patient, StaffShift, Appeal


def test_appointments():
    settings.admin_password = "owner-pass-1"
    day = date.today() + timedelta(days=1)
    with SessionLocal() as db:
        d = Doctor(name="Записна Лікарка", active=True); db.add(d)
        s1 = CbService(id=990101, name="Чистка обличчя записна", price=1500, time_min=60, category="Догляд")
        s2 = CbService(id=990102, name="Маска записна", price=500, time_min=30)
        p = Patient(full_name="Записна Пацієнтка", phone="+380991112233")
        db.add_all([s1, s2, p]); db.commit()
        db.add(StaffShift(doctor_id=d.id, day=day, start="10:00", end="18:00")); db.commit()
        did, pid = d.id, p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert c.get("/lookup/patients?q=записна").json()[0]["id"] == pid
        assert c.get("/lookup/services?q=записна").json()
        assert "Новий запис" in c.get(f"/appointments/new?doc={did}&start={day}T11:00").text
        body = {"patient_id": pid, "doctor_id": did, "start": f"{day}T11:00",
                "services": [{"id": 990101, "name": "Чистка", "price": 1500, "minutes": 60}, {"id": 990102, "name": "Маска", "price": 500, "minutes": 30}],
                "place": "Кабінет 1 (косметолог)"}
        j = c.post("/appointments", json=body).json()
        assert j["ok"]; aid = j["id"]
        j2 = c.post("/appointments", json={**body, "start": f"{day}T12:00", "patient_id": None, "new_name": "Нова Записна", "new_phone": "0501110000"}).json()
        assert j2.get("warnings") and any("уже має запис" in w for w in j2["warnings"]) and any("Кабінет" in w for w in j2["warnings"])
        j3 = c.post("/appointments", json={**body, "start": f"{day}T17:30", "services": body["services"][:1]}).json()
        assert any("Поза зміною" in w for w in j3["warnings"])
        j4 = c.post("/appointments", json={**body, "start": f"{day}T17:30", "services": body["services"][:1], "force": True}).json()
        assert j4["ok"]
        with SessionLocal() as db:
            a = db.get(Appointment, aid)
            assert a.minutes == 90 and a.total == 2000 and a.end == datetime.fromisoformat(f"{day}T12:30")
        cal = c.get(f"/calendar?view=day&start={day}").text
        assert f"apptOpen({aid})" in cal and "Записна Пацієнтка" in cal
        assert "Розрахувати" in c.get(f"/appointments/{aid}").text
        r = c.post(f"/appointments/{aid}/status", data={"status": "cancelled", "reason": ""}, follow_redirects=False)
        assert "err=" in r.headers["location"]
        c.post(f"/appointments/{aid}/status", data={"status": "confirmed"})
        with SessionLocal() as db:
            assert db.get(Appointment, aid).status == "confirmed"
            ap = Appeal(name="Заявка на запис", phone="0507776655", status="Новий"); db.add(ap); db.commit(); apid = ap.id
        j5 = c.post("/appointments", json={"new_name": "Заявка на запис", "new_phone": "0507776655", "doctor_id": did, "start": f"{day}T14:00",
                                           "services": body["services"][1:], "appeal_id": apid}).json()
        assert j5["ok"]
        with SessionLocal() as db:
            ap = db.get(Appeal, apid)
            assert ap.status == "Записаний" and ap.patient_id
