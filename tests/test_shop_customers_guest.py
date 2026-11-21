from datetime import datetime

from app.models import SessionLocal, SiteOrder
from app.services import shop_customers as sc


def test_guest_order_in_customers():
    with SessionLocal() as db:
        db.add(SiteOrder(source="crm", ext_id="G-1", number="G-1", created_at=datetime.now(), status="Оплачено", paid=True, total=3588,
                         name="Гість Тестовий", phone="+380501239999"))
        db.commit()
        r = sc.rows(db, "Гість Тестовий")
        assert len(r["rows"]) == 1 and r["rows"][0]["guest"] and r["rows"][0]["sum"] == 3588 and r["rows"][0]["paid"] == 1
        assert sc.rows(db, "Гість Тестовий", flt="nobuy")["rows"] == []
