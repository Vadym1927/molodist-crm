from datetime import datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import Appointment, Patient, Review, SessionLocal
from app.services import reviews as rv


def _login(c):
    c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})


def test_manual_survey_send_templates_and_mark():
    settings.admin_password = "owner-pass-1"
    with TestClient(app):
        pass
    db = SessionLocal()
    p = Patient(full_name="Тестова Олена Петрівна", phone="+380671112233")
    db.add(p); db.commit()
    ap = Appointment(patient_id=p.id, start=datetime(2026, 11, 18, 10, 0), end=datetime(2026, 11, 18, 11, 0), status="done")
    db.add(ap); db.commit()
    c = rv.cfg(db); c["on"] = False
    c["templates"] = [{"name": "Перший", "text": "{name}, оцініть: {link}"}, {"name": "Другий", "text": "Дякуємо, {name}! {link}"}]
    rv.save_cfg(db, c)
    r, texts = rv.manual(db, ap)
    assert [t["name"] for t in texts] == ["Перший", "Другий"]
    assert texts[0]["text"].startswith("Олена, оцініть: ") and r.token in texts[0]["text"]
    r2, _ = rv.manual(db, ap)
    assert r2.id == r.id  # одне посилання на візит
    with TestClient(app) as cl:
        _login(cl)
        h = cl.get(f"/appointments/{ap.id}/survey")
        assert h.status_code == 200 and "Перший" in h.text and "Viber" in h.text
        assert cl.post(f"/reviews/{r.id}/sent", data={"ch": "viber"}).json()["ok"]
    db.expire_all()
    rr = db.get(Review, r.id)
    assert rr.channel == "viber" and rr.sent_at
    db.close()
