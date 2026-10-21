"""Вебхуки CleverBox: секрет, розбір подій, створення клієнта, планування швидкого синку."""
from fastapi.testclient import TestClient

from app.config import settings
from app.integrations import cb_webhooks as wh
from app.main import app
from app.models import SessionLocal, Patient


def test_parse_variants():
    assert wh.parse({"event": "visit_create", "data": {"id": 55, "client_id": "777"}}) == \
        {"event": "visit_create", "client_id": 777, "visit_id": 55, "client": {}}
    p = wh.parse({"type": "client_create", "data": {"id": 9, "name": "Нова", "phone": "067"}})
    assert p["client_id"] == 9 and p["client"]["name"] == "Нова"
    p = wh.parse({"event": "visit_status", "visit": {"id": 3, "client": {"id": 12, "name": "Х"}}})
    assert p["client_id"] == 12


def test_hook_endpoint(monkeypatch):
    settings.admin_password = "owner-pass-1"
    calls = []
    monkeypatch.setattr(wh, "schedule", lambda cid=None: calls.append(cid))
    with TestClient(app) as c:
        assert c.post("/hooks/cleverbox/wrong", json={}).status_code == 404
        with SessionLocal() as db:
            url = wh.hook_url(db)
        path = url.split("://", 1)[1].split("/", 1)[1]
        r = c.post("/" + path, json={"event": "client_create", "data": {"id": 31337, "name": "Вебхук Клієнтка",
                                                                           "phone": "0671112299"}})
        assert r.status_code == 200 and r.json()["event"] == "client_create"
        assert calls == [31337]
        with SessionLocal() as db:
            p = db.query(Patient).filter_by(cleverbox_id=31337).one()
            assert p.full_name == "Вебхук Клієнтка"
            st = wh.status(db)
            assert st["count"] == 1 and "data" in st["last"]["shape"]
            assert "Вебхук" not in str(st["last"])            # значення не зберігаються
        c.post("/login", data={"login": settings.owner_login, "password": settings.admin_password})
        assert "Миттєве оновлення" in c.get("/data").text
