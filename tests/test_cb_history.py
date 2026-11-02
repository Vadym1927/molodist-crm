from datetime import datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appointment, Doctor, Patient, Product, VisitMaterial
from app.services import cb_history, techcards


def test_cb_history_import():
    with SessionLocal() as db:
        p = Patient(full_name="Історія Пацієнтка", cleverbox_id=771419); d1 = Doctor(name="Лікар Історія", cleverbox_id=7712)
        d2 = Doctor(name="Лікар Друга", cleverbox_id=7713)
        pr = Product(name="Маска тест", name_norm="маска тест", cb_product_id=77346, warehouse="single")
        db.add_all([p, d1, d2, pr]); db.commit(); pid = p.id
        v = {"rid": 778139, "cid": 771419, "start": "31.10.2024 16:00", "stt": "Візит завершений", "st": "3", "author": "Рокун Вікторія",
             "added": 1730203886, "total": 2500, "disc": 0, "paid": 2500, "check": 6794,
             "items": [{"row": 13799, "sid": 5, "t": "Гігієна Обличчя Demax", "p": 1500, "s": 1500, "q": 1, "m": 7712, "mn": "Лікар Історія", "min": 90,
                        "mats": [[77346, 3, 2.92, "Маска"], [999999, 1, 1, "Невідомий"]]},
                       {"row": 13800, "sid": 6, "t": "LED", "p": 1000, "s": 1000, "q": 1, "m": 7713, "mn": "Лікар Друга", "min": 30, "mats": []}]}
        r = cb_history.import_visits(db, [v, dict(v, rid=778140, start="05.11.2026 10:00")], until=datetime(2026, 11, 1))
        assert r["appts_new"] == 2 and r["materials"] == 1 and r["materials_unknown"] == 1 and r["skipped_after"] == 1
        aps = db.query(Appointment).filter(Appointment.cb_record_id == 778139).order_by(Appointment.start).all()
        assert len(aps) == 2 and aps[0].status == "done" and aps[0].cb_paid == 1500 and aps[1].start == datetime(2024, 10, 31, 17, 30)
        assert aps[0].historic and techcards.closed(db, aps[0]) and not techcards.seed(db, aps[0])
        assert db.query(VisitMaterial).filter(VisitMaterial.appt_id == aps[0].id).one().qty == 3
        r2 = cb_history.import_visits(db, [v], until=datetime(2026, 11, 1))              # повторно — без дублів
        assert r2["appts_upd"] == 2 and db.query(Appointment).filter(Appointment.cb_record_id == 778139).count() == 2
        aid = aps[0].id
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get(f"/calendar/visit?appt={aid}").text
        assert "Історія CleverBox" in h and "1500.00" in h and "Маска тест" in h
