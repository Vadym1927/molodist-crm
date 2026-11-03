from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appointment, Doctor, Patient, SyncState


def _setup():
    settings.admin_password = "owner-pass-1"
    day = date.today() + timedelta(days=1)
    with SessionLocal() as db:
        d = Doctor(name="Статусна Лікарка", active=True); p = Patient(full_name="Статусна Пацієнтка", phone="+380991110000")
        db.add_all([d, p]); db.commit()
        a = Appointment(patient_id=p.id, doctor_id=d.id, start=datetime.combine(day, datetime.min.time()) + timedelta(hours=11),
                        end=datetime.combine(day, datetime.min.time()) + timedelta(hours=12), services=[{"name": "Чистка"}], status="booked")
        db.add(a); db.commit()
        return day, a.id


def test_visit_statuses_settings_and_calendar():
    day, aid = _setup()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Статуси візитів" in c.get("/settings").text
        page = c.get("/settings/visit-statuses").text
        assert "Оплачено ₴" in page and "Скасовано" in page
        keys = ["booked", "confirmed", "arrived", "paid", "done", "no_show", "cancelled", ""]
        r = c.post("/settings/visit-statuses", data={"key": keys, "label": ["Записаний", "Підтвердив", "Прийшов", "Оплачено ₴", "Завершено", "Не прийшов", "Відміна", "Чекає оплати"],
                   "color": ["#111111", "#222222", "#333333", "#444444", "#555555", "#666666", "#777777", "#888888"],
                   "on": ["confirmed", "arrived", "paid", "no_show"], "cal": ["cancelled"]}, follow_redirects=False)
        assert r.status_code == 303
        from app.services import visit_statuses as vs
        with SessionLocal() as db:
            lb = vs.labels(db)
            assert lb["confirmed"] == "Підтвердив" and lb["c1"] == "Чекає оплати" and "paid" not in lb
            assert vs.colors(db)["booked"] == "#111111" and vs.show_cancelled(db)
        cal = c.get(f"/calendar?view=day&start={day}").text
        assert ".s-booked{--bc:#111111" in cal and "Чекає оплати" in cal
        assert c.post(f"/appointments/{aid}/status", data={"status": "c1", "ajax": "1"}).json()["ok"]
        assert c.post(f"/appointments/{aid}/status", data={"status": "nope", "ajax": "1"}).status_code == 400
        c.post(f"/appointments/{aid}/status", data={"status": "cancelled", "reason": "захворіла", "ajax": "1"})
        cal = c.get(f"/calendar?view=day&start={day}").text
        assert "s-cancelled" in cal                                         # скасовані показуються, бо увімкнено
        # свій статус, що стоїть у записі, не видаляється — лише вимикається
        with SessionLocal() as db:
            db.get(Appointment, aid).status = "c1"; db.commit()
        c.post("/settings/visit-statuses", data={"key": keys[:-1], "label": ["", "", "", "", "", "", ""], "color": ["#111111"] * 7, "on": []})
        with SessionLocal() as db:
            assert "c1" in vs.labels(db) and "c1" not in vs.labels(db, only_on=True)


def test_shift_required_for_sale_and_close():
    day, aid = _setup()
    with SessionLocal() as db:
        db.merge(SyncState(key="shift_required", value="1")); db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        j = c.post(f"/appointments/{aid}/status", data={"status": "done", "ajax": "1"})
        assert j.status_code == 400 and "Каса не відкрита" in j.json()["error"]
        r = c.post("/sales", json={"patient_id": None, "items": [], "payments": []})
        assert r.status_code == 400 and "Каса не відкрита" in r.json()["error"]
        assert c.post(f"/appointments/{aid}/status", data={"status": "arrived", "ajax": "1"}).json()["ok"]
        c.post("/settings/shift-required", data={"on": ""})
        assert c.post(f"/appointments/{aid}/status", data={"status": "done", "ajax": "1"}).json()["ok"]
