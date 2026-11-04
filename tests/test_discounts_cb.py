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


def test_per_line_discount_and_applicable():
    with SessionLocal() as db:
        a = DiscountRule(name="Тест -10% послуги", kind="percent", value=10, applies_to="services", active=True, auto=False, tag="Постійні акції")
        b = DiscountRule(name="Тест -50% товари", kind="percent", value=50, applies_to="products", active=True, auto=False)
        db.add_all([a, b]); db.commit()
        items = [{"type": "service", "name": "S", "price": 1000, "qty": 1}, {"type": "product", "name": "P", "price": 400, "qty": 1}]
        d = ly.discount_for(db, items, None, rule_ids=[])
        assert d["applicable"][a.id] == [0] and d["applicable"][b.id] == [1]
        items[0]["rule_id"], items[1]["rule_id"] = a.id, b.id
        d = ly.discount_for(db, items, None, rule_ids=[])
        assert d["lines"] == [100, 200] and set(d["rules"]) == {a.name, b.name}
        items[1]["rule_id"] = a.id                                   # не діє на товар — ігнорується
        assert ly.discount_for(db, items, None, rule_ids=[])["lines"] == [100, 0]
        db.delete(a); db.delete(b); db.commit()


def test_pos_with_untagged_discount():
    """Знижка без розділу поруч зі знижками з розділами — каса не падає (групування None vs str)."""
    from app.models import SessionLocal, DiscountRule
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add(DiscountRule(name="Без розділу тест", kind="percent", value=5, active=True, auto=False, tag=None))
        db.add(DiscountRule(name="З розділом тест", kind="percent", value=7, active=True, auto=False, tag="Акції"))
        db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.get("/sales/new")
        assert r.status_code == 200 and "Без розділу тест" in r.text and "З розділом тест" in r.text
