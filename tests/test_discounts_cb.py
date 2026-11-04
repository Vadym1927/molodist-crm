from datetime import date, datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, DiscountRule, Sale
from app.services import loyalty as ly


def test_cb_discounts_import_form_and_rules():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.post("/settings/loyalty/import-discounts", follow_redirects=False)
        assert r.status_code == 303
        c.post("/settings/loyalty/import-discounts")                       # повторно — без дублів
        with SessionLocal() as db:
            rs = db.scalars(__import__("sqlalchemy").select(DiscountRule).where(DiscountRule.note.like("CleverBox #%"))).all()
            names = [x.name for x in rs]
            assert len(names) == len(set(names)) >= len(ly.CB_DISCOUNTS)
            assert all(not x.auto for x in rs)
            assert ly.cb_pending(db) == 0
            bt = next(x for x in rs if x.name.startswith("-40% Ботулінотерапія"))
            assert bt.tag == "Співробітник" and bt.client_groups == ["Співробітник"] and len(bt.services) > 50
            le = next(x for x in rs if x.name == "-15 % Лазер Епіляція")
            assert le.applies_to == "services" and le.categories and le.client_groups == ["-30% ЛЕ"]
            assert ly.item_ok(le, {"type": "service", "category": "Лазерна епіляція (-50% на перший візит від повної вартості)", "price": 1})
        page = c.get("/settings/loyalty?tag=" + "Співробітник").text
        assert "-40% Ботулінотерапія (Співробітники)" in page and "-15% ЗСУ" not in page
        assert "Перенести з CleverBox" not in page
        # нова знижка за макетом: тег, ліміт на пацієнта, дні з годинами, перелік послуг
        c.post("/settings/loyalty/discount", data={"name": "Тест ранок -25%", "kind": "percent", "value": "25", "tag": "Сезонні акції",
                                                   "date_from": "2026-01-01", "per_client_limit": "1", "unlimited": "1",
                                                   "wd_on": "1", "wd0": "1", "wf0": "09:00", "wt0": "12:00",
                                                   "services": ["Чистка обличчя"], "active": "1"})
        with SessionLocal() as db:
            rr = db.scalars(__import__("sqlalchemy").select(DiscountRule).where(DiscountRule.name == "Тест ранок -25%")).one()
            assert rr.tag == "Сезонні акції" and rr.per_client_limit == 1 and rr.services == ["Чистка обличчя"]
            assert rr.weekdays == [0] and rr.time_windows == {"0": ["09:00", "12:00"]} and rr.applies_to == "services"
            rid = rr.id
        assert "Тест ранок -25%" in c.get(f"/settings/loyalty?edit={rid}").text
    with SessionLocal() as db:
        rr = db.get(DiscountRule, rid)
        p = Patient(full_name="Знижка Тест"); db.add(p); db.commit()
        mon = date(2026, 11, 2)                                              # понеділок
        items = [{"type": "service", "name": "Чистка обличчя", "price": 1000, "qty": 1},
                 {"type": "service", "name": "Інша послуга", "price": 1000, "qty": 1}]
        d = ly.discount_for(db, items, p, mon, [rid], at=datetime(2026, 11, 2, 10, 0))
        assert d["lines"] == [250, 0]
        assert ly.discount_for(db, items, p, mon, [rid], at=datetime(2026, 11, 2, 13, 0))["total"] == 0   # поза годинами
        assert ly.discount_for(db, items, p, date(2026, 11, 3), [rid])["total"] == 0                    # вівторок
        db.add(Sale(patient_id=p.id, total=750, status="paid", rules=["Тест ранок -25%"])); db.commit()
        assert ly.discount_for(db, items, p, mon, [rid], at=datetime(2026, 11, 2, 10, 0))["total"] == 0   # ліміт 1 на пацієнта
        assert rr not in ly.available(db, p, mon)
        db.delete(rr); db.commit()
