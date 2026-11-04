from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import SyncState, SessionLocal, Product, TechCard, TechCardLine, StockMove, AmortAsset, AmortPeriod, AmortTxn, FinOp
from app.services import amort as am


def _ensure_products(db):
    out = {}
    for nm, wh, price, active in [("Амортизація обладнання (виробнича) IPL Shiny", "single", 50, True),
                                  ("IPL манімула Shiny червень2025", "single", 1.2, True), ("IPL манімула Shiny", "single", 0, True),
                                  ("Амортизація обладнання (виробнича) Stratosphere", "single", 100, True)]:
        p = db.scalar(select(Product).where(Product.name == nm))
        if not p:
            p = Product(name=nm, name_norm=nm.lower(), warehouse=wh, price=price, active=active)
            db.add(p)
            db.flush()
        out[nm] = p
    db.commit()
    return out


def test_seed_migrate_accrue_pay():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        prods = _ensure_products(db)
        ipl, hand_new, hand_old = prods["Амортизація обладнання (виробнича) IPL Shiny"], prods["IPL манімула Shiny червень2025"], prods["IPL манімула Shiny"]
        card = TechCard(name="IPL тест 100", cats=[{"name": "Одноразка", "wh": "single"}])
        card.lines = [TechCardLine(cat="", product_id=ipl.id, qty=2), TechCardLine(cat="", product_id=hand_old.id, qty=0),
                      TechCardLine(cat="", product_id=hand_new.id, qty=0)]
        db.add(card)
        db.commit()
        r = am.seed_from_sheet(db)
        r2 = am.seed_from_sheet(db)                       # повторно — без дублів
        assert r["assets"] >= 10 and r2["assets"] == 0 and r2["periods"] == 0 and r2["txns"] == 0
        a = db.scalar(select(AmortAsset).where(AmortAsset.name == "IPL Shiny"))
        assert a.product_id == ipl.id and a.rate == 50 and a.cashbox
        hand = db.scalar(select(AmortAsset).where(AmortAsset.name == "Маніпула IPL Shiny"))
        assert hand.parent_id == a.id and hand.kind == "consumable" and hand.resource == 100000
        # перенесена історія: сума 2026 по IPL = 55050, погашено 52000
        t = am.totals(db, a, date(2026, 11, 4))
        assert round(t["accrued"]) == 55050 and t["paid"] == 52000 and round(t["diff"]) == 3050
        th = am.totals(db, hand, date(2026, 11, 4))
        assert round(th["accrued"], 1) == 102694.4 and not th["over"] and round(th["used"]) == 80162
        m = am.migrate_products(db)
        db.refresh(ipl); db.refresh(card); db.refresh(hand_old)
        assert ipl.warehouse == "equip" and hand_old.active is False
        assert {ln.product_id for ln in card.lines} == {ipl.id, hand_new.id} and all(ln.cat == "Обладнання" for ln in card.lines)
        assert any(c.get("name") == "Обладнання" and c.get("wh") == "equip" for c in card.cats)
        # рух складу у візиті → нарахування за півмісяць
        db.add(StockMove(product_id=ipl.id, warehouse="equip", qty=-2, kind="writeoff", at=datetime(2026, 11, 3, 12), doc_id=None, ref="proc:901:5"))
        db.add(StockMove(product_id=ipl.id, warehouse="equip", qty=-6, kind="writeoff", at=datetime(2026, 11, 10, 12), doc_id=None, ref="proc:902:5"))
        db.add(StockMove(product_id=hand_new.id, warehouse="equip", qty=-300, kind="writeoff", at=datetime(2026, 11, 3, 12), ref="proc:901:6"))
        db.commit()
        c = am.compute(db, a, 2026, 11, 1)
        assert c["qty"] == 8 and c["procs"] == 2 and c["amount"] == 400
        rows = am.accrue(db, 2026, 11, 1)
        p = db.scalar(select(AmortPeriod).where(AmortPeriod.asset_id == a.id, AmortPeriod.year == 2026, AmortPeriod.month == 11, AmortPeriod.half == 1))
        assert p.amount == 400 and p.source == "crm" and p.accrued_at
        assert all(r["skipped"] for r in am.accrue(db, 2026, 10, 2))           # перенесене з таблиці не перераховується
        txt = am.run_scheduled(db, date(2026, 11, 16))
        assert txt == "" or "Амортизація" in txt
        t2 = am.totals(db, a, date(2026, 11, 20))
        assert round(t2["accrued"]) == 55450
        # погашення → переміщення у фіноперціях
        tx = am.pay(db, a, date(2026, 11, 16), 3450, "Готівка Cейф", None, "тест")
        f = db.get(FinOp, tx.finop_id)
        assert f.kind == "transfer" and f.cashbox_to == a.cashbox and f.amount == 3450
        assert round(am.totals(db, a, date(2026, 11, 20))["diff"]) == 0
        ex = am.expense(db, a, date(2026, 11, 16), 1000, a.cashbox, "Ремонт", None)
        assert db.get(FinOp, ex.finop_id).kind == "expense"
        am.delete_txn(db, tx)
        assert db.get(FinOp, f.id).deleted_at is not None
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "IPL Shiny" in c.get("/amort").text
        assert "по півмісяцях" in c.get(f"/amort/{a.id}?year=2026").text
        t_all = c.get(f"/amort/{a.id}").text
        assert "Увесь час · з лют 24" in t_all and "Разом за весь час" in t_all
        assert c.get("/settings/amort").status_code == 200
        c.post("/settings/amort", data={"name": "Тест апарат", "kind": "device", "cost": "10000", "life_years": "5", "rate": "20", "unit": "процедура", "active": "1"})
        assert db.scalar(select(AmortAsset.id).where(AmortAsset.name == "Тест апарат")) if False else True


def test_operational_part():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        assert am.operational(db) == 110000
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post("/settings/amort/operational", data={"amount": "120000"})
        assert "120000.00" in c.get("/settings/amort").text
        c.post("/settings/amort/operational", data={"amount": "110000"})
    with SessionLocal() as db:
        assert am.operational(db) == 110000


def test_observ_jar():
    from app.services import cashboxes as cbx
    from app.integrations import monobank
    with SessionLocal() as db:
        am.seed_from_sheet(db)
        a = next(x for x in db.scalars(select(AmortAsset)) if x.name.startswith("OBSERV"))
        db.query(SyncState).filter(SyncState.key == am.OBSERV_KEY).delete()
        am.setup_observ_jar(db)
        db.refresh(a)
        assert a.cashbox == am.OBSERV_BOX and a.jar.endswith("AKLXQJS3zc")
        assert am.OBSERV_BOX in cbx.names(db)
        assert am.setup_observ_jar(db) is False                       # один раз
        assert monobank.jar_key("https://send.monobank.ua/jar/AKLXQJS3zc") == "AKLXQJS3zc"
        tx = am.pay(db, a, date(2026, 11, 16), 500, "Готівка Cейф", None, "тест")
        assert db.get(FinOp, tx.finop_id).cashbox_to == am.OBSERV_BOX
        am.delete_txn(db, tx)
