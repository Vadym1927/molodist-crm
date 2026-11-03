from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient
from app.services import search


def test_match_any_order():
    assert search.match("Вероніка Дубовик Володимирівна", "Дубовик Вероніка Володимирівна")
    assert search.match("дуб вер", "Дубовик Вероніка Володимирівна")
    assert search.match("Дарья", "Ковган Дарʼя") is False
    assert search.match("Дарʼя Ковган", "Ковган Дар'я")
    assert not search.match("Дубовик Олена", "Дубовик Вероніка")
    assert search.phone_match("098 123 45 67", "+380981234567")


def test_lookup_and_clients_any_order():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add(Patient(full_name="Дубовик Вероніка Володимирівна", phone="+380981112233")); db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        js = c.get("/lookup/patients", params={"q": "Вероніка Дубовик Володимирівна"}).json()
        assert js and js[0]["name"] == "Дубовик Вероніка Володимирівна"
        assert c.get("/lookup/patients", params={"q": "вероніка дуб"}).json()
        assert "Дубовик Вероніка" in c.get("/clients", params={"q": "Вероніка Дубовик"}).text
