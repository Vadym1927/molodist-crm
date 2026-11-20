from datetime import date, timedelta

from app.models import ConsignLot, Product, SessionLocal, Supplier
from app.services import consign as cs, tg_notify


def _setup(db, exp):
    db.add(Supplier(name="EXPTEST", consignment=True))
    p = Product(name="Тест крем EXP", name_norm="тест крем exp", supplier="EXPTEST", brand="EXPTEST", sku="EX1", expiry_date=exp)
    db.add(p); db.flush()
    l = ConsignLot(supplier="EXPTEST", invoice="1", inv_date=date(2026, 1, 1), product_id=p.id, name=p.name, sku="EX1", qty=3, cost=500, sold=0, source="manual")
    db.add(l); db.commit()
    return p, l


def test_expiry_due_and_sell():
    db = SessionLocal()
    today = date(2026, 11, 20)
    p, l = _setup(db, date(2027, 12, 1))       # повернути до 01.12.2026 — за 11 днів
    xs = [x for x in cs.expiry_due(db, "EXPTEST", today=today)]
    assert xs and xs[0]["return_by"] == date(2026, 12, 1) and not xs[0]["overdue"] and xs[0]["qty"] == 3
    assert cs.expiry_due(db, "EXPTEST", today=today - timedelta(days=60)) == []
    assert cs.expiry_due(db, "EXPTEST", today=date(2026, 12, 2))[0]["overdue"]
    txt = tg_notify.text_consign_expiry(db, None, "owner", today)
    assert "Тест крем EXP" in txt and "01.12.2026" in txt
    rep = cs.sell_lot(db, l, 1, today, "Забрав співробітник")
    assert rep.total == 500 and l.sold == 1 and cs.is_manual(rep)
    assert cs.last_report_end(db, "EXPTEST") is None
    assert cs.overview(db, "EXPTEST", today)["to_pay"] == 500
    db.close()
