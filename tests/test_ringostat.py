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


def test_call_ai_popup_create_patient_and_kpi():
    from datetime import date
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        tel.save(db, "ringostat", {"project_id": "1", "auth_key": "k"}, "Дзвінок", True, False)
        tel.save_extra(db, [], True, "критерії", 70, False)
        hook = tel.public(db)["hook"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        u = f"/api/public/telephony/ringostat/{hook}"
        body = {"uid": "900", "caller": "380509998877", "type": "in"}
        c.post(u + "?event=ringing", json=body)
        lv = c.get("/telephony/live").json()["calls"][0]
        assert lv["pid"] is None
        r = c.post(f"/telephony/calls/{lv['id']}/patient", json={"last": "Новенька", "first": "Ірина"}).json()
        assert r["ok"] and r["name"] == "Новенька Ірина"
        c.post(u + "?event=end", json={**body, "status": "PROPER", "dialog": "60", "employee_fio": "Оля Адмін"})
        c.post(u + "?event=ai", json={"uid": "900", "caller": "380509998877", "summary": "Запис на чистку",
                                       "transcript": "WEBVTT\n\n00:00.000 --> 00:02.000\n<v Адмін>Добрий день, MOLODIST\n"})
    with SessionLocal() as db:
        cl = db.query(Call).one()
        assert cl.summary == "Запис на чистку" and "Адмін: Добрий день" in cl.transcript and cl.patient_id
        tel.evaluate(db, cl, claude=lambda prompt: {"score": 55, "comment": "Не запропонувала час", "booked": False})
        assert cl.ai_score == 55
        k = tel.kpi(db, date.today().replace(day=1), date.today())
        assert k[0]["name"] == "Оля Адмін" and k[0]["score"] == 55 and k[0]["low"] == 1
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/telephony").text
        assert "KPI дзвінків" in t and "AI 55/100" in t and "Текст діалогу" in t
        assert "Дзвінки · 1" in c.get(f"/patients/{cl.patient_id}").text
