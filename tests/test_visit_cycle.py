"""Повний цикл візиту в CRM: послуга з техкартою → запис → витратні візиту (змінити, додати, прибрати) →
оплата в касі закриває візит і списує матеріали по складах → з/п лікаря від фактичної собівартості → звіт → повернення."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import (SessionLocal, Appointment, CbService, Doctor, Patient, Product, ProductPurchase, StockDoc, StockMove,
                        TechCard, TechCardLine, VisitMaterial)
from app.services import salary_plans as spl, payroll_docs as pd, reports as rp, techcards as tc


def test_visit_cycle():
    settings.admin_password = "owner-pass-1"
    start = (datetime.now() + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        xs = spl.items(db)
        pl = {"id": 971, **spl.blank("Цикловий оклад")}
        pl["comm"].update(on=True, services=[20.0, "paid"], sub_cost=True, cost_src="techcard")
        spl._save(db, xs + [pl])
        d = Doctor(name="Циклова Лікарка", active=True, extra={"salary": "Цикловий оклад"})
        pt = Patient(full_name="Циклова Пацієнтка", phone="+380501112244")
        drug = Product(name="Цикл Препарат", name_norm="цикл препарат", warehouse="inject", unit="шт.", cost=1000)
        glove = Product(name="Цикл Рукавички", name_norm="цикл рукавички", warehouse="single", unit="шт.", cost=2)
        amort = Product(name="Цикл Амортизація апарата", name_norm="цикл амортизація апарата", warehouse="equip", unit="шт.", cost=50)
        cotton = Product(name="Цикл Ватні диски", name_norm="цикл ватні диски", warehouse="single", unit="шт.", cost=0.5)
        db.add_all([d, pt, drug, glove, amort, cotton]); db.flush()
        db.add(ProductPurchase(product_id=drug.id, bought_on=date(2026, 10, 1), qty=10, unit_cost=1200))   # остання ціна закупівлі
        card = TechCard(name="Цикл техкарта", cats=list(tc.DEFAULT_CATS), max_cost=1400,
                        lines=[TechCardLine(cat="Препарат", product_id=drug.id, qty=1), TechCardLine(cat="Одноразка", product_id=glove.id, qty=2),
                               TechCardLine(cat="Обладнання", product_id=amort.id, qty=1)])
        db.add(card); db.flush()
        db.add(CbService(id=9501, name="Циклова процедура", category="Біоревіталізація", price=5000, time_min=60, techcard_id=card.id))
        db.commit()
        ids = dict(d=d.id, pt=pt.id, drug=drug.id, glove=glove.id, amort=amort.id, cotton=cotton.id, card=card.id)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Циклова процедура" in c.get("/services").text and "Цикл техкарта" in c.get("/techcards").text
        r = c.post("/appointments", json={"patient_id": ids["pt"], "doctor_id": ids["d"], "start": start.isoformat(timespec="minutes"),
                                          "services": [{"id": 9501, "name": "Циклова процедура", "price": 5000, "minutes": 60}], "force": True})
        aid = r.json()["id"]
        with SessionLocal() as db:
            ms = {m.product_id: m for m in db.query(VisitMaterial).filter_by(appt_id=aid)}
            assert set(ms) == {ids["drug"], ids["glove"], ids["amort"]}
            assert ms[ids["drug"]].unit_cost == 1200 and ms[ids["drug"]].warehouse == "inject" and ms[ids["amort"]].warehouse == "equip"
            gid, aid_m = ms[ids["glove"]].id, ms[ids["amort"]].id
        h = c.get(f"/calendar/visit?appt={aid}&tab=cost").text
        assert "Цикл техкарта" in h and "Собівартість візиту" in h and "1 254.00" in h       # 1200 + 2×2 + 50
        # змінити, додати, прибрати
        c.post(f"/appointments/{aid}/materials/{gid}", json={"qty": "4"})
        c.post(f"/appointments/{aid}/materials", json={"svc_idx": 0, "product": f"Цикл Ватні диски [#{ids['cotton']}]", "qty": "6", "cat": "Одноразка"})
        c.post(f"/appointments/{aid}/materials/{aid_m}", json={"delete": 1})
        with SessionLocal() as db:
            assert tc.cost_by_service(db, aid) == {0: 1200 + 8 + 3}
        # оплата в касі → візит закрито, матеріали списано по складах
        r = c.post("/sales", json={"patient_id": ids["pt"], "appointment_id": aid,
                                   "items": [{"kind": "service", "ref_id": 9501, "name": "Циклова процедура", "qty": 1, "price": 5000, "doctor_id": ids["d"]}],
                                   "payments": [{"method": "Готівка", "amount": 5000}]})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        with SessionLocal() as db:
            docs = db.query(StockDoc).filter(StockDoc.ext_no == f"visit:{aid}", StockDoc.status == "posted").all()
            assert sorted(x.wh_from for x in docs) == ["inject", "single"]
            mv = {(m.product_id, m.warehouse): m.qty for m in db.query(StockMove).filter(StockMove.doc_id.in_([x.id for x in docs]))}
            assert mv[(ids["drug"], "inject")] == -1 and mv[(ids["glove"], "single")] == -4 and mv[(ids["cotton"], "single")] == -6
            assert db.get(Appointment, aid).status == "done"
            # з/п: 20% × (5000 − 1211) — собівартість з фактичних витратних візиту
            row = {r_["doctor"].name: r_ for r_ in pd.compute(db, start.date().replace(day=1), start.date())}
            dd = start.date()
            row = {r_["doctor"].name: r_ for r_ in pd.compute(db, date.today() - timedelta(days=1), date.today() + timedelta(days=1))}
            assert row["Циклова Лікарка"]["detail"]["comm_services"] == round((5000 - 1211) * 0.2, 2)
            rep = rp.build(db, "services", date.today() - timedelta(days=1), date.today() + timedelta(days=1))
            z = next(x for x in rep.rows if x["name"] == "Циклова процедура")
            assert z["cost"] == 1211 and z["salary"] == round((5000 - 1211) * 0.2, 2)
        # закритий візит не редагується
        assert c.post(f"/appointments/{aid}/materials/{gid}", json={"qty": "9"}).status_code == 409
        assert "матеріали списано зі складу" in c.get(f"/calendar/visit?appt={aid}&tab=cost").text
        # повернення чека — списання скасовано, візит відкрито
        from app.services import sales as sl
        from app.models import Sale
        with SessionLocal() as db:
            sl.refund(db, db.get(Sale, sid), "тест повернення")
            assert tc.sync(db)["reopened"] == 2
            assert not db.query(StockDoc).filter(StockDoc.ext_no == f"visit:{aid}", StockDoc.status == "posted").count()
