from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Call, Patient
from app.services import telephony as tel


def test_ringostat_events_flow():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add(Patient(full_name="Дзвінкова Пацієнтка", phone="+380671112233")); db.commit()
        tel.save(db, "ringostat", {"project_id": "1", "auth_key": "k"}, "Дзвінок", True, False)
        hook = tel.public(db)["hook"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Webhooks 2.0" in c.get("/settings/telephony").text
        u = f"/api/public/telephony/ringostat/{hook}"
        body = {"call_id": "$cdr_id", "uid": "777", "caller": "380671112233", "callee": "0443334455", "type": "in", "status": "$disposition"}
        assert c.post(u + "?event=ringing", json=body).json()["ok"]
        live = c.get("/telephony/live").json()["calls"]
        assert live and live[0]["name"] == "Дзвінкова Пацієнтка" and live[0]["status"] == "ringing"
        c.post(u + "?event=answer", json=body)
        c.post(u + "?event=end", json={**body, "status": "PROPER", "dialog": "95", "record_link": "https://rec/1.wav", "employee_fio": "Адмін Оля"})
        assert c.get("/telephony/live").json()["calls"] == []
    with SessionLocal() as db:
        calls = db.query(Call).all()
        assert len(calls) == 1 and calls[0].status == "answered" and calls[0].duration == 95 and calls[0].record_url.endswith("1.wav")
        assert calls[0].employee == "Адмін Оля" and calls[0].patient_id
