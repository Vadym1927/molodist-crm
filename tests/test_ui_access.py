"""Доступ до розділів / вкладок / кнопок: власник вимикає галочкою — елемент зникає й сервер відповідає 403."""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, User
from app.security import hash_password
from app.services import ui_access as ua


def test_ui_access():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        r = c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"}, follow_redirects=False)
        with SessionLocal() as db:
            db.add(User(login="adm-ui", name="Адмін Обмежений", role="admin", password_hash=hash_password("adm-pass-12"), active=True))
            db.commit()
        assert r.status_code == 303, (r.status_code, r.text[:300])
        assert r.headers["location"] == "/calendar"
        h = c.get("/").text
        assert 'class="brand" href="/calendar"' in h
        page = c.get("/access/ui?role=admin").text
        assert "Розділи й кнопки" in page and "Повернення чека" in page and "Поставки" in page
        allow = [k for k in ua.ITEMS if k not in ("sale_refund", "stock_supply", "sec_reports")]
        assert c.post("/access/ui", data={"role": "admin", "allow": allow}, follow_redirects=False).status_code == 303
    with TestClient(app) as a:
        a.post("/login", data={"login": "adm-ui", "password": "adm-pass-12"})
        assert a.get("/reports").status_code == 403
        assert a.get("/stock?tab=supply").status_code == 403
        assert a.get("/stock").status_code == 200 and a.get("/stock?tab=writeoff").status_code == 200
        r = a.post("/sales/1/refund", data={})
        assert r.status_code == 403 and "Повернення чека" in r.text
        cal = a.get("/calendar").text
        assert "UI_DENY" not in cal and "/reports" in cal                     # правила для браузера передані
    assert ua.blocked("owner", "GET", "/reports", {}) is None
