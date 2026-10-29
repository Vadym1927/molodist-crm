from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, StaffShift


def test_schedule():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        d = Doctor(name="Графікова Лікарка", active=True, position="Косметолог")
        db.add(d); db.commit(); did = d.id
    mon = date.today() - timedelta(days=date.today().weekday())
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Графікова Лікарка" in c.get("/specialists/schedule").text
        c.post("/specialists/schedule/fill", data={"doctor_id": str(did), "d_from": str(mon), "d_to": str(mon + timedelta(days=6)),
               "mode": "weekdays", "wd": ["0", "1", "2", "3", "4"], "start": "10:00", "end": "19:00", "place": "Кабінет 1 (косметолог)", "overwrite": "1"})
        with SessionLocal() as db:
            assert db.query(StaffShift).filter_by(doctor_id=did).count() == 5
        c.post("/specialists/schedule/cell", data={"doctor_id": str(did), "day": str(mon), "off": "1"})
        c.post("/specialists/schedule/cell", data={"doctor_id": str(did), "day": str(mon + timedelta(days=5)), "start": "11:00", "end": "15:00"})
        bad = c.post("/specialists/schedule/cell", data={"doctor_id": str(did), "day": str(mon), "start": "18:00", "end": "09:00"}, follow_redirects=False)
        assert "err=" in bad.headers["location"]
        c.post("/specialists/schedule/copy", data={"src": str(mon), "weeks": "2"})
        with SessionLocal() as db:
            assert db.query(StaffShift).filter_by(doctor_id=did).count() == 15       # 5 змін × 3 тижні
        c.post("/specialists/schedule/fill", data={"doctor_id": str(did), "d_from": str(mon + timedelta(days=21)), "d_to": str(mon + timedelta(days=28)),
               "mode": "2/2", "start": "09:00", "end": "21:00"})
        with SessionLocal() as db:
            days = sorted(s.day for s in db.query(StaffShift).filter(StaffShift.doctor_id == did, StaffShift.day >= mon + timedelta(days=21)))
            assert days[:4] == [mon + timedelta(days=21), mon + timedelta(days=22), mon + timedelta(days=25), mon + timedelta(days=26)]
        cal = c.get(f"/calendar?view=day&start={mon + timedelta(days=1)}").text
        assert "10:00–19:00" in cal and "offsh" in cal
        assert c.get("/calendar?view=day").status_code == 200
        r = c.post("/specialists/schedule/clear", data={"doctor_id": str(did), "d_from": str(mon), "d_to": str(mon + timedelta(days=40)), "del_password": "x"}, follow_redirects=False)
        assert "delerr=" in r.headers["location"]
