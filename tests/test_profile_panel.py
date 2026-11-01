"""Панель профілю: фото, касова зміна (відкрити на обрані каси / закрити з підсумком), хто онлайн."""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CashShift
from app.services import cashboxes, shifts


def test_profile_panel_and_shift():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "ppOpen" in c.get("/calendar").text
        h = c.get("/me/panel").text
        assert "Ваш профіль" in h and "Зміна закрита" in h and "Зараз онлайн (1)" in h and "Вийти" in h
        with SessionLocal() as db:
            ids = [str(b["id"]) for b in shifts.boxes(db)]
        assert ids
        r = c.post("/shift/open", data={"box": ids[:2]})
        assert r.status_code == 200, r.text
        assert c.post("/shift/open", data={"box": ids[:1]}).status_code == 400          # друга відкрита — ні
        h = c.get("/me/panel").text
        assert "Зміна відкрита" in h and "Закрити зміну" in h
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        assert c.post("/me/photo", files={"file": ("me.png", png, "image/png")}, follow_redirects=False).status_code == 303
        h = c.get("/me/panel").text
        assert "/me/photo/" in h
        r = c.post("/shift/close", data={})
        assert r.status_code == 200 and r.json()["summary"]["checks"] == 0
    with SessionLocal() as db:
        s = db.query(CashShift).one()
        assert s.closed_at and len(s.boxes) == 2 and "close" in s.boxes[0]


def test_box_one_user():
    """Одну касу відкриває лише один користувач; інший — лише вільні каси."""
    from app.models import User
    from app.security import hash_password
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            db.add(User(login="adm-cash", name="Адмін Каса", role="admin", password_hash=hash_password("adm-pass-12"), active=True)); db.commit()
            ids = [str(b["id"]) for b in shifts.boxes(db)]
        assert c.post("/shift/open", data={"box": ids[:1]}).status_code == 200
    with TestClient(app) as a:
        a.post("/login", data={"login": "adm-cash", "password": "adm-pass-12"})
        h = a.get("/me/panel").text
        assert "відкрита: " in h and "Відкриті зміни інших" in h
        r = a.post("/shift/open", data={"box": ids[:1]})
        assert r.status_code == 400 and "вже відкрита" in r.json()["error"]
        assert a.post("/shift/open", data={"box": ids[1:2]}).status_code == 200
    with SessionLocal() as db:
        assert len(shifts.open_shifts(db)) == 2
