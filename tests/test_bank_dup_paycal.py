from datetime import date, datetime
from types import SimpleNamespace

from sqlalchemy import select

from app.integrations import monobank
from app.models import BankTxn, FinOp, SessionLocal
from app.services import payments as pay


def test_bank_txn_does_not_duplicate_paycal_expense(monkeypatch):
    monkeypatch.setattr(monobank, "cashbox_for", lambda db, acc, fop: "Фонд Дубль Тест")
    g = SimpleNamespace(docs=[SimpleNamespace(number="П-9001", supplier="MERZ")])
    with SessionLocal() as db:
        db.add(FinOp(day=date(2026, 11, 13), kind="expense", category="Закупка товара", amount=6128.5, cashbox="Фонд Дубль Тест",
                     ref="paycal:901", comment="Платіжний календар: Рахунок 89408"))
        t1 = BankTxn(id="dupT1", fop="Бондар", account="a1", at=datetime(2026, 11, 13, 15, 42), amount=-6128.5)
        t2 = BankTxn(id="dupT2", fop="Бондар", account="a1", at=datetime(2026, 11, 14, 10, 0), amount=-6128.5)
        db.add_all([t1, t2]); db.commit()
        q = select(FinOp).where(FinOp.cashbox == "Фонд Дубль Тест", FinOp.deleted_at.is_(None))
        pay._book_txn(db, t1, 6128.5, g); db.commit()
        ops = db.scalars(q).all()
        assert len(ops) == 1 and "[bank:dupT1]" in ops[0].comment          # друга витрата не створена
        pay._book_txn(db, t1, 6128.5, g); db.commit()
        assert len(db.scalars(q).all()) == 1                                # повторне рознесення — теж без дубля
        pay._book_txn(db, t2, 6128.5, g); db.commit()
        ops = db.scalars(q).all()
        assert len(ops) == 2 and any(o.ref == "banktxn:dupT2" for o in ops)  # інше списання на ту саму суму — окрема витрата
