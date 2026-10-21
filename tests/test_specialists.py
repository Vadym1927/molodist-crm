"""Спеціалісти: синк з CleverBox (працює / звільнений), картки, колонки календаря."""
from datetime import datetime, date, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.integrations import cleverbox as cbm
from app.main import app
from app.models import SessionLocal, Doctor, Patient, Visit
from app.services import calendar as cal_svc
from app.services.calendar import Event


class FakeCB:
    def staff(self):
        return [{"id": 5, "name": "Дарʼя", "lname": "Колган", "email": "d@m.ua", "img": "https://x/y.jpg"},
                {"id": 6, "name": "Ірина", "lname": "Мельник", "email": ""},
                {"id": 7, "name": "Олег", "lname": "Старий", "fired": 1}]


def test_staff_sync_and_pages():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Тест Пацієнтка", cleverbox_id=4242)
        db.add(p); db.flush()
        soon = datetime.now() + timedelta(days=1)
        db.add_all([Visit(source="cb_calendar", external_id="s1", patient_id=p.id, master_name="Дарʼя Колган",
                          starts_at=soon, status="booked", service_name="IPL"),
                    Visit(source="cb_service_sale", external_id="s2", patient_id=p.id, master_name="Анна Пішла",
                          starts_at=datetime.now() - timedelta(days=300), status="done", amount=1000)])
        db.commit()
        cbm.sync_staff(db, FakeCB())
        docs = {d.name: d for d in db.query(Doctor).all()}
        assert docs["Дарʼя Колган"].active and docs["Дарʼя Колган"].photo_url == "https://x/y.jpg"
        assert docs["Ірина Мельник"].active
        assert not docs["Олег Старий"].active                    # позначений звільненим у CleverBox
        assert not docs["Анна Пішла"].active                     # є в історії, немає серед співробітників
        darya = docs["Дарʼя Колган"].id
        # ручний статус не перезаписується синком
        docs["Ірина Мельник"].active, docs["Ірина Мельник"].status_locked = False, True
        db.commit()
        cbm.sync_staff(db, FakeCB())
        assert not db.get(Doctor, docs["Ірина Мельник"].id).active

        ev = {date.today(): [Event("visit", datetime.now(), "А", None, 1, doctor_id=darya),
                             Event("task", datetime.now(), "Т", None, 1, all_day=True)]}
        week = cal_svc.columns(db, ev, fixed=False)[date.today()]
        assert [c.doctor.name if c.doctor else None for c in week] == ["Дарʼя Колган", None]
        day = cal_svc.columns(db, ev, fixed=True)[date.today()]
        assert "Дарʼя Колган" in [c.doctor.name for c in day if c.doctor]

    with TestClient(app) as c:
        assert c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"},
                      follow_redirects=False).status_code == 303
        page = c.get("/specialists").text
        assert "Дарʼя Колган" in page and "Олег Старий" not in page
        assert "Олег Старий" in c.get("/specialists?show=fired").text
        card = c.get(f"/specialists/{darya}").text
        assert "IPL" in card and "Записи на 14 днів" in card
        r = c.post(f"/specialists/{darya}", data={"name": "Дарʼя Колган", "position": "лікар-косметолог",
                                                    "color": "#6f8a7a", "status": "active", "in_calendar": "1",
                                                    "work_days": "Пн–Пт"}, follow_redirects=False)
        assert r.status_code == 303
        assert "лікар-косметолог" in c.get("/specialists").text
        cal = c.get("/calendar?view=day&start=" + (date.today() + timedelta(days=1)).isoformat()).text
        assert "Дарʼя" in cal and "Тест Пацієнтка" in cal
        assert c.post("/specialists", data={"name": "Нова Майстриня", "position": "масажист"},
                      follow_redirects=False).status_code == 303
