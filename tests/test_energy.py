"""Звіт Deye: ціна WOG, розрахунок економії й окупності, імпорт, доступ лише власнику."""
from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, EnergyDay, FuelPrice
from app.services import energy as en

WOG = {"data": {"fuel_filters": [{"price": 10690, "brand": "Євро5", "name": "ДП", "id": 9},
                                 {"price": 9290, "brand": "Євро5-Е10", "name": "95", "id": 2},
                                 {"price": 9590, "brand": "Mustang Євро5-Е10", "name": "95", "id": 5}]}}
DATA = {"assumptions": {"generator_litres_per_battery_hour": 1, "station_cost_uah": 241541.98}, "tariffs": {"2026-10": 13.16},
        "days": [{"day": "2026-10-06", "discharge_kwh": 5.9, "charge_kwh": 4.42, "battery_hours": 3.0, "business_hours": 3.0, "fuel_uah_per_litre": 92.9},
                 {"day": "2026-10-10", "discharge_kwh": 1.3, "charge_kwh": 2.0, "battery_hours": 1.5, "business_hours": 0.0, "fuel_uah_per_litre": 92.9}]}


def test_wog_parse_and_calc():
    assert en.parse_wog(WOG) == 92.9
    with SessionLocal() as db:
        assert en.import_json(db, DATA) == 2
        rs = {r["day"]: r for r in en.rows(db)}
        r = rs[date(2026, 10, 6)]
        assert r["gen_cost"] == 278.7 and r["charge_cost"] == 58.17 and r["savings"] == 220.53
        assert rs[date(2026, 10, 10)]["savings"] == -26.32
        pb = en.payback(db, en.cfg(db), list(rs.values()))
        assert pb["cost"] == 241541.98 and pb["total"] == round(220.53 - 26.32, 2)


def test_frames_hours():
    from datetime import datetime
    base = datetime(2026, 11, 1, 8, 0).timestamp()
    fr = [{"timeStamp": base + i * 300, "purchasePower": 0 if 12 <= i < 60 else 500, "consumptionPower": 800} for i in range(80)]
    tot, work = en.hours_from_frames(fr, 9, 21)
    assert tot == 4.0 and work == 4.0


def test_page_owner_only():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            if not db.get(FuelPrice, date.today()):
                db.add(FuelPrice(day=date.today(), price=92.9, source="test")); db.commit()
        for q in ("", "?p=all", "?p=day&d=2026-10-06", "?p=month&m=2026-10"):
            r = c.get("/deye" + q); assert r.status_code == 200 and "Окупність станції" in r.text, q
        r = c.post("/deye/day", data={"day": "2026-10-07", "charge_kwh": "3,68", "business_hours": "3", "discharge_kwh": "2.6", "battery_hours": "3"},
                   follow_redirects=False)
        assert r.status_code == 303
        r = c.post("/deye/tariff", data={"month": "2026-11", "value": "13,16"}, follow_redirects=False); assert r.status_code == 303
        r = c.post("/deye/settings", data={"app_id": "x1", "app_secret": "s", "email": "e@x", "password": "p"}, follow_redirects=False)
        assert r.status_code == 303
        assert "Deye" in c.get("/reports").text
    with SessionLocal() as db:
        assert db.get(EnergyDay, date(2026, 10, 7)).source == "manual"
        cf = en.cfg(db)
        assert en.deye_ready(cf) and en.secret(cf, "password") == __import__("hashlib").sha256(b"p").hexdigest()
        assert en.cfg(db)["tariffs"]["2026-11"] == 13.16
