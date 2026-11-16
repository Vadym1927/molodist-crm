from datetime import date, datetime, timedelta

from app.models import SessionLocal, AmortAsset, CbService, Product, Sale, SaleItem, StockMove, TechCard, TechCardLine
from app.services import reports as rp


def test_amort_reports():
    t = date.today()
    now = datetime.now()
    with SessionLocal() as db:
        pr = Product(name="Амортизація Тест-апарат", name_norm="АМОРТИЗАЦІЯ ТЕСТ-АПАРАТ", unit="шт", warehouse="equip")
        pm = Product(name="Маніпула тест", name_norm="МАНІПУЛА ТЕСТ", unit="шт", warehouse="equip")
        db.add_all([pr, pm])
        db.flush()
        card = TechCard(name="Масаж тест-апаратом")
        db.add(card)
        db.flush()
        db.add(TechCardLine(card_id=card.id, product_id=pr.id, qty=1, cat="Обладнання"))
        sv = CbService(name="Масаж тест-апаратом 50 хв", price=1800, techcard_id=card.id)
        db.add(sv)
        dev = AmortAsset(name="Тест-апарат", kind="device", product_id=pr.id, cost=100000, life_years=5, rate=100, avg_month=20)
        db.add(dev)
        db.flush()
        con = AmortAsset(name="Маніпула тест", kind="consumable", product_id=pm.id, parent_id=dev.id, cost=30000, resource=1000, unit="імпульс",
                         rate=30, used_before=700, accrued_before=21000)
        db.add(con)
        for i in range(2):
            s = Sale(at=now - timedelta(hours=1 + i), status="paid", subtotal=1800, total=1800)
            s.items.append(SaleItem(kind="service", ref_id=sv.id, name=sv.name, qty=1, price=1800, total=1800))
            db.add(s)
        db.add(StockMove(product_id=pr.id, warehouse="equip", qty=-1, kind="procedure", at=now - timedelta(hours=1), ref="visit:1"))
        db.add(StockMove(product_id=pm.id, warehouse="equip", qty=-150, kind="procedure", at=now - timedelta(minutes=30), ref="visit:2"))
        db.commit()
        d = rp.build(db, "devices", t, t)
        row = next(x for x in d.rows if x["name"] == "Тест-апарат")
        assert row["procs"] == 2 and row["wr"] == 1 and row["rev"] == 3600 and row["am"] == 100 and row["per"] == 1800 and row["cost"] == 100000
        assert d.tiles[3][1] >= 1
        c = rp.build(db, "consumables", t, t)
        row = next(x for x in c.rows if x["name"] == "Маніпула тест")
        assert row["dev"] == "Тест-апарат" and row["pace"] == 50 and row["left"] == 150 and row["months"] == 3 and row["st"] == "заміна до 3 міс"
        assert row["fund"] and c.tiles[2][1] >= 1
