"""Режими роботи: автономний (без CleverBox, облік у CRM з дати) і тестовий (без фіскалізації й термінала)."""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Check, CbService, Doctor, Sale
from app.services import autonomy, fiscal, reports


def test_modes():
    settings.admin_password = "owner-pass-1"
    since = date.today()
    with SessionLocal() as db:
        d = Doctor(name="Режимна Лікарка", active=True)
        db.add_all([d, CbService(id=96001, name="Режимна послуга", price=1000)])
        db.add_all([Check(id=96101, date=datetime.combine(since - timedelta(days=1), datetime.min.time()) + timedelta(hours=12), amount=700,
                          items=[{"item_type": "service", "item_name": "До переходу", "payd_amount": 700}], payments=[]),
                    Check(id=96102, date=datetime.combine(since, datetime.min.time()) + timedelta(hours=9), amount=5000,
                          items=[{"item_type": "service", "item_name": "Після переходу (CB)", "payd_amount": 5000}], payments=[])])
        db.commit()
        did = d.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Режим роботи" in c.get("/data").text
        r = c.post("/settings/work-mode", data={"autonomous": "1", "since": since.isoformat(), "test": "1"}, follow_redirects=False)
        assert r.status_code == 303
        assert "Тестовий режим" in c.get("/").text
        # оплата карткою — без термінала й без Checkbox
        r = c.post("/sales", json={"items": [{"kind": "service", "ref_id": 96001, "name": "Режимна послуга", "qty": 1, "price": 1000, "doctor_id": did}],
                                   "payments": [{"method": "Картка (термінал)", "amount": 1000}], "fiscal_rro": 0})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        assert "ТЕСТ" in c.get(f"/sales/{sid}").text
        # обмін з CleverBox вимкнено
        from app.integrations.cleverbox import CleverBox, CleverBoxError
        with pytest.raises(CleverBoxError):
            CleverBox(token="x").call("/clients", {})
        from app.integrations import cb_webhooks
        assert cb_webhooks.handle({"event": "client.create", "data": {"client_id": 1}}).get("ignored")
    with SessionLocal() as db:
        s = db.get(Sale, sid)
        assert s.status == "paid" and s.fiscal.get("test_sale") and not s.fiscal.get("term_wait")
        with pytest.raises(fiscal.FiscalError):
            fiscal.fiscalize(db, s)
        rep = reports.build(db, "revenue", since - timedelta(days=1), since)
        assert rep.total["revenue"] == 700 + 1000            # CleverBox — до дати переходу, з неї — каса CRM
        autonomy.save(db, autonomous=False, test=False)
        assert reports.build(db, "revenue", since - timedelta(days=1), since).total["revenue"] == 5700
