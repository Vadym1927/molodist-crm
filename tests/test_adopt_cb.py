"""Візити CleverBox → записи CRM: керування (перенос, статус) і розрахунок у касі."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appointment, CbService, Doctor, Patient, Visit
from app.services import appointments as aps, calendar as cal


def test_adopt_cb_visit():
    settings.admin_password = "owner-pass-1"
    t = (datetime.now() + timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        d = Doctor(name="Олена Прийнята", active=True)
        p = Patient(full_name="Пацієнтка CleverBox", cleverbox_id=777)
        db.add_all([d, p, CbService(id=93001, name="Пілінг PRX", price=2500, time_min=45), CbService(id=93002, name="LED", price=500, time_min=15)])
        db.flush()
        db.add_all([Visit(source="cb_visit", external_id="15986-93001", patient_id=p.id, service_id=93001, service_name="Пілінг PRX",
                          master_name="Олена Прийнята", starts_at=t, ends_at=t + timedelta(minutes=45), status="booked"),
                    Visit(source="cb_visit", external_id="15986-93002", patient_id=p.id, service_id=93002, service_name="LED",
                          master_name="Олена Прийнята", starts_at=t, ends_at=t + timedelta(minutes=60), status="booked")])
        db.commit()
        vids = ",".join(str(v.id) for v in db.query(Visit).filter(Visit.source == "cb_visit").order_by(Visit.id))
        did, pid = d.id, p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get(f"/calendar/visit?v={vids}").text
        assert "Керувати в CRM" in h                                                   # без автономного режиму — кнопка
        r = c.post("/calendar/visit/adopt", data={"v": vids}).json()
        aid = r["id"]
        assert c.post("/calendar/visit/adopt", data={"v": vids}).json()["id"] == aid          # без дублів
        assert "Перенести / змінити" in c.get(f"/calendar/visit?appt={aid}").text
        assert c.get(f"/sales/new?appt={aid}").status_code == 200
    with SessionLocal() as db:
        a = db.get(Appointment, aid)
        assert a.cb_record_id == 15986 and a.doctor_id == did and a.patient_id == pid and len(a.services) == 2
        assert a.start == t and a.end == t + timedelta(minutes=60)
        from app.models import User
        owner = db.query(User).filter(User.role == "owner").first()
        ev = [e for es in cal.events(db, owner, t.date(), days=1).values() for e in es]
        assert len(ev) == 1 and ev[0].appt_id == aid                                   # лише запис CRM, без дубля CleverBox
        a.start, a.end = a.start + timedelta(days=1), a.end + timedelta(days=1)       # перенесли в CRM
        db.commit()
        assert not [e for es in cal.events(db, owner, t.date(), days=1).values() for e in es]   # перенесли — оригінал не вилазить
        assert aps.adopt_all_cb(db, datetime.combine(date.today(), datetime.min.time())) == 0


def test_auto_adopt_with_techcard():
    """Автономний режим: відкрили візит CleverBox — це вже запис CRM з витратними з техкарти."""
    from app.models import Product, TechCard, TechCardLine, VisitMaterial
    from app.services import autonomy, techcards as tc
    settings.admin_password = "owner-pass-1"
    t = (datetime.now() + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            pr = Product(name="Голка тест", name_norm="голка тест", warehouse="single", cost=3)
            p = Patient(full_name="Авто Прийнята", cleverbox_id=778)
            db.add_all([pr, p]); db.flush()
            card = TechCard(name="ТК тест", cats=list(tc.DEFAULT_CATS), lines=[TechCardLine(cat="Одноразка", product_id=pr.id, qty=2)])
            db.add(card); db.flush()
            db.add(CbService(id=93101, name="Мезотерапія", price=3000, time_min=60, techcard_id=card.id))
            db.add(Visit(source="cb_visit", external_id="16001-93101", patient_id=p.id, service_id=93101, service_name="Мезотерапія",
                         starts_at=t, ends_at=t + timedelta(hours=1), status="booked"))
            db.commit()
            autonomy.save(db, autonomous=True, since=date.today().isoformat())
            vid = db.query(Visit).filter(Visit.external_id == "16001-93101").one().id
        h = c.get(f"/calendar/visit?v={vid}").text
        assert "Перенести / змінити" in h and "Голка тест" in h
    with SessionLocal() as db:
        a = db.query(Appointment).filter(Appointment.cb_record_id == 16001).one()
        assert db.query(VisitMaterial).filter(VisitMaterial.appt_id == a.id).count() == 1


def test_adopt_resolves_service_by_name():
    """id послуги в записі CleverBox — номер рядка; техкарта знаходиться за назвою послуги."""
    from app.models import Product, TechCard, TechCardLine, VisitMaterial
    from app.services import techcards as tc
    t = (datetime.now() + timedelta(days=3)).replace(hour=13, minute=30, second=0, microsecond=0)
    with SessionLocal() as db:
        pr = Product(name="OBSERV амортизація т", name_norm="observ амортизація т", warehouse="equip", cost=50)
        p = Patient(full_name="Юля Тест", cleverbox_id=779)
        db.add_all([pr, p]); db.flush()
        card = TechCard(name="Консультація ТК", cats=list(tc.DEFAULT_CATS), lines=[TechCardLine(cat="Обладнання", product_id=pr.id, qty=1)])
        db.add(card); db.flush()
        db.add_all([CbService(id=93201, name="Консультація Beauty plan Anti Acne", price=2500, time_min=60, techcard_id=card.id),
                    CbService(id=555777, name="Інша послуга", price=100)])
        db.add(Visit(source="cb_visit", external_id="17001-555777", patient_id=p.id, service_id=555777,
                     service_name="Консультація Beauty plan Anti Acne", starts_at=t, ends_at=t + timedelta(hours=1), status="booked"))
        db.commit()
        a = aps.adopt_cb(db, 17001)
        assert a.services[0]["id"] == 93201 and a.services[0]["price"] == 2500
        assert db.query(VisitMaterial).filter(VisitMaterial.appt_id == a.id).count() == 1
        # уже прийнятий раніше з неправильним id — виправляється при відкритті / seed
        a.services = [{"id": 555777, "name": "Консультація Beauty plan Anti Acne", "price": 2500, "minutes": 60}]
        db.query(VisitMaterial).filter(VisitMaterial.appt_id == a.id).delete(); db.commit()
        tc.seed(db, a)
        assert a.services[0]["id"] == 93201 and db.query(VisitMaterial).filter(VisitMaterial.appt_id == a.id).count() == 1
