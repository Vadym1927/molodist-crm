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
        assert a and "НЕЗАВЕРШЕНИЙ ОНЛАЙН-ЗАПИС" in a.comment and "gclid=gx1" in (a.utm or "")
        assert ob.process_drafts(db, now=datetime.utcnow() + timedelta(minutes=50)) == 0   # чернетку прибрано


def test_finished_booking_drops_draft():
    with SessionLocal() as db:
        ob.save_draft(db, {"phone": "+380675559999", "name": "Завершила"})
        ob.drop_draft(db, "+380675559999")
        assert ob.process_drafts(db, now=datetime.utcnow() + timedelta(minutes=25)) == 0


def test_booking_funnel_counts_steps():
    from datetime import date
    from app.models import SiteEvent
    from app.services import site_stats as ss
    with SessionLocal() as db:
        now = datetime.now()
        for sid, steps in (("fa1", ["krok-2", "krok-3"]), ("fa2", ["krok-2", "krok-3", "krok-4", "krok-tel", "krok-5"]), ("fa3", [])):
            db.add(SiteEvent(at=now, sid=sid, page="/onlain-zapys", kind="view", paid=True))
            for k in steps:
                db.add(SiteEvent(at=now, sid=sid, page="/onlain-zapys", kind="sec", target=k, paid=True))
        db.commit()
        f = {x["key"]: x["n"] for x in ss.booking_funnel(db, date.today(), date.today(), paid=True)}
        assert f["open"] >= 3 and f["krok-3"] >= 2 and f["krok-5"] >= 1 and f["krok-tel"] >= 1


def test_draft_alert_says_not_a_lead():
    from app.services import site_leads as sl
    t = sl.alert_text({"formname": "onlain-zapys:draft", "name": "Тест", "phone": "+380671112233", "interest": "Консультація",
                       "want": "Консультація · 25.11 12:00"}, 5, "https://x")
    assert "Незавершений онлайн-запис" in t and "Нова заявка" not in t and "25.11 12:00" in t
