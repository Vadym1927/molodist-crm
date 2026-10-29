from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbFinOp, FinOp, Sale, SalePayment, SaleItem
from app.services import cashboxes as cbx


def test_cashboxes_from_cleverbox_and_balance():
    with SessionLocal() as db:
        db.add_all([CbFinOp(id=1, at=datetime(2026, 10, 1, 10), kind="income", account="Готівка ФОП Бондар", amount=1000),
                    CbFinOp(id=2, at=datetime(2026, 10, 2, 10), kind="expense", account="Готівка ФОП Бондар", amount=300),
                    CbFinOp(id=3, at=datetime(2026, 10, 3, 10), kind="transfer", account="Готівка ФОП Бондар", vid="Операційний Фонд", amount=200)])
        db.commit()
        names = cbx.names(db)
        assert "Готівка ФОП Бондар" in names and "Операційний Фонд" in names
        ov = {b["name"]: b for b in cbx.overview(db)}
        assert ov["Готівка ФОП Бондар"]["balance"] == 500 and ov["Готівка ФОП Бондар"]["cash"]
        assert ov["Операційний Фонд"]["balance"] == 200 and not ov["Операційний Фонд"]["cash"]
        b = ov["Готівка ФОП Бондар"]
        cbx.save(db, b["id"], {**b, "methods": ["Готівка"], "active": True})
        s = Sale(total=150, subtotal=150, status="paid", at=datetime(2026, 10, 5, 12))
        s.items.append(SaleItem(kind="other", name="x", qty=1, price=150, total=150)); s.payments.append(SalePayment(method="Готівка", amount=150))
        db.add(s); db.add(FinOp(day=date(2026, 10, 6), kind="expense", category="Оренда", amount=50, cashbox="Готівка ФОП Бондар")); db.commit()
        assert next(x for x in cbx.overview(db) if x["name"] == "Готівка ФОП Бондар")["balance"] == 600
        cbx.save(db, b["id"], {**b, "methods": ["Готівка"], "active": True, "opening": 17321, "opening_date": "2026-10-05"})
        assert next(x for x in cbx.overview(db) if x["name"] == "Готівка ФОП Бондар")["balance"] == 17321 + 150 - 50
        other = ov["Операційний Фонд"]
        try:
            cbx.save(db, other["id"], {**other, "methods": ["Готівка"], "active": True})
            assert False
        except ValueError:
            pass
        cbx.delete(db, other["id"])                          # є операції → у видалені
        assert next(x for x in cbx.items(db) if x["id"] == other["id"])["deleted"] and "Операційний Фонд" not in cbx.names(db)


def test_cashboxes_page_and_hub():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        hub = c.get("/settings").text
        assert 'href="/settings/cashboxes"' in hub and "Чайові" not in hub and "Операції з балансом пацієнта" not in hub
        t = c.get("/settings/cashboxes").text
        assert "Каси / рахунки" in t and "Готівка" in t
        r = c.post("/settings/cashboxes", data={"name": "Картка МОНО", "type": "bank", "active": "1", "opening": "0"}, follow_redirects=False)
        assert r.status_code == 303 and "msg=" in r.headers["location"]
        t = c.get("/settings/cashboxes").text
        assert "Картка МОНО" in t
        with SessionLocal() as db:
            bid = next(x["id"] for x in cbx.items(db) if x["name"] == "Картка МОНО")
        assert "Операцій ще немає" in c.get(f"/settings/cashboxes?open={bid}").text
        assert "Картка МОНО" in c.get("/finance/ops").text           # каса в довіднику фіноперацій


def test_seed_20261030():
    with SessionLocal() as db:
        db.add(CbFinOp(id=9, at=datetime(2026, 10, 29, 10), kind="income", account="Картка МОНО", amount=5)); db.commit()
        assert cbx.apply_seed(db) and not cbx.apply_seed(db)
        ov = {b["name"]: b for b in cbx.overview(db)}
        assert ov["Амортизаційний + Операційний Фонд"]["balance"] == 278751.33 and ov["Готівка Cейф"]["cash"]
        assert not ov["Картка МОНО"]["active"] and ov["Готівка ФОП Бондар"]["balance"] == 22011.0
        assert "Картка МОНО" not in cbx.names(db) and len(cbx.names(db)) == 8


def test_cash_day_and_fin_day_include_crm():
    from app.models import Doctor
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        cbx.apply_seed(db)
        x = next(b for b in cbx.items(db) if b["name"] == "Готівка ФОП Бондар")
        cbx.save(db, x["id"], {**x, "methods": ["Готівка"], "active": True, "opening_date": date.today().isoformat(), "opening": 1000})
        d = Doctor(name="Денна Лікарка", active=True); db.add(d); db.flush()
        s = Sale(total=400, subtotal=500, discount=100, status="paid", at=datetime.now())
        s.items.append(SaleItem(kind="service", name="Пілінг денний", qty=1, price=300, discount=0, total=300, doctor_id=d.id))
        s.items.append(SaleItem(kind="product", name="Крем денний", qty=1, price=200, discount=100, total=100, doctor_id=d.id))
        s.payments.append(SalePayment(method="Готівка", amount=400)); db.add(s); db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/sales").text
        assert "Продажі за добу" in t and "Пілінг денний" in t and "1000.00" in t and "1400.00" in t
        f = c.get("/finance/day").text
        assert "Денна Лікарка" in f and "Крем денний" in f and "Косметика" in f
