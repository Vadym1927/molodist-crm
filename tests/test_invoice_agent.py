from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, ApiKey, InvoiceJob


def test_invoice_agent_flow(tmp_path):
    from app.security import new_api_key
    from app.services import invoice_ai
    settings.admin_password = "owner-pass-1"
    old_dir, settings.files_dir = settings.files_dir, str(tmp_path)
    with SessionLocal() as db:
        invoice_ai.set_engine(db, "agent")
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="openclaw-inv", prefix=prefix, key_hash=hsh, scopes=["invoices"])); db.commit()
    try:
        with TestClient(app) as c:
            c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
            r = c.post("/stock/recognize", files={"file": ("nakl.jpg", b"\xff\xd8\xff fake jpeg", "image/jpeg")}).json()
            jid = r["queued"]
            assert c.get(f"/stock/recognize/{jid}").json()["status"] == "pending"
            h = {"X-API-Key": raw}
            p = c.get("/api/v1/invoices/pending", headers=h).json()
            assert p["invoices"][0]["id"] == jid and "постачальника" in p["prompt"]
            f = c.get(f"/api/v1/invoices/{jid}/file", headers=h)
            assert f.status_code == 200 and f.content.startswith(b"\xff\xd8")
            assert c.post(f"/api/v1/invoices/{jid}/result", headers=h, json={"result": {"lines": []}}).status_code == 422
            ok = c.post(f"/api/v1/invoices/{jid}/result", headers=h, json={"result": {
                "supplier": {"name": "ТОВ Косметик"}, "number": "Н-17", "date": "03.11.2026", "total": "1 200,50",
                "lines": [{"name": "Крем тест", "qty": "2", "price": "600,25", "sum": 1200.5}]}}).json()
            assert ok["status"] == "done"
            res = c.get(f"/stock/recognize/{jid}").json()
            assert res["source"] == "agent" and res["number"] == "Н-17" and res["lines"][0]["qty"] == 2 and res["date"] == "2026-11-03"
            assert c.get("/api/v1/invoices/pending", headers=h).json()["invoices"] == []
            assert c.get("/api/v1/calls/pending-ai", headers=h).status_code == 403
    finally:
        settings.files_dir = old_dir
        with SessionLocal() as db:
            invoice_ai.set_engine(db, "crm")
