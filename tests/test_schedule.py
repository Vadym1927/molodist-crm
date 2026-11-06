from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, StaffShift


def test_schedule():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        d = Doctor(name="Графікова Лікарка", active=True, position="Косметолог")
        db.add(d); db.commit(); did = d.id
    mon = date.today() - timedelta(days=date.today().weekday())
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Графікова Лікарка" in c.get("/specialists/schedule").text
        c.post("/specialists/schedule/fill", data={"doctor_id": str(did), "d_from": str(mon), "d_to": str(mon + timedelta(days=6)),
               "mode": "weekdays", "wd": ["0", "1", "2", "3", "4"], "start": "10:00", "end": "19:00", "place": "Кабінет 1 (косметолог)", "overwrite": "1"})
        with SessionLocal() as db:
            assert db.query(StaffShift).filter_by(doctor_id=did).count() == 5
        c.post("/specialists/schedule/cell", data={"doctor_id": str(did), "day": str(mon), "off": "1"})
        c.post("/specialists/schedule/cell", data={"doctor_id": str(did), "day": str(mon + timedelta(days=5)), "start": "11:00", "end": "15:00"})
        bad = c.post("/specialists/schedule/cell", data={"doctor_id": str(did), "day": str(mon), "start": "18:00", "end": "09:00"}, follow_redirects=False)
        assert "err=" in bad.headers["location"]
        c.post("/specialists/schedule/copy", data={"src": str(mon), "weeks": "2"})
        with SessionLocal() as db:
            assert db.query(StaffShift).filter_by(doctor_id=did).count() == 15       # 5 змін × 3 тижні
        c.post("/specialists/schedule/fill", data={"doctor_id": str(did), "d_from": str(mon + timedelta(days=21)), "d_to": str(mon + timedelta(days=28)),
               "mode": "2/2", "start": "09:00", "end": "21:00"})
        with SessionLocal() as db:
            days = sorted(s.day for s in db.query(StaffShift).filter(StaffShift.doctor_id == did, StaffShift.day >= mon + timedelta(days=21)))
            assert days[:4] == [mon + timedelta(days=21), mon + timedelta(days=22), mon + timedelta(days=25), mon + timedelta(days=26)]
        cal = c.get(f"/calendar?view=day&start={mon + timedelta(days=1)}").text
        assert "10:00–19:00" in cal and "offsh" in cal
        assert c.get("/calendar?view=day").status_code == 200
        r = c.post("/specialists/schedule/clear", data={"doctor_id": str(did), "d_from": str(mon), "d_to": str(mon + timedelta(days=40)), "del_password": "x"}, follow_redirects=False)
        assert "delerr=" in r.headers["location"]


def test_in_schedule_toggle():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        a = Doctor(name="Видима Графік", active=True, in_calendar=True)
        b = Doctor(name="Адмін Неграфік", active=True, in_calendar=False)
        db.add_all([a, b]); db.commit(); aid = a.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/specialists/schedule").text
        assert "Видима Графік" in t and "Адмін Неграфік" not in t and "Показати всіх" in t
        assert "Адмін Неграфік" in c.get("/specialists/schedule?all=1").text
        assert "Відображати в графіку співробітників" in c.get(f"/specialists/{aid}").text
        c.post(f"/specialists/{aid}", data={"name": "Видима Графік", "in_calendar": "1"})   # галочку графіка знято
        assert "Видима Графік" not in c.get("/specialists/schedule").text
        c.post(f"/specialists/{aid}", data={"name": "Видима Графік", "in_calendar": "1", "in_schedule": "1"})
        assert "Видима Графік" in c.get("/specialists/schedule").text


def test_schedule_bulk_panel():
    """Бокова панель «Розклад»: групова зміна клітинок (робочий час / неробочий день) через JSON."""
    from app.models import DayOff
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        d = Doctor(name="Балк Лікарка", active=True)
        db.add(d); db.commit(); did = d.id
    d1, d2 = date.today() + timedelta(days=40), date.today() + timedelta(days=41)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        j = c.post("/specialists/schedule/bulk", json={"cells": [[did, str(d1)], [did, str(d2)]], "action": "work", "start": "10:00", "end": "20:00", "place": "Кабінет 2"}).json()
        assert j.get("ok") or j.get("n") == 2, j
        with SessionLocal() as db:
            assert db.query(StaffShift).filter_by(doctor_id=did).count() == 2
        j = c.post("/specialists/schedule/bulk", json={"cells": [[did, str(d1)]], "action": "off", "kind": "vacation"}).json()
        assert not j.get("error"), j
        with SessionLocal() as db:
            assert db.query(StaffShift).filter_by(doctor_id=did).count() == 1 and db.query(DayOff).filter_by(doctor_id=did, kind="vacation").count() == 1
        j = c.post("/specialists/schedule/bulk", json={"cells": [[did, str(d2)]], "action": "work", "start": "20:00", "end": "10:00"}).json()
        assert j.get("error")


def test_favicon_and_og_tags():
    with TestClient(app) as c:
        r = c.get("/login")
        assert 'rel="icon"' in r.text and 'property="og:image"' in r.text and "/static/img/og.png" in r.text and "Робочий простір команди" in r.text
        assert c.get("/favicon.ico").status_code == 200 and c.get("/static/img/og.png").status_code == 200


def test_call_dialog_roles():
    from app.services.telephony import dialog
    d = dialog("Адміністратор: Добрий день\nПацієнт: Хочу записатись", "Оля")
    assert [x["role"] for x in d["lines"]] == ["a", "p"] and not d["guessed"]
    g = dialog("MOLODIST, слухаю вас.\nДоброго дня.\nЧим допомогти?")
    assert [x["role"] for x in g["lines"]] == ["a", "p", "a"] and g["guessed"]


def test_cosmetics_pct_big_discount_tiers():
    from app.models import SessionLocal
    from app.services import cosmetics_pct as cp
    with SessionLocal() as db:
        cp.save(db, {"full": 10, "disc": 5, "disc_from": 0, "min_price": 0, "low": 0, "base": "paid",
                     "tiers": [{"from": "20", "pct": "2"}, {"from": "30", "off": True}], "brands": {"ТестБренд": {"full": 15, "disc": 12}}})
        r = cp.Rules(db)
        rows = [{"staff": "X", "name": "Крем", "price": 1000, "qty": 1, "paid": p, "disc_pct": d} for d, p in ((0, 1000), (10, 900), (25, 750), (40, 600))]
        for x in rows:
            r.apply(x)
        assert [x["pct"] for x in rows] == [10, 5, 2, 0] and rows[2]["sum"] == 15 and "не нараховується" in rows[3]["rule"]
        assert "знижка більше 30% — не нараховується" in r.describe()
    with TestClient(app) as c:
        settings.admin_password = "owner-pass-1"
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/settings/cosmetics-pct").text
        assert "Велика знижка" in page and 'name="tier_from"' in page
        c.post("/settings/cosmetics-pct", data={"full": "10", "disc": "5", "disc_from": "0", "min_price": "1000", "low": "0", "base": "paid",
                                                "tier_from": ["20", "35"], "tier_pct": ["3", "0"], "tier_off": ["1"]})
        with SessionLocal() as db:
            assert cp.cfg(db)["tiers"] == [{"from": 20.0, "pct": 3.0}, {"from": 35.0, "pct": 0.0}]


def test_appeal_not_target_and_ignore_phone():
    from app.models import SessionLocal, Appeal
    from app.services import appeals as ap
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        d = ap.dirs(db)
        assert ap.other_status(d) == "Не цільове" and "Постачальник" in d["other_reasons"]
        a = ap.create(db, {"phone": "+380671119988", "name": "Постачальник Тест"})
        aid = a.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post(f"/appeals/{aid}/status", data={"status": "Не цільове", "deny_reason": "Постачальник", "ignore": "1"})
        with SessionLocal() as db:
            a = db.get(Appeal, aid)
            assert a.closed_at and a.deny_reason == "Постачальник" and ap.is_ignored(db, "0671119988")["reason"] == "Постачальник"
            st = ap.stats(db, [a])
            assert st["n"] == 0 and st["by_type"]["other"] == 1
        assert "Номери без звернень" in c.get("/settings/appeals").text
        c.post("/settings/appeals-ignore", data={"phone": "+380671119988", "action": "remove"})
        with SessionLocal() as db:
            assert not ap.is_ignored(db, "+380671119988")
