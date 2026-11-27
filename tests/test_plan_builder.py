from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, BeautyPlan, CbService, Doctor, Patient, Product
from app.services import plan_builder as pb


def test_plan_builder_flow():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Планова Ольга Іванівна", phone="+380671112233")
        d = Doctor(name="Лікар План")
        db.add_all([p, d, CbService(id=97001, name="Біоревіталізація План", price=4000),
                    Product(name="Крем План SPF", name_norm="крем план spf", warehouse="vitrina", price=1500)])
        db.commit()
        pid, did = p.id, d.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.post(f"/patients/{pid}/plans/new", follow_redirects=False)
        plan_id = int(r.headers["location"].split("/")[2])
        assert "Консультація" in c.get(f"/plans/{plan_id}/edit").text
        c.post(f"/plans/{plan_id}/edit", data={
            "session_at": "2026-12-01", "doctor_id": str(did), "mimic_type": "Гіпермімічний", "complaints": "зморшки",
            "diagnosis": "Хроностаріння", "anamnesis": "алергій немає", "conclusion": "Курс біоревіталізації",
            "new_kind": ["procedure", "procedure", "product"], "new_name": ["Біоревіталізація План", "Пілінг ручний", "Крем План SPF"],
            "new_price": ["", "1200", ""], "new_qty": ["3", "1", "1"], "new_interval": ["14", "", "60"], "action": "dates"})
        with SessionLocal() as db:
            pl = db.get(BeautyPlan, plan_id)
            its = pb.ordered(pl)
            assert [i.name for i in its] == ["Біоревіталізація План", "Пілінг ручний", "Крем План SPF"]
            assert its[0].unit_price == 4000 and its[2].unit_price == 1500 and pl.total_planned == 4000 * 3 + 1200 + 1500
            assert its[0].plan_date == date(2026, 12, 1) and its[1].plan_date == date(2027, 1, 12)   # курс 3 × 14 днів
            assert pl.diagnosis == "Хроностаріння" and pl.mimic_type == "Гіпермімічний" and pl.doctor_id == did
            second = its[1].id
        c.post(f"/plans/{plan_id}/edit", data={"action": f"up_{second}"})                 # переставити місцями
        with SessionLocal() as db:
            assert pb.ordered(db.get(BeautyPlan, plan_id))[0].name == "Пілінг ручний"
        r = c.get(f"/plans/{plan_id}/pdf?v=full")
        assert r.status_code == 200 and r.content[:4] == b"%PDF" and "380671112233" in r.headers["content-disposition"]
        assert c.get(f"/plans/{plan_id}/pdf?v=short").content[:4] == b"%PDF"
        assert "Редактор плану" in c.get(f"/patients/{pid}").text
