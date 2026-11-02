from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_finance_hub():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get("/finance").text
        assert "fwOpen" in h and "Нарахування зарплати" in h and "Каси й рахунки" in h and 'id="fw-f"' in h
        assert "in-frame" in c.get("/finance/ops").text
