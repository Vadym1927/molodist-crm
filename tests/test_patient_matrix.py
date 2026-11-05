"""Матриця статусів пацієнтів: умови за візитами / запланованими / днями від останнього візиту, плаваючі статуси."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, Appointment, Doctor, SyncState
from app.services import patient_tags as pt


def test_matrix_rules_and_floating():
    L = [pt._norm(r) for r in pt.DEFAULT_MATRIX]
    assert pt.tag_for(L, 0, 1, None) == "Потенційний" and pt.tag_for(L, 1, 0, 20) == "Первинний пацієнт"
    assert pt.tag_for(L, 5, 0, 100) == "Постійний пацієнт" and pt.tag_for(L, 12, 0, 10) == "ФАНАТ"
    assert pt.tag_for(L, 12, 0, 200) == "Давно не бачилися" and pt.tag_for(L, 3, 0, 400) == "Колишній"
    assert pt.tag_for(L, 3, 1, 400) == "Постійний пацієнт"          # повернувся з записом — знову активний
    assert pt.tag_for(L, 3) == "Постійний пацієнт"                  # без дат — лише за візитами (старі виклики)
    # старий формат налаштувань {name, min, color} читається як «від N візитів»
    with SessionLocal() as db:
        pt.save_tags(db, [{"name": "Новий", "min": 0, "color": "#111111"}, {"name": "Свій", "min": 3, "color": "#222222"}])
        lv = pt.tags(db)
        assert [x["name"] for x in lv] == ["Новий", "Свій"] and lv[1]["v_from"] == 3 and lv[1]["v_to"] == pt.INF and pt.tag_for(lv, 4, 0, 500) == "Свій"
        pt.reset_tags(db)
        assert len(pt.tags(db)) == 6


def test_stats_and_settings_page():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        pt.reset_tags(db)
        d = Doctor(name="Матриця Лікар", active=True); db.add(d); db.commit()
        p = Patient(full_name="Матриця Пацієнтка"); db.add(p); db.commit()
        old = datetime.now() - timedelta(days=200)
        db.add(Appointment(patient_id=p.id, doctor_id=d.id, start=old, end=old + timedelta(hours=1), status="done"))
        db.add(Appointment(patient_id=p.id, doctor_id=d.id, start=old - timedelta(days=30), end=old - timedelta(days=30) + timedelta(hours=1), status="paid"))
        db.commit()
        st = pt.stats(db, [p.id])[p.id]
        assert st["visits"] == 2 and st["planned"] == 0 and 199 <= st["last_days"] <= 201
        assert pt.tag_of(db, pt.tags(db), p.id) == "Давно не бачилися"
        fut = datetime.now() + timedelta(days=3)
        db.add(Appointment(patient_id=p.id, doctor_id=d.id, start=fut, end=fut + timedelta(hours=1), status="booked")); db.commit()
        assert pt.tag_of(db, pt.tags(db), p.id) == "Постійний пацієнт"
        pid = p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        pg = c.get("/settings/patient-tags").text
        assert "Матриця статусів" in pg and "Давно не бачилися" in pg and 'name="l_from"' in pg
        r = c.post("/settings/patient-tags", data={"what": "tags", "name": ["Новачок", "Спить"], "color": ["#111111", "#222222"], "cat": ["active", "sleep"],
                                                   "v_from": ["0", "1"], "v_to": ["", ""], "p_from": ["0", "0"], "p_to": ["", "0"], "l_from": ["0", "90"], "l_to": ["", ""]},
                   follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            lv = pt.tags(db)
            assert [x["name"] for x in lv] == ["Новачок", "Спить"] and lv[1]["cat"] == "sleep" and lv[1]["l_from"] == 90 and lv[1]["p_to"] == 0
            assert pt.tag_of(db, lv, pid) == "Новачок"            # є майбутній запис → «Спить» не підходить
        assert "Матриця Пацієнтка" in c.get("/clients").text
