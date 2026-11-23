from datetime import date, datetime, timedelta

from app.models import Appeal, SessionLocal, SiteClick
from app.services import google_ads as ga, site_leads as sl


def test_not_target_appeals_are_not_leads_and_known_contacts_not_attributed():
    with SessionLocal() as db:
        now = datetime.utcnow()
        db.add(Appeal(name="Постачальник Тест", source="Telegram", status="Не цільове", utm="utm_source=google&utm_medium=cpc&gclid=x1", created_at=now))
        db.add(Appeal(name="Справжній Лід", source="Сайт · форма", status="Новий", utm="utm_source=google&utm_medium=cpc&gclid=x2", created_at=now))
        db.commit()
        names = {r.get("phone") or r["appeal_id"] for r in ga.leads(db, date.today() - timedelta(days=1), date.today() + timedelta(days=1))}
        ids = {r["appeal_id"] for r in ga.leads(db, date.today() - timedelta(days=1), date.today() + timedelta(days=1))}
        nt = db.query(Appeal).filter_by(name="Постачальник Тест").one().id
        ok = db.query(Appeal).filter_by(name="Справжній Лід").one().id
        assert ok in ids and nt not in ids and names
        # постійний контакт у Telegram: писав тиждень тому → новий клік з реклами його не «забирає»
        db.add(Appeal(name="Постійний Контакт", source="Telegram", status="Новий", created_at=now - timedelta(days=7)))
        a = Appeal(name="Постійний Контакт", source="Telegram", status="Новий", created_at=now)
        db.add(a); db.commit()
        assert sl._known_contact(db, a)
        b = Appeal(name="Новий Контакт", source="Telegram", status="Новий", created_at=now)
        db.add(b); db.commit()
        assert not sl._known_contact(db, b)
