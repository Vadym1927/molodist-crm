from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, BeautyPlan, PlanItem


def test_plan_edit_items():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="План Редагування"); db.add(p); db.flush()
        pl = BeautyPlan(patient_id=p.id, total_planned=300)
        pl.items = [PlanItem(kind="procedure", name="Чистка", qty=1, unit_price=100, total=100),
                    PlanItem(kind="procedure", name="Пілінг", qty=1, unit_price=200, total=200)]
        db.add(pl); db.commit(); plid, i1, i2 = pl.id, pl.items[0].id, pl.items[1].id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post(f"/plans/{plid}/items", data={f"name_{i1}": "Чистка обличчя", f"price_{i1}": "150", f"qty_{i1}": "2", f"del_{i2}": "1",
                                            "new_kind": "product", "new_name": "Крем SPF", "new_price": "900", "new_qty": "1",
                                            "complaints": "висипи", "status": "active"})
    with SessionLocal() as db:
        pl = db.get(BeautyPlan, plid)
        names = {i.name: i for i in pl.items}
        assert set(names) == {"Чистка обличчя", "Крем SPF"} and names["Чистка обличчя"].total == 300
        assert pl.total_planned == 1200 and pl.complaints == "висипи"


def test_cb_sync_one_by_name():
    from app.integrations import cleverbox as cbx

    class FakeCB:
        def search_client(self, phone): return []
        def clients(self, **kw): return iter([{"id": "777001", "name": "Синк Тестова Ольга", "phone": "380501231234", "date_add": "21.10.2026"}])
        def services_sales(self, *a, **k): return iter([])
        def client_future_visits(self, cid): return []
    with SessionLocal() as db:
        p = Patient(full_name="Синк Тестова Ольга"); db.add(p); db.commit()
        r = cbx.sync_one(db, p, cb=FakeCB())
        assert r["cb_id"] == 777001 and p.cleverbox_id == 777001
