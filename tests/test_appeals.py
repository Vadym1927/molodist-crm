from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appeal, Patient, Task


def test_appeals_flow():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add(Patient(full_name="Відома Пацієнтка", phone="+380 67 111 22 33")); db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Звернення" in c.get("/appeals").text
        nc = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        r = c.post("/appeals", data={"name": "Нова Заявка", "phone": "0501112299", "source": "Instagram", "cause": "Ціна",
                                     "interest": "ботулінотерапія", "marks": ["Гаряча"], "next_contact": nc}, follow_redirects=False)
        assert r.status_code == 303
        c.post("/appeals", data={"name": "Відома", "phone": "0671112233", "source": "Google"})
        with SessionLocal() as db:
            a1 = db.query(Appeal).filter_by(name="Нова Заявка").one(); a2 = db.query(Appeal).filter_by(name="Відома").one()
            assert a1.marks == ["Гаряча"] and a1.status == "Новий" and a2.patient_id    # знайдено за телефоном
            assert db.query(Task).filter_by(source_key=f"appeal:{a1.id}").one().status == "open"
            id1 = a1.id
        card = c.get(f"/appeals/{id1}").text
        assert "Нова Заявка" in card and "Звернення створено" in card
        c.post(f"/appeals/{id1}/note", data={"text": "Передзвонити після 18:00"})
        c.post(f"/appeals/{id1}/status", data={"status": "Відмова", "deny_reason": "Дорого"})
        with SessionLocal() as db:
            a1 = db.get(Appeal, id1)
            assert a1.closed_at and a1.deny_reason == "Дорого" and db.query(Task).filter_by(source_key=f"appeal:{id1}").one().status == "done"
        board = c.get("/appeals").text
        assert "Дорого" in board and "Instagram" in board
        r = c.post(f"/appeals/{id1}/patient", follow_redirects=False)
        assert "/patients/" in r.headers["location"]
        with SessionLocal() as db:
            assert db.query(Patient).filter_by(full_name="Нова Заявка").count() == 1
        c.post("/settings/appeals/sources", data={"action": "add", "name": "TikTok"})
        c.post("/settings/appeals/statuses", data={"action": "add", "name": "Думає", "type": "work", "color": "#123456"})
        assert "TikTok" in c.get("/settings/appeals").text and "Думає" in c.get("/appeals").text
        assert 'href="/appeals"' in c.get("/").text
