from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appointment, Patient
from app.services import patient_tags as pt


def test_tags_and_calendar_fields():
    settings.admin_password = "owner-pass-1"
    day = date.today()
    with SessionLocal() as db:
        p = Patient(full_name="Статусна Тест", phone="+380991230000", instagram="@stat_test", drink="Чай"); db.add(p); db.commit()
        for i in range(3):
            db.add(Appointment(patient_id=p.id, start=datetime(2026, 1, 1 + i, 10), end=datetime(2026, 1, 1 + i, 11), status="done", services=[]))
        db.add(Appointment(patient_id=p.id, start=datetime.combine(day, datetime.min.time()) + timedelta(hours=12),
                           end=datetime.combine(day, datetime.min.time()) + timedelta(hours=13), status="booked", services=[], comment="Важлива"))
        db.commit(); pid = p.id
        assert pt.visit_days(db, [pid])[pid] == 3
        assert pt.tag_for(pt.tags(db), 1) == "Первинний пацієнт" and pt.tag_for(pt.tags(db), 10) == "ФАНАТ"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Статуси пацієнтів" in c.get("/settings").text
        c.post("/settings/patient-tags", data={"what": "tags", "name": ["Новий", "Свій", "VIP"], "min": ["0", "2", "4"], "color": ["#111111", "#222222", "#333333"]})
        c.post("/settings/patient-tags", data={"what": "fields", "f_phone": "on", "f_comment": "on", "f_instagram": "hover", "f_drink": "on"})
        cal = c.get(f"/calendar?view=day&start={day}").text
        assert "VIP" in cal and "#333333" in cal                      # 3 завершені + цей = 4 → VIP
        assert "Напій, якому надає перевагу: Чай" in cal and 'class="ext"' in cal and "@stat_test" in cal and "Важлива" in cal
