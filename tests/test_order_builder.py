from datetime import datetime

from app.models import SessionLocal, Product, StockMove, StockDoc
from app.services import order_builder as ob


def test_build_rows_and_create():
    with SessionLocal() as db:
        a = Product(name="Крем ОБ тест SOOTH CREAM 50 мл", name_norm="ob-a", supplier="ОБ ТЕСТ", min_stock=10, cost=1352, warehouse="vitrina", active=True)
        b = Product(name="Засіб ОБ тест OSK-CLEAN 200 мл", name_norm="ob-b", supplier="ОБ ТЕСТ", min_stock=2, cost=1326, warehouse="vitrina", active=True)
        c = Product(name="Крем ОБ тест LC 50 мл", name_norm="ob-c", supplier="ОБ ТЕСТ", min_stock=2, cost=1862, warehouse="vitrina", active=True)
        db.add_all([a, b, c]); db.flush()
        db.add_all([StockMove(product_id=a.id, warehouse="vitrina", qty=9, kind="opening", at=datetime(2026, 11, 1)),
                    StockMove(product_id=b.id, warehouse="vitrina", qty=1, kind="opening", at=datetime(2026, 11, 1)),
                    StockMove(product_id=c.id, warehouse="vitrina", qty=2, kind="opening", at=datetime(2026, 11, 1))])
        db.commit()
        rs = {r["p"].id: r for r in ob.rows(db, "ОБ ТЕСТ")}
        assert rs[a.id]["need"] == 1 and rs[b.id]["need"] == 1 and rs[c.id]["need"] == 0
        assert rs[a.id]["sum"] + rs[b.id]["sum"] == 2678
        rs2 = {r["p"].id: r for r in ob.rows(db, "ОБ ТЕСТ", facts={a.id: 5})}
        assert rs2[a.id]["need"] == 5
        doc = ob.create(db, "ОБ ТЕСТ", [(a.id, 1, 1352), (b.id, 1, 1326), (c.id, 0, 1862)])
        assert doc.kind == "order" and len(doc.lines) == 2 and doc.supplier == "ОБ ТЕСТ"
