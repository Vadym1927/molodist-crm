"""Звіт «Касові зміни»: залишки по касах за зміну, продажі, операції після закриття, сторінка й Excel."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbFinOp, FinOp, Sale, SalePayment, SaleItem, User
from app.services import cashboxes as cbx, shifts as sh, shift_report


def test_shift_report_build_and_page():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            db.add(CbFinOp(id=777001, at=datetime(2026, 10, 1, 10), kind="income", account="Готівка ФОП Бондар", amount=1000))
            db.commit()
            box = next(x for x in cbx.overview(db) if x["name"] == "Готівка ФОП Бондар")
            cbx.save(db, box["id"], {**box, "methods": ["Готівка"], "active": True, "shift": True})
            owner = db.scalar(select(User).where(User.role == "owner"))
            s_ = sh.open_shift(db, owner, [box["id"]])
            base = s_.boxes[0]["open"]
            sale = Sale(total=150, subtotal=150, status="paid", at=datetime.now(), created_by=owner.id)
            sale.items.append(SaleItem(kind="other", name="x", qty=1, price=150, total=150))
            sale.payments.append(SalePayment(method="Готівка", amount=150))
            db.add(sale)
            db.add(FinOp(day=date.today(), kind="expense", category="Канцтовари", amount=50, cashbox="Готівка ФОП Бондар"))
            db.commit()
            r = shift_report.build(db, date.today(), date.today())
            assert r["n"] == 0 and r["n_open"] == 1 and r["open"][0]["boxes"][0]["expected"] == round(base + 100, 2) and r["open"][0]["total"] == 150
            sh.close_shift(db, owner, "усе ок")
            r = shift_report.build(db, date.today(), date.today())
            assert r["n"] == 1 and r["n_open"] == 0
            b = r["rows"][0]["boxes"][0]
            assert b["in"] == 150 and b["out"] == 50 and b["close"] == round(base + 100, 2) and b["late"] == 0
            assert r["rows"][0]["checks"] == 1 and r["by_user"][0]["total"] == 150 and r["by_method"][0][0] == "Готівка"
            # операція заднім числом після закриття → розбіжність
            db.add(FinOp(day=date.today(), kind="expense", category="Таксі", amount=30, cashbox="Готівка ФОП Бондар",
                         created_at=datetime.utcnow() - timedelta(hours=5)))
            db.commit()
            r = shift_report.build(db, date.today(), date.today())
            assert r["rows"][0]["late"] == 30 and r["late_n"] == 1
            assert shift_report.build(db, date.today(), date.today(), user_id=owner.id + 999)["n"] == 0
            assert len(shift_report.to_xlsx(r)) > 1000
        t = c.get("/reports?r=shifts&period=today").text
        assert "Касові зміни" in t and "Готівка ФОП Бондар" in t and "усе ок" in t and "Після закриття" in t
        assert c.get("/reports?r=shifts&period=today&fmt=xlsx").status_code == 200
        hub = c.get("/reports").text
        assert 'href="/reports?r=shifts"' in hub and "Щоденний фінансовий звіт" not in hub
