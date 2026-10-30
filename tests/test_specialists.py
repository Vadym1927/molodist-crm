"""Спеціалісти: синк з CleverBox (працює / звільнений), картки, колонки календаря."""
from datetime import datetime, date, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.integrations import cleverbox as cbm
from app.main import app
from app.models import SessionLocal, Doctor, Patient, Visit
from app.services import calendar as cal_svc
from app.services.calendar import Event


class FakeCB:
    def staff(self):
        return [{"id": 5, "name": "Дарʼя", "lname": "Колган", "email": "d@m.ua", "img": "https://x/y.jpg"},
                {"id": 6, "name": "Ірина", "lname": "Мельник", "email": ""},
                {"id": 7, "name": "Олег", "lname": "Старий", "fired": 1}]


def test_staff_sync_and_pages():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Тест Пацієнтка", cleverbox_id=4242)
        db.add(p); db.flush()
        soon = datetime.now() + timedelta(days=1)
        db.add_all([Visit(source="cb_calendar", external_id="s1", patient_id=p.id, master_name="Дарʼя Колган",
                          starts_at=soon, status="booked", service_name="IPL"),
                    Visit(source="cb_service_sale", external_id="s2", patient_id=p.id, master_name="Анна Пішла",
                          starts_at=datetime.now() - timedelta(days=300), status="done", amount=1000)])
        db.commit()
        cbm.sync_staff(db, FakeCB())
        docs = {d.name: d for d in db.query(Doctor).all()}
        assert docs["Дарʼя Колган"].active and docs["Дарʼя Колган"].photo_url == "https://x/y.jpg"
        assert docs["Ірина Мельник"].active
        assert not docs["Олег Старий"].active                    # позначений звільненим у CleverBox
        assert not docs["Анна Пішла"].active                     # є в історії, немає серед співробітників
        darya = docs["Дарʼя Колган"].id
        # ручний статус не перезаписується синком
        docs["Ірина Мельник"].active, docs["Ірина Мельник"].status_locked = False, True
        db.commit()
        cbm.sync_staff(db, FakeCB())
        assert not db.get(Doctor, docs["Ірина Мельник"].id).active

        ev = {date.today(): [Event("visit", datetime.now(), "А", None, 1, doctor_id=darya),
                             Event("task", datetime.now(), "Т", None, 1, all_day=True)]}
        week = cal_svc.columns(db, ev, fixed=False)[date.today()]
        assert [c.doctor.name if c.doctor else None for c in week] == ["Дарʼя Колган", None]
        day = cal_svc.columns(db, ev, fixed=True)[date.today()]
        assert "Дарʼя Колган" in [c.doctor.name for c in day if c.doctor]

    with TestClient(app) as c:
        assert c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"},
                      follow_redirects=False).status_code == 303
        page = c.get("/specialists").text
        assert "Дарʼя Колган" in page and "Олег Старий" not in page
        assert "Олег Старий" in c.get("/specialists?show=fired").text
        card = c.get(f"/specialists/{darya}").text
        assert "IPL" in card and "Записи на 14 днів" in card
        r = c.post(f"/specialists/{darya}", data={"name": "Дарʼя Колган", "position": "лікар-косметолог",
                                                    "color": "#6f8a7a", "status": "active", "in_calendar": "1",
                                                    "work_days": "Пн–Пт"}, follow_redirects=False)
        assert r.status_code == 303
        assert "лікар-косметолог" in c.get("/specialists").text
        cal = c.get("/calendar?view=day&start=" + (date.today() + timedelta(days=1)).isoformat()).text
        assert "Дарʼя" in cal and "Тест Пацієнтка" in cal
        assert c.post("/specialists", data={"name": "Нова Майстриня", "position": "масажист"},
                      follow_redirects=False).status_code == 303


class NoStaffCB:
    def staff(self):
        return []


def test_specialists_from_visits_without_staff():
    with SessionLocal() as db:
        p = Patient(full_name="Візит Пацієнтка", cleverbox_id=5151)
        db.add(p); db.flush()
        db.add_all([Visit(source="cb_service_sale", external_id="n1", patient_id=p.id, master_name="Свіжа Майстриня",
                          starts_at=datetime.now() - timedelta(days=5), status="done"),
                    Visit(source="cb_service_sale", external_id="n2", patient_id=p.id, master_name="Давня Майстриня",
                          starts_at=datetime.now() - timedelta(days=400), status="done")])
        db.commit()
        cbm.sync_staff(db, NoStaffCB())
        docs = {d.name: d for d in db.query(Doctor).all()}
        assert docs["Свіжа Майстриня"].active and not docs["Давня Майстриня"].active


def test_time_grid_layout():
    from app.services.calendar import time_grid, Column
    day = date.today() + timedelta(days=1)
    at = lambda h, m=0: datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)
    d = Doctor(id=999, name="Тест Лікар", active=True)
    evs = [Event("visit", at(10, 30), "А", None, 1, doctor_id=999, ends=at(11, 15)),
           Event("visit", at(11), "Б", None, 2, doctor_id=999, ends=at(12)),          # накладка → 2 доріжки
           Event("visit", at(15), "В", None, 3, doctor_id=999, ends=at(15, 45))]
    g = time_grid(day, [Column("999", d, evs)], 10, 19)
    blocks = g["cols"][0]["blocks"]
    assert [b.lanes for b in blocks] == [2, 2, 1] and blocks[0].lane != blocks[1].lane
    free = g["cols"][0]["free"]
    assert [(f.start.hour, f.end.hour) for f in free] == [(12, 15), (15, 19)]
    assert g["h0"] == 10 and g["h1"] >= 19


def test_calendar_views_render():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        for v in ("day", "week", "month"):
            r = c.get(f"/calendar?view={v}")
            assert r.status_code == 200 and "Календар" in r.text
        assert 'class="mon"' not in c.get("/calendar?view=month").text          # місяць прибрано → день
        with SessionLocal() as db:
            from app.models import Doctor
            d = Doctor(name="Тижневий Лікар", in_calendar=True, active=True); db.add(d); db.commit(); did = d.id
        wk = c.get(f"/calendar?view=week&doc={did}").text
        assert "Всі спеціалісти" in wk and "Тижневий Лікар" in wk and 'class="wkh' in wk


def test_plans_board():
    from pathlib import Path
    pdf = Path(__file__).parent / "sample_plan.pdf"
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        if pdf.exists():
            c.post("/upload", files={"files": ("plan.pdf", pdf.read_bytes(), "application/pdf")})
        r = c.get("/plans")
        assert r.status_code == 200 and "Потребують запису" in r.text and "Записані" in r.text
        if pdf.exists():
            from app.models import BeautyPlan
            with SessionLocal() as db:
                pid = db.query(BeautyPlan).first().id
            panel = c.get(f"/plans?plan={pid}").text
            assert "Етапи плану" in panel and "Відкрити картку" in panel


def test_calendar_toggle_from_list():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        d = Doctor(name="Перемикач Календаря", active=True, in_calendar=True, source="manual")
        db.add(d); db.commit(); did = d.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Показувати в календарі" in c.get("/specialists").text
        r = c.post(f"/specialists/{did}/calendar", data={}, headers={"Accept": "application/json"})
        assert r.json() == {"ok": True, "in_calendar": False}
        from app.services.specialists import calendar_doctors
        with SessionLocal() as db:
            assert did not in [x.id for x in calendar_doctors(db)]
        r = c.post(f"/specialists/{did}/calendar", data={"enabled": "1"}, headers={"Accept": "application/json"})
        assert r.json()["in_calendar"] is True


def test_sale_initiator_toggle():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, Doctor
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        d = Doctor(name="Ініціатор Тест", active=True); db.add(d); db.commit(); did = d.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post(f"/specialists/{did}", data={"name": "Ініціатор Тест", "status": "active", "in_calendar": "1", "sale_initiator": "0"})
        with SessionLocal() as db:
            assert db.get(Doctor, did).sale_initiator is False
        c.post(f"/specialists/{did}", data={"name": "Ініціатор Тест", "status": "active", "in_calendar": "1", "sale_initiator": "1"})
        with SessionLocal() as db:
            assert db.get(Doctor, did).sale_initiator is True


def test_specialist_card_tabs():
    from datetime import datetime as _dt
    from app.models import Doctor, CbService, MedRecord, PatientFile, Patient, DoctorFile
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            d = Doctor(name="Картка Табів"); s = CbService(id=765432, name="Тест послуга табів", category="Тест категорія", price=100, time_min=45)
            p = Patient(full_name="Фото Пацієнтка"); db.add_all([d, s, p]); db.commit()
            r = MedRecord(patient_id=p.id, doctor_id=d.id, at=_dt(2026, 6, 13, 11, 45), title="IPL Над верхньою губою"); db.add(r); db.commit()
            db.add(PatientFile(patient_id=p.id, kind="photo", path="/tmp/x.jpg", stage="before", record_id=r.id)); db.commit()
            did = d.id
        for t in ("info", "services", "photos", "log", "files", "online"):
            assert c.get(f"/specialists/{did}?tab={t}").status_code == 200
        assert "IPL Над верхньою губою" in c.get(f"/specialists/{did}?tab=photos").text
        c.post(f"/specialists/{did}/services", data={"svc_765432": "1", "min_765432": "50"})
        assert "1 / 1" in c.get(f"/specialists/{did}?tab=services").text
        c.post(f"/specialists/{did}", data={"name": "Картка Табів", "status": "active", "in_calendar": "1", "sale_initiator": "1",
                                            "social_instagram": "@kartka", "wh_MOLODIST": "inject"})
        with SessionLocal() as db:
            ex = db.get(Doctor, did).extra
            assert ex["services"] == {"765432": 50} and ex["social"]["instagram"] == "@kartka" and ex["writeoff_wh"]["MOLODIST"] == "inject"
        c.post(f"/specialists/{did}/files", files=[("files", ("dyplom.pdf", b"%PDF-1.4 test", "application/pdf")), ("files", ("x.exe", b"MZ", "application/octet-stream"))])
        with SessionLocal() as db:
            fs = db.query(DoctorFile).filter_by(doctor_id=did).all()
            assert [f.name for f in fs] == ["dyplom.pdf"]
            fid = fs[0].id
        assert c.get(f"/specialists/files/{fid}").content.startswith(b"%PDF")
        assert "Оновлено картку співробітника" in c.get(f"/specialists/{did}?tab=log").text


def test_specialist_avatar_upload():
    from app.models import Doctor
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            d = Doctor(name="Аватар Тест"); db.add(d); db.commit(); did = d.id
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
        r = c.post(f"/specialists/{did}/avatar", files={"file": ("me.png", png, "image/png")}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            assert db.get(Doctor, did).photo_url.startswith(f"/specialists/{did}/avatar")
        assert c.get(f"/specialists/{did}/avatar").content == png
        assert f"/specialists/{did}/avatar?v=" in c.get(f"/specialists/{did}").text
        c.post(f"/specialists/{did}/avatar/delete")
        with SessionLocal() as db:
            assert db.get(Doctor, did).photo_url is None


def test_calendar_time_block():
    from datetime import date as _d
    from app.models import Doctor, TimeBlock
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            d = Doctor(name="Блок Календар", in_calendar=True); db.add(d); db.commit(); did = d.id
        day = _d.today().isoformat()
        c.post("/calendar/blocks", data={"doctor_id": did, "day": day, "t_from": "10:00", "t_to": "12:00", "title": "Зйомка"})
        page = c.get(f"/calendar?start={day}").text
        assert "Зйомка" in page and "Створити візит" in page and "Заблокувати час" in page
        with SessionLocal() as db:
            bid = db.query(TimeBlock).filter_by(doctor_id=did).one().id
        c.post(f"/calendar/blocks/{bid}/delete")
        with SessionLocal() as db:
            assert db.get(TimeBlock, bid) is None
        assert c.get(f"/calendar?start={day}&view=week").status_code == 200 and c.get(f"/calendar?start={day}&view=month").status_code == 200
