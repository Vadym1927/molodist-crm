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
