from datetime import date

import pytest

from app.models import SessionLocal, Patient
from app.services import finops as fo, sales as sl


def test_balance_topup_sale():
    with SessionLocal() as db:
        p = Patient(full_name="Поповненко Олена", phone="+380501110077", cb_balance=0)
        db.add(p); db.commit()
        top = [{"kind": "topup", "name": "Поповнення балансу", "qty": 1, "price": 3000}]
        with pytest.raises(sl.SaleError):                          # без пацієнта
            sl.create(db, {"items": top, "payments": [{"method": "Готівка", "amount": 3000}]})
        with pytest.raises(sl.SaleError):                          # разом із послугою — ні
            sl.create(db, {"patient_id": p.id, "items": top + [{"kind": "other", "name": "X", "qty": 1, "price": 100}],
                           "payments": [{"method": "Готівка", "amount": 3100}]})
        with pytest.raises(sl.SaleError):                          # рахунком пацієнта — ні
            sl.create(db, {"patient_id": p.id, "items": top, "payments": [{"method": sl.BALANCE, "amount": 3000}]})
        s = sl.create(db, {"patient_id": p.id, "items": top, "payments": [{"method": "Готівка", "amount": 3000}]})
        db.refresh(p)
        assert p.cb_balance == 3000 and s.discount == 0 and not s.bonus_accrued
        sl.apply_topup(db, s); db.refresh(p)
        assert p.cb_balance == 3000                               # повторно не нараховує
        rev = [c for c in fo.crm_checks(db, date.today(), date.today(), revenue=True) if c.id == f"Ч-{s.id}"]
        assert not rev                                            # аванс — не виручка
        assert any(c.id == f"Ч-{s.id}" for c in fo.crm_checks(db, date.today(), date.today()))
        s2 = sl.create(db, {"patient_id": p.id, "items": [{"kind": "other", "name": "Процедура", "qty": 1, "price": 1000}],
                            "payments": [{"method": sl.BALANCE, "amount": 1000}]})
        db.refresh(p)
        assert p.cb_balance == 2000 and s2.status == "paid"
        with pytest.raises(sl.SaleError):                          # поповнення частково витрачено — повернути все не можна
            sl.refund(db, s, "тест")
        sl.refund(db, s2, "тест"); db.refresh(p)
        sl.refund(db, s, "тест"); db.refresh(p)
        assert p.cb_balance == 0
