"""Онлайн-запис на сайті (/zapys): каталог, вільний час з графіка, запис, скасування, сертифікати, публікація."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appeal, Appointment, CbService, DayOff, Doctor, Patient, StaffShift
from app.services import online_booking as ob

NOW = datetime(2026, 11, 12, 15, 0)            # четвер
D1 = date(2026, 11, 13)


def _seed(db, publish=True):
    d1, d2 = Doctor(name="Онлайн Перша", active=True, in_calendar=True), Doctor(name="Онлайн Друга", active=True, in_calendar=True)
    db.add_all([d1, d2]); db.flush()
    db.add(CbService(id=9701, name="Онлайн консультація", category="Консультації", price=800, duration=30, online=True, active=True, doctor_ids=[d1.id, d2.id]))
    db.add(CbService(id=9702, name="Онлайн чистка", category="Догляд", price=1900, duration=90, online=True, active=True, doctor_ids=[d1.id]))
    db.add(CbService(id=9703, name="Не онлайн", category="Догляд", price=500, duration=30, online=False, active=True, doctor_ids=[d1.id]))
    db.add(CbService(id=9704, name="Без спеціаліста", category="Догляд", price=500, duration=30, online=True, active=True))
    for d in (d1, d2):
        db.add(StaffShift(doctor_id=d.id, day=D1, start="10:00", end="13:00", place=f"Кабінет {d.id}"))
    db.add(StaffShift(doctor_id=d1.id, day=D1 + timedelta(days=1), start="10:00", end="12:00"))
    db.add(DayOff(doctor_id=d1.id, day=D1 + timedelta(days=1), kind="vacation"))
    pt = Patient(full_name="Зайнятий Пацієнт", phone="+380500000001")
    db.add(pt); db.flush()
    db.add(Appointment(patient_id=pt.id, doctor_id=d1.id, start=datetime(2026, 11, 13, 10, 30), end=datetime(2026, 11, 13, 11, 30), status="booked",
                       services=[{"id": 9702, "name": "Онлайн чистка"}]))
    c = ob.cfg(db); c["on"] = publish; ob.save_cfg(db, c)
    db.commit(); ob.reset_cache()
    return d1.id, d2.id


def test_catalog_and_availability():
    with SessionLocal() as db:
        a, b = _seed(db)
        cat = ob.catalog(db)
        ids = {s["id"] for c in cat["cats"] for s in c["items"]}
        assert ids == {9701, 9702} and set(cat["doctor_ids"]) == {a, b}        # не онлайн і без спеціаліста — не показуються
        av = ob.availability(db, 9701, None, now=NOW)
        assert list(av) == [D1]                                               # другий день — відпустка лікаря
        slots = {s["t"]: s["docs"] for s in av[D1]}
        assert slots["10:00"] == [a, b] and slots["10:30"] == [b] and slots["11:00"] == [b] and slots["11:30"] == [a, b] and "13:00" not in slots
        assert [s["t"] for s in ob.availability(db, 9702, a, now=NOW)[D1]] == ["11:30"]        # 90 хв: лише після зайнятого запису, до кінця зміни
        assert ob.availability(db, 9703, None, now=NOW) == {}
        late = datetime(2026, 11, 13, 9, 0)                                   # запас 3 год: найближчий початок — 12:00
        assert [s["t"] for s in ob.availability(db, 9701, b, now=late)[D1]] == ["12:00", "12:30"]


def test_book_cancel_and_limits():
    with SessionLocal() as db:
        a, b = _seed(db)
        ap = ob.book(db, {"service": 9701, "doctor": 0, "day": D1.isoformat(), "time": "10:30", "name": "Нова Пацієнтка", "phone": "067 111 22 33",
                          "comment": "перший візит"}, now=NOW)
        assert ap.doctor_id == b and ap.source == "online" and ap.status == "booked" and ap.online_token and ap.place == f"Кабінет {b}"
        assert ap.patient.phone == "+380671112233" and ap.services[0]["price"] == 800 and "перший візит" in ap.comment
        assert "10:30" not in {s["t"] for s in ob.availability(db, 9701, None, now=NOW)[D1]}       # обидва лікарі тепер зайняті
        for bad, code in (({"time": "10:30"}, "taken"), ({"phone": "123"}, "phone"), ({"name": ""}, "name")):
            try:
                ob.book(db, {"service": 9701, "doctor": 0, "day": D1.isoformat(), "time": "12:00", "name": "Нова Пацієнтка", "phone": "+380671112233", **bad}, now=NOW)
                raise AssertionError(bad)
            except ob.BookingError as e:
                assert e.code == code, (bad, e.code)
        ob.book(db, {"service": 9701, "doctor": a, "day": D1.isoformat(), "time": "12:00", "name": "Нова Пацієнтка", "phone": "+380671112233"}, now=NOW)
        try:                                                                  # ліміт активних онлайн-записів з одного номера
            ob.book(db, {"service": 9701, "doctor": b, "day": D1.isoformat(), "time": "12:30", "name": "Нова Пацієнтка", "phone": "+380671112233"}, now=NOW)
            raise AssertionError("limit")
        except ob.BookingError as e:
            assert e.code == "limit"
        assert db.query(Patient).filter(Patient.phone == "+380671112233").count() == 1
        v = ob.view(db, ap)
        assert v["service"] == "Онлайн консультація" and v["time"] == "10:30" and "BEGIN:VEVENT" in ob.ics(ap, v, {"address": "Київ", "phone_text": "+380"})
        assert ob.by_token(db, ap.online_token).id == ap.id and ob.by_token(db, "нема") is None and ob.by_token(db, "x" * 12) is None
        try:
            ob.cancel(db, ap, now=datetime(2026, 11, 13, 9, 0))                # менше ніж за 3 години — лише через адміністратора
            raise AssertionError("late")
        except ob.BookingError as e:
            assert e.code == "late"
        ob.cancel(db, ap, now=NOW)
        assert ap.status == "cancelled" and "10:30" in {s["t"] for s in ob.availability(db, 9701, b, now=NOW)[D1]}


def test_site_page_api_and_publication(monkeypatch):
    sent = []
    from app.services import alerts
    monkeypatch.setattr(alerts, "send", lambda t, *a, **k: sent.append(t) or True)
    monkeypatch.setattr(ob, "now_local", lambda: NOW)
    with SessionLocal() as db:
        a, b = _seed(db, publish=False)
    with TestClient(app) as cl:
        assert cl.get("/_site/zapys").status_code == 404                      # не опубліковано — стороннім недоступно
        assert cl.get("/_site/api/booking/days?service=9701").status_code == 404
        with SessionLocal() as db:
            c = ob.cfg(db); c["on"] = True; ob.save_cfg(db, c)
        r = cl.get("/_site/zapys")
        assert r.status_code == 200 and '"id": 9701' in r.text and "Наші лікарі" in r.text and "Сертифікат" in r.text and "Гайди" in r.text
        assert "molodist.store" in r.text and "booking.js" in r.text and "клінік" not in r.text.lower().split("<main>")[1].split("</main>")[0]
        js = cl.get("/_site/api/booking/days?service=9701&doctor=0").json()
        assert js["ok"] and js["days"][0]["d"] == D1.isoformat() and js["days"][0]["slots"][0] == {"t": "10:00", "docs": [a, b]}
        body = {"service": 9701, "doctor": b, "day": D1.isoformat(), "time": "11:00", "name": "Ірина Сайт", "phone": "+380 67 222 33 44",
                "gclid": "Cj0BOOK", "utm_source": "google", "t": 20}
        assert cl.post("/_site/api/booking", json={**body, "website": "bot"}).status_code == 429          # приховане поле — бот
        r = cl.post("/_site/api/booking", json=body)
        assert r.status_code == 200 and r.json()["ok"] and r.json()["time"] == "11:00" and r.json()["doctor"]
        tok = r.json()["token"]
        r2 = cl.post("/_site/api/booking", json=body)
        assert r2.status_code == 400 and r2.json()["error"] == "taken"
        assert "Ваш запис" in cl.get(f"/_site/zapys?v={tok}").text and cl.get(f"/_site/zapys.ics?v={tok}").text.startswith("BEGIN:VCALENDAR")
        assert "Запис не знайдено" in cl.get("/_site/zapys?v=abcdefghijkl").text
        assert any("Новий онлайн-запис" in t and "Ірина Сайт" in t for t in sent)
        with SessionLocal() as db:
            ap = ob.by_token(db, tok)
            al = db.get(Appeal, ap.appeal_id)
            assert al and "gclid=Cj0BOOK" in (al.utm or "") and al.status == "Записаний" and al.interest == "Онлайн консультація"
        r = cl.post("/_site/api/booking/cancel", json={"token": tok})
        assert r.json()["ok"] and r.json()["status"] == "cancelled" and any("скасовано" in t for t in sent)
        # сертифікат: заявка-звернення (платіжну систему не підключено)
        r = cl.post("/_site/api/order", json={"kind": "cert", "id": 2, "name": "Дарувальник", "phone": "0672223355", "to": "Мамі", "t": 15})
        assert r.json()["ok"] and r.json()["price"] == 3000 and not r.json().get("pay_url")
        with SessionLocal() as db:
            al = db.query(Appeal).filter(Appeal.phone == "+380672223355").one()
            assert "Сертифікат 3000" in al.interest and "Мамі" in al.comment
        assert cl.post("/_site/api/order", json={"kind": "cert", "id": 999, "name": "X Y", "phone": "0672223355", "t": 15}).status_code == 400
        # сайт: кнопки ведуть на онлайн-запис, сторінка в sitemap
        from app.services import site_pages as sp
        with SessionLocal() as db:
            c = sp.cfg(db); c["hosts"] = ["new.molodist.test"]; sp.save_cfg(db, c)
        h = {"host": "new.molodist.test"}
        assert "/zapys" in cl.get("/sitemap.xml", headers=h).text and cl.get("/zapys", headers=h).status_code == 200
        with SessionLocal() as db:
            c = sp.cfg(db); c["hosts"] = ["new.molodist.com.ua"]; sp.save_cfg(db, c)


def test_settings_online():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        a, b = _seed(db, publish=False)
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = cl.get("/settings/online-booking")
        assert r.status_code == 200 and "Онлайн консультація" in r.text and "Не опубліковано" in r.text and "Без спеціаліста" in r.text
        assert cl.get("/_site/zapys").status_code == 200                      # співробітник бачить неопубліковану сторінку
        assert cl.post("/settings/online-booking/services", data={"shown": ["9701", "9702", "9703"], "on": ["9701", "9703"]},
                       follow_redirects=False).status_code == 303
        assert cl.post("/settings/online-booking", data={"on": "1", "step": "20", "lead_h": "1", "days": "14", "cancel_h": "2", "max_active": "3",
                                                         "doctor": [str(a)], "shop_url": "https://molodist.store/shop/", "certs_on": "1",
                                                         "g_title_0": "Мій гайд", "g_text_0": "Раз\nДва", "g_on_0": "1", "faq": "Питання? | Відповідь."},
                       follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.get(CbService, 9702).online is False and db.get(CbService, 9703).online is True
        c = ob.cfg(db)
        assert c["on"] and c["step"] == 20 and c["days"] == 14 and c["doctors_off"] == [b] and c["guides"][0]["title"] == "Мій гайд"
        assert c["faq"] == [["Питання?", "Відповідь."]] and not c["subs_on"]
        assert set(ob.catalog(db)["doctor_ids"]) == {a}
