"""PDF «Імʼя Прізвище» ↔ CleverBox «Прізвище Імʼя По батькові»; об'єднання дубля; поля картки CleverBox."""
from datetime import datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, BeautyPlan, PatientFile
from app.services import patient_match as pm
from app.services.plans import find_patient
from app.integrations.cleverbox import apply_client_fields


def test_score_and_find():
    assert pm.score("Ірина Фабаровська", "Фабаровська Ірина Олександрівна") == 90
    assert pm.score("Ірина Фабаровська", "Фабаровська Ірина") == 100
    assert 60 < pm.score("Ірина Фабаровська", "Фаборовська Ірина Олександрівна") < 90
    assert pm.score("Ірина Фабаровська", "Ірина Лисенко Миколаївна") == 0
    with SessionLocal() as db:
        cb = Patient(full_name="Збігова Мирослава Олексіївна", phone="380501112299", cleverbox_id=8800111)
        db.add(cb); db.commit()
        assert find_patient(db, "Мирослава Збігова").id == cb.id          # без по батькові — та сама
        db.add(Patient(full_name="Збігова Мирослава Петрівна")); db.commit()
        assert find_patient(db, "Мирослава Збігова").id == cb.id          # дві тезки — перевага картці CleverBox


def test_merge_duplicate_from_pdf():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        real = Patient(full_name="Дублева Олена Іванівна", phone="380671234500", cleverbox_id=8800222)
        stub = Patient(full_name="Олена Дублєва")                         # з PDF, одруківка
        db.add_all([real, stub]); db.flush()
        db.add(BeautyPlan(patient_id=stub.id, title="План з PDF", session_at=datetime(2026, 10, 24), total_planned=21450))
        db.add(PatientFile(patient_id=stub.id, kind="plan_pdf", path="/tmp/x.pdf"))
        db.commit()
        assert real.id in [p.id for _, p in pm.suggestions(db, stub)]
        rid, sid = real.id, stub.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Схоже, ця пацієнтка вже є" in c.get(f"/patients/{sid}").text
        r = c.post(f"/patients/{sid}/merge", data={"target": rid}, follow_redirects=False)
        assert r.headers["location"] == f"/patients/{rid}?merged=1"
    with SessionLocal() as db:
        assert db.get(Patient, sid) is None
        assert db.query(BeautyPlan).filter_by(patient_id=rid, title="План з PDF").count() == 1
        assert db.query(PatientFile).filter_by(patient_id=rid).count() == 1


def test_client_fields_from_cleverbox():
    p = Patient(full_name="Поля Тест")
    apply_client_fields(p, {"id": 5, "name": "Поля Тест", "phone": "0671112233", "sex": "female",
                            "source": "Instagram", "groups": [{"name": "VIP"}, {"name": "Ботулінотерапія"}],
                            "balance": "1500", "bonus": 200, "card_number": "A-77", "comment": "Любить ранок",
                            "discount": 10})
    assert p.phone == "0671112233" and p.gender == "жіноча" and p.source == "Instagram"
    assert p.client_groups == ["VIP", "Ботулінотерапія"] and p.cb_balance == 1500 and p.cb_bonus == 200
    assert p.cb_extra["card_number"] == "A-77" and p.cb_extra["discount"] == 10 and "phone" not in p.cb_extra


def test_sync_links_orphan_card():
    from app.integrations.cleverbox import sync_clients

    class FakeCB:
        def clients(self):
            return [{"id": 8800333, "name": "Привʼязна Ганна Степанівна", "phone": "380639998877", "source": "Google"}]

    with SessionLocal() as db:
        o = Patient(full_name="Ганна Привʼязна"); db.add(o); db.commit()
        sync_clients(db, FakeCB())
        db.refresh(o)
        assert o.cleverbox_id == 8800333 and o.phone == "380639998877" and o.source == "Google"
