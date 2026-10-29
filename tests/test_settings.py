from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_settings_hub():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/settings").text
        for block in ("CRM MOLODIST", "Основні налаштування", "Комунікації", "Віджети та форми", "Налаштування фінансів",
                      "Звернення", "Співробітники й графік"):
            assert block in t
        assert "#setup/dirs/appeal_deny" not in t and 'href="/access"' in t
        assert 'href="/settings"' in c.get("/").text


def test_empty_query_params_ignored():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.get("/clients?q=%D0%91%D0%BE%D0%BD%D0%B4%D0%B0%D1%80&doctor=&seg=all")
        assert r.status_code == 200 and "Пацієнти" in r.text
