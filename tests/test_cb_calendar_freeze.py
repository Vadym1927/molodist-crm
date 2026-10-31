from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, Visit, SyncState
from app.integrations import cleverbox as cbm
from app.services import calendar as cal


class _NoCB:
    def visits_on(self, day):
        raise AssertionError("календар не має тягнутися з CleverBox")

    def client_future_visits(self, cid):
        raise AssertionError("майбутні записи не мають тягнутися")


def test_calendar_freeze():
    settings.admin_password = "owner-pass-1"
    day = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0) + timedelta(days=1)
    with TestClient(app) as c:                                   # старт застосунку — календар заморожено один раз
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            assert db.get(SyncState, "cb_calendar_freeze_20261031") and cbm.calendar_frozen(db)
            p = Patient(full_name="Заморожена Пацієнтка"); db.add(p); db.flush()
            cbm.set_calendar_frozen(db, False)
            old = Visit(source="cb_visit", external_id="555-1", patient_id=p.id, starts_at=day, status="booked", service_name="Стара")
            db.add(old); db.commit()
            cbm.set_calendar_frozen(db, True)                    # вимкнули: старий запис лишається
            new = Visit(source="cb_service_sale", external_id="556-1", patient_id=p.id, starts_at=day + timedelta(hours=3),
                        status="done", service_name="Нова з CleverBox")
            db.add(new); db.commit()
            assert cbm.sync_calendar(db, _NoCB(), 14) == 0 and cbm.sync_future_visits(db, _NoCB()) == 0
            from app.models import User
            u = db.query(User).filter_by(role="owner").first()
            evs = [e.subtitle or e.service for e in cal.events(db, u, day.date(), 1)[day.date()]]
            assert any("Стара" in (x or "") for x in evs) and not any("Нова з CleverBox" in (x or "") for x in evs)
        assert "не синхронізується" in c.get("/data").text
        c.post("/settings/cleverbox-calendar", data={"on": "1"})
        with SessionLocal() as db:
            assert not cbm.calendar_frozen(db)
        c.post("/settings/cleverbox-calendar", data={"on": "0"})
