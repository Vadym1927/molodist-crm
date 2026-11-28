from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, FinOp


def test_change_op_article():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        op = FinOp(day=date.today(), kind="expense", category="Оренда", amount=30060, cashbox="Термінал")
        db.add(op); db.commit(); oid = op.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post(f"/finance/ops/{oid}/article", data={"article": "Оренда прим.Клініки"})
        assert "/finance/ops/%d/article" % oid in c.get(f"/finance/ops?df={date.today()}&dt_={date.today()}").text
    with SessionLocal() as db:
        assert db.get(FinOp, oid).category == "Оренда прим.Клініки"
