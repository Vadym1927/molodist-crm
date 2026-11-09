from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.models import SessionLocal, Appointment, BankTxn, Doctor, Patient, Product, StockDoc, StockDocLine, StockMove, StockPayRow, Supplier, SupplierInvoice
from app.services import fop_schedule as fs, stock_paycal as sp


def test_fop_schedule_forecast():
    with SessionLocal() as db:
        d = Doctor(name="Лікар ФОП тест", position="Лікар-косметолог", active=True)
        pt = Patient(full_name="Пацієнт ФОП", phone="+380000000999")
        db.add_all([d, pt]); db.flush()
        day = date.today() + timedelta(days=2)
        db.add(Appointment(patient_id=pt.id, doctor_id=d.id, start=datetime.combine(day, datetime.min.time()).replace(hour=10),
                           end=datetime.combine(day, datetime.min.time()).replace(hour=11), services=[{"name": "Ботулінотерапія", "price": 5600}]))
        db.commit()
        cfg = fs.load(db)
        cfg["weekly"][str(day.weekday())] = {str(d.id): "ФОП Бондар"}
        fs.save(db, cfg)
        assert fs.income_until(db, day)["ФОП Бондар"] >= 5600
        cfg["dates"][day.isoformat()] = {str(d.id): "ФОП Колган"}
        fs.save(db, cfg)
        inc = fs.income_until(db, day)
        assert inc["ФОП Колган"] >= 5600


def test_stock_paycal_flow():
    with SessionLocal() as db:
        sup = Supplier(name="КАЛ ТЕСТ")
        db.add(sup)
        p = Product(name="Крем кал тест", name_norm="kal-t", supplier="КАЛ ТЕСТ", min_stock=5, cost=1000, warehouse="vitrina", active=True)
        db.add(p); db.flush()
        db.add(StockMove(product_id=p.id, warehouse="vitrina", qty=2, kind="opening", at=datetime(2026, 11, 1)))
        db.add(SupplierInvoice(cb_number="KAL1", inv_date=datetime.now() - timedelta(days=30), supplier="КАЛ ТЕСТ", warehouse="vitrina", amount=1000, paid=1000))
        db.commit()
        v = sp.build(db)
        row = next(x for k, t, rows in v["groups"] for x in rows if x["r"].supplier == "КАЛ ТЕСТ")
        assert row["status"] == "waiting" and row["est"] == 3000 and row["rec"] in fs.FOPS
        doc = StockDoc(kind="order", number="ЗТ-1", doc_date=date.today(), status="draft", supplier="КАЛ ТЕСТ")
        doc.lines.append(StockDocLine(product_id=p.id, qty=3, unit_cost=1000))
        db.add(doc); db.commit()
        row = next(x for k, t, rows in sp.build(db)["groups"] for x in rows if x["r"].supplier == "КАЛ ТЕСТ")
        assert row["status"] == "ordered" and row["plan"] == 3000
        r = db.get(StockPayRow, row["r"].id)
        r.fact_amount, r.approved = 3100, True
        db.add(BankTxn(id="kal-bank-1", fop="Бондар", account="x", at=datetime.now(), amount=-3100, counter_name="КАЛ ТЕСТ", supplier_id=sup.id))
        db.commit()
        row = next(x for k, t, rows in sp.build(db)["groups"] for x in rows if x["r"].supplier == "КАЛ ТЕСТ")
        assert row["status"] == "paid" and row["dev"] == 100 and row["r"].paid_ref == "bank:kal-bank-1"


def test_fund_state_targets():
    with SessionLocal() as db:
        fs.set_targets(db, {"ФОП Колган": 190000, "ФОП Бондар": 110000}, 3, date(2026, 11, 9))
        st = fs.fund_state(db, today=date(2026, 11, 9))
        k = st["ФОП Колган"]
        assert k["target"] == 190000 and k["deficit"] == max(190000 - k["oper"], 0) and k["deadline"] == date(2027, 2, 9)
        if k["deficit"]:
            assert 6000 < k["per_week"] * 13 / k["deficit"] * 1000 or k["per_week"] > 0


def test_fund_plan_weeks():
    with SessionLocal() as db:
        fs.set_targets(db, {"ФОП Колган": 190000, "ФОП Бондар": 110000}, 3, date.today())
        p = fs.plan(db)
        k = p["fops"]["ФОП Колган"]
        assert len(p["weeks"]) >= 12 and abs(k["rows"][-1]["end"] - max(k["target"], k["oper"])) < 1


def test_recommendation_spreads_between_fops(monkeypatch):
    """Оплати не всі на один ФОП: кожна наступна «віртуально» списується з обраного й наступна йде на інший."""
    st = {"ФОП Колган": {"oper": 100000.0, "target": 190000.0}, "ФОП Бондар": {"oper": 40000.0, "target": 110000.0}}
    monkeypatch.setattr(fs, "fund_state", lambda db, today=None: {f: dict(v, box=fs.FUND[f], total=v["oper"], amort=0, deficit=0, pct=0,
                                                                            deadline=date.today(), per_month=0, per_week=0) for f, v in st.items()})
    monkeypatch.setattr(fs, "income_until", lambda db, d, today=None: {"ФОП Колган": 0.0, "ФОП Бондар": 0.0})
    with SessionLocal() as db:
        for i in range(4):
            name = f"РОЗКЛАД ТЕСТ {i}"
            db.add(Supplier(name=name))
            db.add(Product(name=f"Препарат розклад {i}", name_norm=f"rozklad-{i}", supplier=name, min_stock=1, cost=900000 + i, warehouse="inject", active=True))
        db.commit()
        v = sp.build(db)
        recs = [x["rec"] for k, t, rows in v["groups"] for x in rows if x["r"].supplier.startswith("РОЗКЛАД ТЕСТ")]
        assert len(recs) == 4 and set(recs) == set(fs.FOPS)
