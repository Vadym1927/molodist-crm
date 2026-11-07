"""Звіт «Доходи і витрати за рік»: статті по місяцях, сторно, переміщення, порівняння років, Excel."""
from datetime import datetime, date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbFinOp, FinOp
from app.services import pnl


def _seed(db):
    rows = [(2022, 3, "income", "Оплата послуг, товарів", 10000, "Готівка"), (2022, 3, "expense", "Зарплата лікарям", 3000, "Готівка"),
            (2022, 3, "expense", "Оренда приміщення", 2000, "Готівка"), (2022, 3, "transfer", "Переміщення", 999, "Готівка"),
            (2022, 4, "income", "Оплата послуг, товарів", 5000, "Термінал"), (2022, 4, "storno", "Оплата послуг, товарів", 500, "Термінал"),
            (2023, 3, "income", "Оплата послуг, товарів", 12000, "Готівка"), (2023, 3, "expense", "Реклама Google", 1500, "Готівка")]
    for i, (y, m, k, a, v, acc) in enumerate(rows, 1):
        db.add(CbFinOp(id=900000 + i, at=datetime(y, m, 10, 12), kind=k, article=a, amount=v, account=acc, source="import"))
    db.add(FinOp(day=date(2023, 3, 15), kind="expense", category="Дивіденди", amount=700, cashbox="Готівка"))
    db.commit()


def test_pnl_build_and_page():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        _seed(db)
        r22 = pnl.build(db, 2022)
        assert r22["sum_i"] == 14500 and r22["sum_e"] == 5000 and r22["sum_net"] == 9500
        assert r22["ti"][2] == 10000 and r22["ti"][3] == 4500 and r22["skipped"]["transfer"] == 999
        g = {x["key"]: x for x in r22["groups"]}
        assert g["salary"]["total"] == 3000 and g["rent"]["total"] == 2000
        r23 = pnl.build(db, 2023)
        assert r23["prev"]["sum_i"] == 14500 and r23["yoy"]["i"] is not None
        assert {x["key"] for x in r23["groups"]} >= {"marketing", "owner"}
        assert 2022 in pnl.years(db)
        xl = pnl.to_xlsx(r23)
        assert xl[:2] == b"PK"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/reports?r=pnl&year=2022").text
        assert "Доходи і витрати за 2022" in t and "Зарплата й персонал" in t and "Оренда й комунальні" in t
        assert c.get("/reports?r=pnl&year=2023&fmt=xlsx").status_code == 200
        assert "Фінансовий аналіз доходів і витрат за рік" in c.get("/reports").text
