from app.models import SessionLocal, Patient
from app.services import name_order as no


def test_name_order():
    assert no.suggest("Вадим Бондар Миколайович") == "Бондар Вадим Миколайович"
    assert no.suggest("Бондар Вадим Миколайович") is None and no.suggest("Юля Юля") is None
    assert no.suggest("Дар'я Колган Олександрівна") == "Колган Дар'я Олександрівна"
    with SessionLocal() as db:
        p = Patient(full_name="Анна Тестенко Олександрівна"); db.add(p); db.commit()
        assert no.apply(db, {p.id}) == 1
        assert db.get(Patient, p.id).full_name == "Тестенко Анна Олександрівна"


def test_patient_edit_split_fio():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, Patient
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Колган Дар'я Олександрівна", phone="380634998912"); db.add(p); db.commit(); pid = p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get(f"/patients/{pid}").text
        assert 'name="last_name" value="Колган"' in h and 'name="middle_name" value="Олександрівна"' in h
        c.post(f"/patients/{pid}", data={"last_name": "Колган", "first_name": "Дарʼя", "middle_name": "Олександрівна", "phone": "380634998912",
                                          "instagram": "@dr.darya", "birthday": "1990-05-01"})
    with SessionLocal() as db:
        p = db.get(Patient, pid)
        assert p.full_name == "Колган Дарʼя Олександрівна" and p.instagram == "@dr.darya" and str(p.birthday) == "1990-05-01"
