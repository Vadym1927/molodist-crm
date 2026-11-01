from datetime import datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Check, CbService, Doctor, Patient, Product


def test_coverage_page():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add_all([Check(id=95001, date=datetime(2023, 3, 5, 12), amount=1000, items=[{"item_type": "service", "payd_amount": 0}], payments=[]),
                    Check(id=95002, date=datetime(2023, 6, 5, 12), amount=2000, items=[], payments=[]),
                    Patient(full_name="Дубль Один", phone="+380501112233"), Patient(full_name="Дубль Два", phone="0501112233"),
                    CbService(id=95101, name="Без техкарти", price=0), Doctor(name="Без графіку", active=True),
                    Product(name="Апарат IPL аморт", name_norm="апарат ipl аморт", warehouse="equip")])
        db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get("/data/coverage").text
        assert "Повнота даних" in h and "Чеки CleverBox" in h and "04.2023" in h          # дірки між березнем і червнем
        assert "Дублікати" in h and "Дубль Один" in h and "Без техкарти" in h and "Обладнання (амортизація) без залишку" in h
        assert "Без графіку" in h
        assert "/data/coverage" in c.get("/data").text
