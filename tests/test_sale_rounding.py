from app.models import SessionLocal
from app.services import sales as sl


def test_total_rounded_to_hryvnia():
    with SessionLocal() as db:
        q = sl.quote(db, [{"kind": "other", "name": "A", "qty": 1, "price": 14150}], None, manual_discount=712.5)
        assert q["total"] == 13437 and q["discount"] == 713 and "Заокруглення до гривні" in q["rules"]
        q = sl.quote(db, [{"kind": "other", "name": "B", "qty": 1, "price": 100.5}], None)
        assert q["total"] == 100.5                            # ціна з копійками без знижки — не чіпаємо
        q = sl.quote(db, [{"kind": "other", "name": "A", "qty": 1, "price": 2650}], None)
        assert q["total"] == 2650 and q["discount"] == 0 and "Заокруглення до гривні" not in q["rules"]


def test_pay_whole_hryvnias_for_kopeck_price():
    with SessionLocal() as db:
        s = sl.create(db, {"items": [{"kind": "other", "name": "A", "qty": 1, "price": 5000}, {"kind": "other", "name": "B", "qty": 1, "price": 0.01}],
                           "payments": [{"method": "Готівка", "amount": 5000}]})
        assert s.status == "paid" and s.total == 5000 and s.discount == 0.01
