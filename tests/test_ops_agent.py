from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, ApiKey, OpsTask


def test_ops_flow():
    from app.security import new_api_key
    from app.services import ops_tasks
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="openclaw-ops", prefix=prefix, key_hash=hsh, scopes=["ops"]))
        db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert c.post("/ops", data={"title": "Диск", "kind": "check", "text": "df -h /"}, follow_redirects=False).status_code == 303
        c.post("/ops", data={"title": "Чистка", "kind": "change", "text": "docker builder prune -af"}, follow_redirects=False)
        h = {"X-API-Key": raw}
        p = c.get("/api/v1/ops/pending", headers=h).json()
        assert [t["title"] for t in p["tasks"]] == ["Диск"] and "kind=check" in p["rules"]      # зміна чекає погодження
        tid = p["tasks"][0]["id"]
        assert c.get("/api/v1/ops/pending", headers=h).json()["tasks"] == []
        r = c.post(f"/api/v1/ops/{tid}/result", headers=h, json={"output": "Avail 11G\nTELEGRAM_BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz0123456789", "exit_code": 0})
        assert r.json()["status"] == "done"
        j = c.get(f"/ops/{tid}.json").json()
        assert "11G" in j["output"] and "abcdefghijklmnop" not in j["output"]
        assert c.post(f"/api/v1/ops/{tid}/result", headers=h, json={"output": "x"}).status_code == 409
        with SessionLocal() as db:
            ch = db.query(OpsTask).filter_by(title="Чистка").one()
            assert ch.status == "approval"
            assert ops_tasks.decide(db, ch, True, None)
        assert [t["title"] for t in c.get("/api/v1/ops/pending", headers=h).json()["tasks"]] == ["Чистка"]
        assert "Завдання для OpenClaw" in c.get("/ops").text
        assert c.get("/api/v1/invoices/pending", headers=h).status_code == 403


def test_redact():
    from app.services.ops_tasks import redact
    s = redact("API_KEY=sk-abc123456789012345678 password: hunter2 ok mcrm_AbCdEfGhIjKlMnOpQrSt")
    assert "hunter2" not in s and "sk-abc" not in s and "mcrm_AbCd" not in s and "ok" in s
