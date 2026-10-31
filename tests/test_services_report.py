from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Check, CbService, Doctor, Product, ProcedureNorm, Sale, SaleItem
from app.services import reports as rp, salary_plans as spl


def test_services_sales_report():
    settings.admin_password = "owner-pass-1"
    day = datetime(2026, 10, 27, 12)
    with SessionLocal() as db:
        xs = spl.items(db)
        xs.append({"id": 900, **spl.blank("Звітний оклад")})
        xs[-1]["comm"].update(on=True, services=[20.0, "paid"])
        spl._save(db, xs)
        d = Doctor(name="Звітна Лікарка", active=True, extra={"salary": "Звітний оклад"})
        db.add_all([d, CbService(id=7001, name="Звітний Пілінг PRX", category="Звітні Пілінги", price=2700),
                    CbService(id=7002, name="Звітна Консультація", category="Звітні Консультації", price=1000),
                    CbService(id=7003, name="Звітна Непродана", category="Звітні Пілінги", price=500)])
        pr = Product(name="Звітна кислота", name_norm="звітна кислота", cost=100.0)
        db.add(pr); db.flush()
        db.add(ProcedureNorm(service_id=7001, product_id=pr.id, qty=2))           # 200 ₴ матеріалів на 1 пілінг
        db.add(Check(id=880001, date=day, amount=5400, discount=0, deleted=False, payments=[], author="Адмін",
                     items=[{"item_type": "service", "item_name": "Звітний Пілінг PRX", "category_name": "Звітні Пілінги", "quantity": 2,
                             "price": 2700, "payd_amount": 5000, "initiator_name": "Звітна Лікарка"},
                            {"item_type": "product", "item_name": "Крем", "payd_amount": 900, "quantity": 1}]))
        s = Sale(status="paid", at=day, subtotal=1000, total=1000)
        s.items = [SaleItem(kind="service", name="Звітна Консультація", category="Звітні Консультації", qty=1, price=1000, total=1000,
                            doctor_id=d.id)]
        db.add(s); db.commit()
        r = rp.build(db, "services", date(2026, 10, 27), date(2026, 10, 27))
        assert r.title == "Звіт з продажу послуг" and r.grouped
        names = [(z["name"], bool(z.get("_group"))) for z in r.rows]
        assert ("Звітні Пілінги", True) in names and ("Звітний Пілінг PRX", False) in names and ("Звітна Консультація", False) in names
        p = next(z for z in r.rows if z["name"] == "Звітний Пілінг PRX")
        assert p["qty"] == 2 and p["sum"] == 5000 and p["disc"] == 400 and p["cost"] == 400 and p["salary"] == 1000
        assert p["profit"] == 5000 - 400 - 1000
        g = next(z for z in r.rows if z["name"] == "Звітні Пілінги")
        assert g["sum"] == 5000 and g["_group"]
        assert r.total["sum"] == 6000 and r.total["salary"] == 1200            # групи не рахуються двічі
        assert "Звітна Непродана" not in names
        assert any(z["name"] == "Звітна Непродана" for z in rp.build(db, "services", date(2026, 10, 27), date(2026, 10, 27), zero="all").rows)
        only = rp.build(db, "services", date(2026, 10, 27), date(2026, 10, 27), cat="Звітні Консультації")
        assert [z["name"] for z in only.rows if not z.get("_group")] == ["Звітна Консультація"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/reports?r=services&df=2026-10-27&dt_=2026-10-27").text
        assert "Звіт з продажу послуг" in t and "Скрити з 0" in t and "З/П спеціаліста" in t and "Звітний Пілінг PRX" in t
        x = c.get("/reports?r=services&df=2026-10-27&dt_=2026-10-27&staff=Звітна Лікарка&fmt=xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"
