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


def test_act_parse_and_compare(tmp_path):
    from openpyxl import Workbook
    from app.services import consign_act as ca
    with SessionLocal() as db:
        db.add(Supplier(name="АКТ ТЕСТ", consignment=True))
        db.add(SupplierInvoice(cb_number="A1", inv_date=datetime(2026, 9, 4), supplier="АКТ ТЕСТ", amount=2704, paid=0))
        db.add_all([ConsignLot(supplier="АКТ ТЕСТ", invoice="A1", inv_date=date(2026, 9, 4), name="Крем SOOTH CREAM 50 мл", qty=2, cost=1352),
                    ConsignLot(supplier="АКТ ТЕСТ", invoice="A1", inv_date=date(2026, 9, 4), name="Емульсія SOOTH-CLEAN 200 мл", qty=1, cost=988)])
        db.commit()
        wb = Workbook()
        ws = wb.active
        ws.append(["Акт звірки взаєморозрахунків"])
        ws.append([])
        ws.append(["№", "Найменування товару", "Кількість", "Ціна", "Сума"])
        ws.append([1, "Крем SOOTH CREAM 50мл", 2, 1352, 2704])
        ws.append([2, "Емульсія SOOTH-CLEAN 200 мл", 2, 988, 1976])
        ws.append([None, "Разом", 4, None, 4680])
        f = tmp_path / "akt.xlsx"
        wb.save(f)
        parsed = ca.parse(ca.read_rows(str(f)))
        assert len(parsed["goods"]) == 2
        cmp = ca.compare(db, "АКТ ТЕСТ", parsed)
        bad = [g for g in cmp["goods"] if not g["ok"]]
        assert len(bad) == 1 and bad[0]["dq"] == 1 and bad[0]["ds"] == 988
        assert ca.to_xlsx(cmp["act"])[:2] == b"PK"
        rows = [["Дата", "Документ", "Дебет", "Кредит"], ["04.09.2026", "Видаткова накладна №5", "2704,00", ""], ["10.09.2026", "Оплата", "", "1000"]]
        p2 = ca.parse(rows)
        c2 = ca.compare(db, "АКТ ТЕСТ", p2)
        assert c2["docs"][0]["ok"] and not c2["docs"][1]["ok"] and c2["s_debt"] == 1704
