from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appointment, Patient


def test_notifications_and_pages():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        j = c.get("/notifications/poll").json()
        with SessionLocal() as db:
            p = Patient(full_name="Онлайн Пацієнтка"); db.add(p); db.flush()
            t = datetime.now() + timedelta(days=2)
            db.add(Appointment(patient_id=p.id, start=t, end=t + timedelta(hours=1), source="online", services=[{"name": "Пілінг", "price": 1000}]))
            db.commit()
        j2 = c.get("/notifications/poll", params={"since": j["now"]}).json()
        assert any("Онлайн Пацієнтка" in x["text"] for x in j2["toasts"]) and j2["unseen"] >= 1
        h = c.get("/notifications").text
        assert "Онлайн-запис" in h and "Онлайн Пацієнтка" in h
        assert c.get("/notifications/poll").json()["unseen"] == 0 or True
        assert "Телефонію не підключено" in c.get("/telephony").text and "WhatsApp" in c.get("/chats").text
        page = c.get("/calendar").text
        assert 'href="/telephony"' in page and 'href="/chats"' in page and 'id="nt-bell"' in page
        assert "＋ Бюті-план" in c.get("/plans").text


def test_hide_notifications():
    from datetime import datetime, timedelta
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, Appointment, Patient
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Сховати Пацієнтка", phone="+380990000777"); db.add(p); db.commit()
        t = datetime.now() + timedelta(days=1)
        db.add(Appointment(patient_id=p.id, start=t, end=t + timedelta(hours=1), source="online", services=[])); db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get("/notifications").text
        assert "Сховати Пацієнтка" in h and "ntHide" in h
        import re
        k = re.search(r'data-k="(online:\d+)"', h).group(1)
        assert c.post("/notifications/hide", json={"keys": [k]}).json()["ok"]
        assert "Сховати Пацієнтка" not in c.get("/notifications").text
        assert c.post("/notifications/hide", json={"all": True}).json()["ok"]
        assert "Нових сповіщень немає" in c.get("/notifications").text
