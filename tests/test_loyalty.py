from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, BonusTxn, DiscountRule
from app.services import loyalty as ly


def test_loyalty():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Лояльна Пацієнтка", cb_bonus=120, birthday=date.today().replace(year=1990))
        db.add(p); db.commit(); pid = p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post("/settings/loyalty/bonus", data={"enabled": "1", "pct_services": "5", "pct_products": "3", "max_pay_pct": "30",
                                                "expire_days": "365", "birthday": "200"})
        c.post("/settings/loyalty/discount", data={"name": "Догляд -10%", "kind": "percent", "value": "10", "applies_to": "products",
                                                   "auto": "1", "active": "1"})
        c.post("/settings/loyalty/discount", data={"name": "Іменинникам -20%", "kind": "percent", "value": "20", "applies_to": "all",
                                                   "segment": "birthday", "auto": "1", "active": "1"})
        c.post("/settings/loyalty/import")
        c.post(f"/patients/{pid}/bonus", data={"amount": "50", "sign": "+", "comment": "подарунок"})
        r = c.post(f"/patients/{pid}/bonus", data={"amount": "10000", "sign": "-"}, follow_redirects=False)
        assert "err=" in r.headers["location"]
        page = c.get("/settings/loyalty?calc=1&sp=1000&pp=500").text
        assert "Догляд -10%" in page and "50,00 ₴" in page
        assert "170,00 ₴" in c.get(f"/patients/{pid}").text
    with SessionLocal() as db:
        p = db.get(Patient, pid)
        assert ly.balance(db, pid) == 170
        items = [{"type": "service", "price": 1000, "qty": 1}, {"type": "product", "price": 500, "qty": 1}]
        d = ly.discount_for(db, items, p)
        assert d["total"] == 300 and d["rules"] == ["Іменинникам -20%"]          # найвигідніша одна
        assert ly.discount_for(db, items, None)["total"] == 50
        assert ly.birthday_bonuses(db) == 1 and ly.birthday_bonuses(db) == 0 and ly.balance(db, pid) == 370
        assert ly.max_bonus_pay(db, 1000, pid) == 300
        db.add(BonusTxn(patient_id=pid, amount=40, kind="manual", at=datetime.now() - timedelta(days=400),
                        expires_at=date.today() - timedelta(days=1)))
        db.commit()
        ly.expire(db)
        assert ly.balance(db, pid) == 370                                         # 40 згоріли
        db.query(DiscountRule).delete(); db.commit()
