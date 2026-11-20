from datetime import date, datetime

from app.models import Patient, ProductSale, SessionLocal
from app.services import reports as rp


def test_dup_sales_report():
    with SessionLocal() as db:
        p = Patient(full_name="Дублева Ольга", phone="+380501119901")
        db.add(p); db.flush()
        at = datetime(2026, 9, 3, 12)
        db.add_all([ProductSale(external_id="c1-5-2026-09-03", patient_id=p.id, product_name="Крем Х", quantity=1, amount=900, sold_at=at),
                    ProductSale(external_id="c1-5-03.09.2026", patient_id=p.id, product_name="Крем Х", quantity=1, amount=900, sold_at=at),
                    ProductSale(external_id="c2-6", patient_id=p.id, product_name="Тонік", quantity=1, amount=500, sold_at=at)])
        db.commit()
        r = rp.build(db, "dup_sales", date(2026, 9, 1), date(2026, 9, 30)) if hasattr(rp, "build") else rp.dup_sales(db, date(2026, 9, 1), date(2026, 9, 30))
        rows = [x for x in r.rows if x["patient"] == "Дублева Ольга"]
        assert len(rows) == 1 and rows[0]["n"] == 2 and rows[0]["extra"] == 900
