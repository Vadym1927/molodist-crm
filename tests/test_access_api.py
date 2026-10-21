"""Ролі веб-інтерфейсу та REST API."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, User
from app.security import hash_password, ensure_owner

PDF = Path(__file__).parent / "sample_plan.pdf"
needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="потрібен tests/sample_plan.pdf")


def login(c: TestClient, lg: str, pw: str):
    r = c.post("/login", data={"login": lg, "password": pw}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", r.text[:300]


@pytest.fixture
def client():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        yield c


@needs_pdf
def test_roles_and_api(client):
    c = client
    # власник створюється автоматично
    login(c, settings.owner_login, "owner-pass-1")
    r = c.post("/upload", files={"files": ("plan.pdf", PDF.read_bytes(), "application/pdf")}, follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    page = c.get(f"/patients/{pid}").text
    assert "Медичні нотатки" in page and "Видалити план" in page
    assert "Фінансовий прогноз" in c.get("/").text

    with SessionLocal() as db:
        doc = db.query(Doctor).first()
        other = Doctor(name="Інший Лікар"); db.add(other); db.commit()
        doc_id, other_id = doc.id, other.id
    # створення користувачів через UI власника
    for lg, role, d in [("admin1", "admin", ""), ("doc1", "doctor", str(doc_id)), ("doc2", "doctor", str(other_id))]:
        r = c.post("/access/users", data={"login": lg, "name": lg, "role": role, "doctor_id": d,
                                          "password": "password-123", "active": "1"}, follow_redirects=False)
        assert r.status_code == 303

    # API-ключ (показується один раз)
    shown = c.post("/access/api/keys", data={"name": "n8n", "scopes": ["read", "write"]}).text
    key = re.search(r"<pre[^>]*>(mcrm_[\w-]+)</pre>", shown).group(1)
    assert key not in c.get("/access/api").text          # повторно ключ не показується
    assert c.get("/access/audit").status_code == 200
    c.get("/logout")

    # адмін: усі пацієнти, без фінансів, без мед. нотаток, без доступів
    login(c, "admin1", "password-123")
    assert c.get(f"/patients/{pid}").status_code == 200
    assert "Медичні нотатки" not in c.get(f"/patients/{pid}").text
    dash = c.get("/").text
    assert "Фінансовий прогноз" not in dash and "Сума планів" not in dash and "Не записані" in dash
    assert c.get("/access").status_code == 403
    assert c.post(f"/plans/1/delete").status_code == 403
    c.get("/logout")

    # лікар цього пацієнта бачить, інший — ні
    login(c, "doc1", "password-123")
    assert "Медичні нотатки" in c.get(f"/patients/{pid}").text
    assert "Мій KPI" in c.get("/").text
    c.get("/logout")
    login(c, "doc2", "password-123")
    assert c.get(f"/patients/{pid}").status_code == 403
    assert "Даниленко" not in c.get("/patients").text
    c.get("/logout")

    # REST API
    assert c.get("/api/v1/patients").status_code == 401
    h = {"X-API-Key": key}
    js = c.get("/api/v1/patients", headers=h).json()
    assert js["total"] == 1 and js["items"][0]["last_plan"]["planned"] == 26450
    plan = c.get("/api/v1/plans/1", headers=h).json()
    assert "medical_notes" not in plan and plan["procedures"]["planned"] == 5
    r = c.post("/api/v1/plans", headers=h, json={
        "patient": {"full_name": "Тест Пацієнтка", "phone": "0501234567"}, "doctor": "Інший Лікар",
        "items": [{"kind": "procedure", "name": "Біоревіталізація Rejuran", "qty": 3, "unit_price": 6500}]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["totals"]["planned"] == 19500 and new["items"][0]["interval_days"] == 14
    r = c.patch(f"/api/v1/plans/{new['id']}", headers=h, json={"items": [{"id": new["items"][0]["id"], "manual_done": 1}]})
    assert r.json()["procedures"]["done"] == 1
    k = c.get("/api/v1/kpi", headers=h).json()
    assert k["total"]["plans"] == 2 and len(k["by_doctor"]) == 2
    assert c.get("/api/docs").status_code in (303, 200)     # без сесії — редірект на вхід
