from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Check, Doctor, FinOp, PayrollDoc, Sale, SaleItem, StaffShift
from app.services import payroll_docs as pd, salary_plans as spl, fin_articles as fa


def test_payroll_doc_period():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        xs = spl.items(db)
        a = {"id": 951, **spl.blank("Док денний")}
        a["fixed"] = {"on": True, "amount": 1200.0, "period": "day"}
        b = {"id": 952, **spl.blank("Док комісійний")}
        b["comm"].update(on=True, services=[30.0, "paid"], products=[10.0, "paid"])
        b["fixed"] = {"on": True, "amount": 31000.0, "period": "month"}
        spl._save(db, xs + [a, b])
        d1 = Doctor(name="Адмін Денна", active=True, extra={"salary": "Док денний"})
        d2 = Doctor(name="Лікарка Комісійна", active=True, extra={"salary": "Док комісійний"})
        db.add_all([d1, d2]); db.flush()
        for dd in (17, 18, 20):
            db.add(StaffShift(doctor_id=d1.id, day=date(2026, 10, dd), start="10:00", end="20:00"))
        db.add(Check(id=880101, date=datetime(2026, 10, 20, 12), amount=3000, deleted=False, payments=[],
                     items=[{"item_type": "service", "item_name": "Пілінг", "payd_amount": 2000, "quantity": 1, "price": 2000,
                             "initiator_name": "Лікарка Комісійна"},
                            {"item_type": "product", "item_name": "Крем", "payd_amount": 1000, "quantity": 1, "price": 1000,
                             "initiator_name": "Лікарка Комісійна"}]))
        s = Sale(status="paid", at=datetime(2026, 10, 10, 12), subtotal=5000, total=5000)      # поза періодом 16–31
        s.items = [SaleItem(kind="service", name="Ботулінотерапія", qty=1, price=5000, total=5000, doctor_id=d2.id)]
        db.add(s); db.commit()
        rows = {r["doctor"].name: r for r in pd.compute(db, date(2026, 10, 16), date(2026, 10, 31))}
        assert rows["Адмін Денна"]["days"] == 3 and rows["Адмін Денна"]["calc"] == 3600
        k = rows["Лікарка Комісійна"]
        assert k["detail"]["comm_services"] == 600 and k["detail"]["comm_products"] == 100
        assert k["detail"]["fixed"] == 16000                      # 31000 × 16/31
        assert k["calc"] == 16700
        d1id, d2id = d1.id, d2.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Нарахування зарплати" in c.get("/finance/accruals").text
        t = c.get("/finance/accruals/new?df=2026-10-16&dt_=2026-10-31").text
        assert "Адмін Денна" in t and "Док комісійний" in t and "3600.00" in t
        r = c.post("/finance/accruals", data={"df": "2026-10-16", "dt_": "2026-10-31", f"a_{d1id}": "4000", "comment": "друга половина",
                                              "post": "1"}, follow_redirects=False)
        did = int(r.headers["location"].split("/")[-1].split("?")[0])
        with SessionLocal() as db:
            doc = db.get(PayrollDoc, did)
            assert doc.posted and doc.total == 4000 + 16700
            assert pd.balances(db)[d1id] == 4000
            db.add(FinOp(day=date(2026, 11, 1), kind="expense", category=fa.salary_name(db), amount=1500, doctor_id=d1id)); db.commit()
            assert pd.balances(db)[d1id] == 2500
        v = c.get(f"/finance/accruals/{did}").text
        assert "На балансі" in v and "Зняти з балансу" in v and "друга половина" in v
        assert "уже є нарахування" in c.get("/finance/accruals/new?df=2026-10-20&dt_=2026-10-25").text
        assert c.get(f"/finance/accruals/{did}?fmt=xlsx").content[:2] == b"PK"
        c.post(f"/finance/accruals/{did}/post", data={"on": "0"})
        with SessionLocal() as db:
            assert not db.get(PayrollDoc, did).posted and pd.balances(db) == {}
