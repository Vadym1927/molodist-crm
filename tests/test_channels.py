import hashlib
import hmac
import json

import pytest

from fastapi.testclient import TestClient

from app.config import settings
from app.models import SessionLocal
from app.main import app
from app.services import channels as ch


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    for k, v in ch.ENV0.items():
        setattr(settings, k, v)


def _login(c):
    c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})


def test_channels_page_and_save():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        _login(c)
        t = c.get("/settings/channels").text
        assert "MOLODIST.CLINIC" in t and "/api/public/turbosms/dlr" in t
        assert 'href="/settings/channels"' in c.get("/settings").text
        r = c.post("/settings/channels/1", data={"on": "1", "name": "Sms", "sender": "MOLODIST", "token": "tok-secret-123",
                                                 "allowed_from": "08:00", "allowed_to": "21:30", "webhook_secret": "whk"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert settings.turbosms_token == "tok-secret-123" and settings.turbosms_sender_sms == "MOLODIST"
        assert settings.quiet_hours_start == 21 and settings.quiet_hours_end == 8
        page = c.get("/settings/channels").text
        assert "tok-secret-123" not in page and "збережено" in page
        # вимкнути SMS → відправник порожній, токен лишається
        c.post("/settings/channels/1", data={"name": "Sms", "sender": "MOLODIST"})
        assert settings.turbosms_sender_sms == "" and settings.turbosms_token == "tok-secret-123"
        # DLR з підписом
        body = json.dumps({"message_id": "abc-1", "status": "Delivered"}).encode()
        assert c.post("/api/public/turbosms/dlr", content=body, headers={"X-Signature": "bad"}).status_code == 403
        sig = hmac.new(b"whk", body, hashlib.sha1).hexdigest()
        r = c.post("/api/public/turbosms/dlr", content=body, headers={"X-Signature": sig})
        assert r.status_code == 200 and r.json()["ok"]


def test_channel_test_telegram():
    class R:
        def __init__(self, js): self._js = js
        def json(self): return self._js
    class H:
        def get(self, url, **kw): return R({"ok": True, "result": {"username": "molodist_bot"}})
        def post(self, url, **kw): return R({"response_result": {"balance": 123.4}})
    db = SessionLocal()
    ch.update(db, 3, None, True, {}, {"token": "123:abc"})
    ok, msg = ch.test(db, 3, H())
    assert ok and "@molodist_bot" in msg
    ch.update(db, 1, None, True, {}, {"token": "sms-tok"})
    ok, msg = ch.test(db, 1, H())
    assert ok and "123.40" in msg
    ok, msg = ch.test(db, 4, H())
    assert not ok
    db.close()
