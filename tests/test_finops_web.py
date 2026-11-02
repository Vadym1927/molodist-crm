from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbFinOp
from app.services import autonomy


def test_finops_web_import():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            autonomy.save(db, autonomous=True, since="2026-11-01")
        rows = [[32336, "31.10.2026 18:40:54", "pko", 9500, "Джус Валентина", "Продаж #12090", "Оплата послуг, товарів", "Molodist_", "Готівка ФОП Бондар", "Готівка ФОП Бондар", "Дзеціна Вікторія", "Продаж N 12090"],
                [32337, "31.10.2026 20:04:56", "rpo", -12000, "", "Переказ", "", "Molodist_", "Готівка ФОП Бондар", "Готівка Cейф", "Бондар Вадим", ""],
                [32343, "01.11.2026 10:56:31", "rko", -1800, "Колган Дар'я", "Нарахування зарплатні #6688", "Персонал Зарплата", "Molodist_", "Готівка ФОП Колган", "", "Бондар Вадим", ""]]
        r = c.post("/finance/ops/import-cb-web", json={"rows": rows}).json()
        assert r["created"] == 2 and r["skipped"] == 1
        assert c.post("/finance/ops/import-cb-web", json={"rows": rows}).json()["updated"] == 2
    with SessionLocal() as db:
        x = db.get(CbFinOp, 32337)
        assert x.kind == "transfer" and x.amount == 12000 and x.vid == "Готівка Cейф" and x.at.hour == 20
        assert db.get(CbFinOp, 32343) is None
