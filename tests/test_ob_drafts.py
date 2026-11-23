from datetime import datetime, timedelta

from app.models import Appeal, SessionLocal
from app.services import online_booking as ob


def test_unfinished_booking_becomes_appeal_after_wait():
    with SessionLocal() as db:
        assert ob.save_draft(db, {"phone": "+380 67 555 12 34", "name": "Незавершена Ніна", "services": "", "gclid": "gx1",
                                  "utm_source": "google", "utm_medium": "cpc", "day": "2026-11-25", "time": "12:00"})
        assert not ob.save_draft(db, {"phone": "123"})
        assert ob.process_drafts(db) == 0                                  # ще не минуло 20 хв
        assert ob.process_drafts(db, now=datetime.utcnow() + timedelta(minutes=25)) == 1
        a = db.query(Appeal).filter(Appeal.phone.like("%5551234")).order_by(Appeal.id.desc()).first()
        assert a and "не завершив" in a.comment and "gclid=gx1" in (a.utm or "")
        assert ob.process_drafts(db, now=datetime.utcnow() + timedelta(minutes=50)) == 0   # чернетку прибрано


def test_finished_booking_drops_draft():
    with SessionLocal() as db:
        ob.save_draft(db, {"phone": "+380675559999", "name": "Завершила"})
        ob.drop_draft(db, "+380675559999")
        assert ob.process_drafts(db, now=datetime.utcnow() + timedelta(minutes=25)) == 0
