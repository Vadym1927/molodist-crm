import re

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, User
from app.services import login2fa


def test_login_with_sms_code(monkeypatch):
    settings.admin_password = "owner-pass-1"
    sent = []
    monkeypatch.setattr(login2fa, "channels", lambda u: ["sms"])

    def fake_send(phone, text, channels, sequence_id=None, internal=False):
        from app.integrations.messaging import SendResult
        assert internal
        sent.append(re.search(r"\d{6}", text).group(0))
        return SendResult(True, "sms", message_id="x")
    monkeypatch.setattr("app.integrations.messaging.send_turbosms", fake_send)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            u = db.query(User).filter_by(login=settings.owner_login).one()
            u.phone = "+380501234567"
            db.commit()
            login2fa.save(db, ["owner"], 30)
        c.get("/logout")
        r = c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"}, follow_redirects=False)
        assert r.headers["location"] == "/login/code" and len(sent) == 1
        assert c.get("/calendar", follow_redirects=False).status_code in (302, 303)      # без коду — не пускає
        bad = c.post("/login/code", data={"code": "000000" if sent[0] != "000000" else "111111"})
        assert "Невірний" in bad.text
        ok = c.post("/login/code", data={"code": sent[0], "remember": "1"}, follow_redirects=False)
        assert ok.status_code == 303 and "mtd" in ok.cookies
        c.get("/logout")
        r2 = c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"}, follow_redirects=False)
        assert r2.headers["location"] != "/login/code" and len(sent) == 1          # запамʼятований пристрій
        assert "Вхід із кодом" in c.get("/settings/login-2fa").text
        with SessionLocal() as db:
            login2fa.save(db, [], 30)


def test_code_check_expiry():
    import time
    h = login2fa.code_hash(5, "123456", int(time.time()) + 100)
    assert login2fa.check(5, f"{h}|{int(time.time()) + 100}", "123 456")
    old = int(time.time()) - 1
    assert not login2fa.check(5, f"{login2fa.code_hash(5, '123456', old)}|{old}", "123456")
