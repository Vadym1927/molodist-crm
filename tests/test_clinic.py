from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal
from app.services import clinic as cl


def test_clinic_settings():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/settings/clinic").text
        assert "Molodist_" in t and "Стратосфера 2" in t and "Кабінет 3 (масаж)" in t and "Внесення коштів в касу" in t
        c.post("/settings/clinic/general", data={"country": "Україна", "timezone": "Europe/Kyiv", "language": "Українська", "currency": "UAH", "vat": "1"})
        form = {"on_0": "1", "from_0": "09:00", "to_0": "21:00"}
        for i in range(1, 7):
            form.update({f"on_{i}": "1", f"from_{i}": "10:00", f"to_{i}": "20:00"})
        form.pop("on_6")
        c.post("/settings/clinic/location/114/hours", data=form)
        c.post("/settings/clinic/location/114/resources", data={"action": "add", "name": "Лазер 2"})
        c.post("/settings/clinic/location/114/places", data={"action": "toggle", "idx": "2"})
        r = c.post("/settings/clinic/location/114/resources", data={"action": "delete", "idx": "0", "del_password": "x"}, follow_redirects=False)
        assert "delerr=" in r.headers["location"]                                # без пароля видалення — ні
        c.post("/settings/clinic/reports", data={"exclude_income": ["Внесення коштів в касу"], "extra": "Повернення від постачальника"})
        assert "Загальні" in c.get("/settings").text and 'href="/settings/clinic"' in c.get("/settings").text
    with SessionLocal() as db:
        d = cl.get(db)
        loc = cl.location(d, 114)
        assert d["general"]["vat"] and loc["resources"][-1]["name"] == "Лазер 2" and loc["resources"][0]["name"] == "IPL"
        assert loc["places"][2]["active"] is False and not loc["hours"]["6"]["on"]
        monday = date(2026, 11, 2)
        assert cl.hours_for(db, monday) == (9, 21) and cl.hours_for(db, date(2026, 11, 8)) is None
        assert cl.excluded_income(db) == {"внесення коштів в касу", "повернення від постачальника"}
        db.query(__import__("app.models", fromlist=["SyncState"]).SyncState).filter_by(key="clinic_settings").delete(); db.commit()
