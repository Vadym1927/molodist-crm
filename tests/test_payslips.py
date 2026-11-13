"""Розрахункові листи: нараховано, премії, податки, утримання, до виплати, виплачено на руки."""
from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, FinOp, SalaryAdj, SalaryRule
from app.services import payslips as ps, finops as fo


def test_payslip_month():
    settings.admin_password = "owner-pass-1"
    mk = fo.month_key(date.today())
    with SessionLocal() as db:
        d = Doctor(name="Розрахунок Тест", active=True, position="Адміністратор")
        db.add(d); db.flush()
        db.add(SalaryRule(doctor_id=d.id, base=20000))
        db.add_all([SalaryAdj(doctor_id=d.id, month=mk, amount=3000, article="Премія", comment="план"),
                    SalaryAdj(doctor_id=d.id, month=mk, amount=-2500, article="Податки"),
                    SalaryAdj(doctor_id=d.id, month=mk, amount=-500, article="Штраф", comment="запізнення")])
        from app.services.fin_articles import salary_name
        db.add(FinOp(day=date.today(), kind="expense", category=salary_name(db), amount=15000, doctor_id=d.id, salary_month=mk, cashbox="Готівка"))
        db.commit()
        fo.set_payroll_start(db, mk)
        v = ps.build(db, mk)
        r = next(x for x in v["rows"] if x["doctor"].id == d.id)
        assert (r["base"], r["bonus"], r["gross"], r["tax"], r["hold"], r["net"], r["paid"], r["closing"]) == (20000, 3000, 23000, -2500, -500, 20000, 15000, 5000)
        assert v["total"]["gross"] >= 23000 and ps.to_xlsx(v)[:2] == b"PK"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get(f"/finance/payslips?m={mk}").text
        assert "Розрахункові листи" in h and "Розрахунок Тест" in h and "запізнення" in h and "Виплачено на руки" in h
        x = c.get(f"/finance/payslips?m={mk}&xlsx=1")
        assert x.status_code == 200 and x.content[:2] == b"PK"
