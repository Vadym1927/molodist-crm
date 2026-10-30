from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor
from app.services import positions as ps


def test_positions_settings_and_specialist_select():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            d = Doctor(name="Посадова Олена", position="Адміністратор"); db.add(d); db.commit(); did = d.id
        r = c.get("/settings/positions")
        assert r.status_code == 200 and "Лікар-косметолог" in r.text
        c.post("/settings/positions", data={"name": "Нутриціолог"})
        with SessionLocal() as db:
            xs = ps.items(db)
            assert "Нутриціолог" in [x["name"] for x in xs]
            adm = next(x for x in xs if x["name"] == "Адміністратор")
        c.post("/settings/positions", data={"pid": str(adm["id"]), "name": "Адміністратор рецепції"})
        with SessionLocal() as db:
            assert db.get(Doctor, did).position == "Адміністратор рецепції"
            ids = [x["id"] for x in ps.items(db)][::-1]
        c.post("/settings/positions/order", json={"ids": ids})
        with SessionLocal() as db:
            assert [x["id"] for x in ps.items(db)] == ids
        html = c.get(f"/specialists/{did}").text
        assert "<option selected>Адміністратор рецепції</option>" in html and "<option >Нутриціолог</option>" in html


def test_expiry_text_parsing():
    from datetime import date
    from app.main import _exp_day
    assert _exp_day("31.12.2027") == date(2027, 12, 31) == _exp_day("12/27") == _exp_day("12.2027") == _exp_day("2027-12-31")
    assert _exp_day("02.2028") == date(2028, 2, 29) and _exp_day("31.02.2027") is None and _exp_day("") is None
