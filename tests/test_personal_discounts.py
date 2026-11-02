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
