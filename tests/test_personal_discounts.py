from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient
from app.services import loyalty, personal_discounts as pdc


def test_personal_discounts():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        a = Patient(full_name="Віп Пацієнтка", phone="+380501110001"); b = Patient(full_name="Стара Знижка", phone="+380501110002", discount_pct=5)
        db.add_all([a, b]); db.commit(); ida, idb = a.id, b.id
        assert pdc.pcts(b) == (5, 5)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Персональні знижки" in c.get("/settings").text
        assert "Стара Знижка" in c.get("/settings/personal-discounts").text
        r = c.post("/settings/personal-discounts", data={"name": "VIP", "auto": "1", "pid": [ida], "products": ["10"], "services": ["3"]}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            a = db.get(Patient, ida); b = db.get(Patient, idb)
            assert pdc.pcts(a) == (10, 3) and not pdc.has(b)                 # прибрана зі списку — без знижки
            items = [{"type": "product", "price": 1000, "qty": 1}, {"type": "service", "price": 2000, "qty": 1}]
            d = loyalty.discount_for(db, items, a)
            assert d["lines"] == [100.0, 60.0] and d["rules"][0].startswith("VIP")
            pdc.save_cfg(db, "VIP", auto=False, combine=False)
            assert loyalty.discount_for(db, items, a)["total"] == 0                       # не авто — лише якщо обрали
            assert loyalty.discount_for(db, items, a, rule_ids=[0])["total"] == 160
            pdc.save_cfg(db, "Персональна знижка", auto=True, combine=False)
        c.post(f"/patients/{ida}/discount", data={"products": "", "services": ""})
        with SessionLocal() as db:
            assert not pdc.has(db.get(Patient, ida))


def test_personal_discounts_import_cb():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Аштаменко Тест", cleverbox_id=990082); db.add(p); db.commit(); pid = p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.post("/settings/personal-discounts/import-cb", json={"items": [[990082, 10, 0], [123456789, 5, 5]], "name": "Personal discount", "auto": "1"}).json()
        assert r["saved"] == 1 and r["no_patient"] == [123456789]
        with SessionLocal() as db:
            assert pdc.pcts(db.get(Patient, pid)) == (10, 0) and pdc.cfg(db)["name"] == "Personal discount"
            pdc.save_cfg(db, "Персональна знижка", True, False)


def test_cashbox_in_shift():
    from app.services.cashboxes import in_shift
    assert in_shift({"type": "cash", "name": "Готівка ФОП Колган"}) and in_shift({"type": "card", "name": "Термінал"})
    assert not in_shift({"type": "cash", "name": "Готівка Cейф"}) and not in_shift({"type": "bank", "name": "Фонд"})
    assert in_shift({"type": "cash", "name": "Готівка Сейф", "shift": True})
