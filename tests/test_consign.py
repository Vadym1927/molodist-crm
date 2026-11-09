from datetime import date, datetime

from sqlalchemy import select

from app.models import SessionLocal, ConsignLot, ConsignReport, Product, StockMove, Supplier, SupplierInvoice
from app.services import consign as cs


def test_fifo_report_and_pay():
    with SessionLocal() as db:
        db.add(Supplier(name="КОНС ТЕСТ", consignment=True))
        p = Product(name="Крем конс тест", name_norm="крем конс тест", supplier="КОНС ТЕСТ")
        db.add(p)
        db.add_all([SupplierInvoice(cb_number="K1", inv_date=datetime(2025, 3, 1), supplier="КОНС ТЕСТ", amount=3000, paid=0),
                    SupplierInvoice(cb_number="K2", inv_date=datetime(2026, 9, 1), supplier="КОНС ТЕСТ", amount=4400, paid=0)])
        db.flush()
        db.add_all([ConsignLot(supplier="КОНС ТЕСТ", invoice="K2", inv_date=date(2026, 9, 1), product_id=p.id, name=p.name, qty=4, cost=1100),
                    ConsignLot(supplier="КОНС ТЕСТ", invoice="K1", inv_date=date(2025, 3, 1), product_id=p.id, name=p.name, qty=3, cost=1000)])
        for d in (5, 6, 7, 8):
            db.add(StockMove(product_id=p.id, warehouse="vitrina", qty=-1, kind="sale", at=datetime(2026, 11, d, 12)))
        db.commit()
        r = cs.build_report(db, "КОНС ТЕСТ", date(2026, 11, 1), date(2026, 11, 10))
        al = r["lines"][0]["alloc"]
        assert [(a["invoice"], a["qty"]) for a in al] == [("K1", 3), ("K2", 1)] and r["total"] == 4100
        rep = cs.fix_report(db, "КОНС ТЕСТ", date(2026, 11, 1), date(2026, 11, 10))
        assert cs.build_report(db, "КОНС ТЕСТ", date(2026, 11, 11), date(2026, 11, 12))["total"] == 0
        pay = cs.pay(db, "КОНС ТЕСТ", 3500, date(2026, 11, 12))
        inv = {x.cb_number: x for x in db.scalars(select(SupplierInvoice).where(SupplierInvoice.supplier == "КОНС ТЕСТ"))}
        assert inv["K1"].crm_paid == 3000 and inv["K1"].debt == 0 and inv["K2"].crm_paid == 500 and inv["K2"].debt == 3900
        assert db.get(ConsignReport, rep.id).paid == 3500
        v = cs.overview(db, "КОНС ТЕСТ")
        assert v["on_hand"] == 3300 and v["to_pay"] == 600
        cs.cancel_pay(db, pay)
        assert inv["K1"].debt == 3000
        cs.cancel_report(db, db.get(ConsignReport, rep.id))
        assert cs.overview(db, "КОНС ТЕСТ")["on_hand"] == 7400


def test_seed_file():
    import json, os
    d = json.load(open(os.path.join(os.path.dirname(cs.__file__), "consign_seed.json")))
    tb = sum(x["qty"] * x["cost"] for x in d["lots"] if x["supplier"] == "TEBISKIN")
    ag = sum(x["qty"] * x["cost"] for x in d["lots"] if x["supplier"] == "AG SKIN")
    assert round(tb, 2) == 30839 + 5044 and round(ag, 2) == 56714.15
