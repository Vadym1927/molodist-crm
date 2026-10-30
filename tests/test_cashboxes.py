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


def test_pos_toggle():
    from app.services import sales as sl
    with SessionLocal() as db:
        cbx.apply_seed(db)
        f = next(x for x in cbx.items(db) if x["name"] == "Операційний Фонд")
        assert "Операційний Фонд" not in sl.methods(db)
        cbx.save(db, f["id"], {**f, "active": True, "pos": True})
        assert "Операційний Фонд" in sl.methods(db)
        s = next(x for x in cbx.items(db) if x["name"] == "Готівка Cейф")
        cbx.save(db, s["id"], {**s, "active": True, "pos": False})
        assert "Готівка Cейф" not in sl.methods(db)


def test_bank_fee_on_payments():
    with SessionLocal() as db:
        cbx.apply_seed(db)
        x = next(b for b in cbx.items(db) if b["name"] == "Термінал ФОП Колган")
        cbx.save(db, x["id"], {**x, "active": True, "methods": ["Картка (термінал)"], "fee_pct": 1.3,
                               "fee_article": "Комісія банку Еквайринг MonoBank"})
        assert next(b for b in cbx.items(db) if b["id"] == x["id"])["fee_from"] == date.today().isoformat()
        old = Sale(total=999, subtotal=999, status="paid", at=datetime(2026, 1, 5, 12))       # до увімкнення — без комісії
        old.payments.append(SalePayment(method="Картка (термінал)", amount=999))
        s = Sale(total=2000, subtotal=2000, status="paid", at=datetime.now())
        s.payments.append(SalePayment(method="Картка (термінал)", amount=1500)); s.payments.append(SalePayment(method="Готівка", amount=500))
        op = FinOp(day=date.today(), kind="income", category="Внесення коштів в касу", amount=1000, cashbox="Термінал ФОП Колган")
        db.add_all([old, s, op]); db.commit()
        assert cbx.sync_fees(db) == 2 and cbx.sync_fees(db) == 0           # ідемпотентно
        fees = db.query(FinOp).filter(FinOp.ref.like("fee:%"), FinOp.deleted_at.is_(None)).all()
        assert sorted(f.amount for f in fees) == [13.0, 19.5]
        assert all(f.kind == "expense" and f.cashbox == "Термінал ФОП Колган" and f.category == "Комісія банку Еквайринг MonoBank" for f in fees)
        bal = next(b for b in cbx.overview(db) if b["name"] == "Термінал ФОП Колган")["balance"]
        s.status = "refunded"; db.commit()                                  # повернення — комісію знято
        cbx.sync_fees(db)
        assert [f.amount for f in db.query(FinOp).filter(FinOp.ref.like("fee:sp:%"), FinOp.deleted_at.is_(None))] == []
        assert next(b for b in cbx.overview(db) if b["name"] == "Термінал ФОП Колган")["balance"] != bal
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/settings/cashboxes").text
        assert "Комісія 1.3%" in t and 'name="fee_pct"' in t
        assert "Комісія банку Еквайринг MonoBank" in c.get("/finance/ops").text
        c.post("/finance/ops", data={"kind": "income", "category": "Внесення коштів в касу", "amount": "200",
                                     "cashbox": "Термінал ФОП Колган"}, follow_redirects=False)
        with SessionLocal() as db:                                    # після операції — комісія одразу в журналі
            assert db.query(FinOp).filter(FinOp.ref.like("fee:op:%"), FinOp.amount == 2.6).count() == 1
