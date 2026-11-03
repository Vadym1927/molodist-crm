from datetime import datetime

from app.models import SessionLocal, Check, Patient, ProductSale


def test_sale_initiator_from_cb_check():
    from app.main import _sale_sellers
    with SessionLocal() as db:
        p = Patient(full_name="Ініціатор Тест"); db.add(p); db.flush()
        db.add(Check(id=987654, date=datetime(2026, 11, 3, 12), amount=2050, patient_id=p.id,
                     items=[{"item_type": "product", "item_id": 55, "item_name": "Крем", "initiator_name": "Дар'я Колган"}]))
        s1 = ProductSale(external_id="987654-55-2026-11-03", patient_id=p.id, product_id=55, product_name="Крем", amount=2050,
                         sold_at=datetime(2026, 11, 3, 12))
        s2 = ProductSale(external_id="111-1-x", patient_id=p.id, product_name="Без чека", amount=1, sold_at=datetime(2026, 11, 3))
        db.add_all([s1, s2]); db.commit()
        r = _sale_sellers(db, [s1, s2])
        assert r == {s1.id: "Дар'я Колган"}


def test_patient_groups_add_remove():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Група Тест", client_groups=["ЖК", {"name": "Трихолог"}]); db.add(p); db.commit(); pid = p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Трихолог" in c.get(f"/patients/{pid}").text
        c.post(f"/patients/{pid}/groups", data={"action": "remove", "name": "Трихолог"})
        c.post(f"/patients/{pid}/groups", data={"action": "add", "name": "ЛЄ розсилка"})
        names = [g["name"] for g in c.get("/patient-groups").json()]
        assert "ЖК" in names and "ЛЄ розсилка" in names
    with SessionLocal() as db:
        assert db.get(Patient, pid).client_groups == ["ЖК", "ЛЄ розсилка"]
