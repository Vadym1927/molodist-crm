from datetime import date

from app.models import SessionLocal, BankTxn
from app.integrations import monobank


def test_mono_history(monkeypatch):
    with SessionLocal() as db:
        monkeypatch.setattr(monobank, "saved", lambda db: [{"fop": "Колган", "token": "t", "watch": ["acc1"]}])
        monkeypatch.setattr(monobank, "_save", lambda db, items: None)
        calls = []

        def getter(path, token):
            calls.append(path)
            if len(calls) == 2:
                raise RuntimeError("period unavailable")
            return [{"id": f"tx{len(calls)}", "time": 1700000000, "amount": -12345, "counterName": "Постачальник"},
                    {"id": f"in{len(calls)}", "time": 1700000000, "amount": 50000}]
        r = monobank.history(db, date(2024, 1, 1), date(2024, 4, 1), getter=getter, sleep=lambda s: None)
        assert len(calls) == 3 and "періодів з помилкою 1" in r["Колган"]
        assert db.query(BankTxn).count() == 2
        monobank.history(db, date(2024, 1, 1), date(2024, 4, 1), getter=lambda p, t: [{"id": "tx1", "time": 1700000000, "amount": -1}], sleep=lambda s: None)
        assert db.query(BankTxn).count() == 2
