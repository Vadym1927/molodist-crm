import pytest

from app.models import SessionLocal, Patient, FinOp
from app.services import sales as sl


def test_overpay_to_balance_and_change():
    with SessionLocal() as db:
        p = Patient(full_name="Переплатенко Ірина", phone="+380501110000", cb_balance=0)
        db.add(p)
        db.commit()
        item = [{"kind": "other", "name": "Процедура", "qty": 1, "price": 16995}]
        with pytest.raises(sl.SaleError):
            sl.create(db, {"patient_id": p.id, "items": item, "payments": [{"method": "Готівка", "amount": 17000}]})
        s = sl.create(db, {"patient_id": p.id, "items": item, "payments": [{"method": "Готівка", "amount": 17000}], "overpay": "balance"})
        assert [round(x.amount, 2) for x in s.payments] == [16995] and s.balance_topup == 5
        db.refresh(p)
        assert p.cb_balance == 5
        op = db.query(FinOp).filter_by(ref=f"sale:{s.id}:topup").one()
        assert op.kind == "income" and op.amount == 5 and op.cashbox == "Готівка"
        s2 = sl.create(db, {"patient_id": p.id, "items": item, "payments": [{"method": "Готівка", "amount": 17000}], "overpay": "change"})
        assert s2.payments[0].amount == 16995 and "Решта 5.00" in (s2.comment or "")
        db.refresh(p)
        assert p.cb_balance == 5
        with pytest.raises(sl.SaleError):                    # карткою — лише рівна сума
            sl.create(db, {"patient_id": p.id, "items": item, "payments": [{"method": "Картка (термінал)", "amount": 17000}], "overpay": "balance"})
