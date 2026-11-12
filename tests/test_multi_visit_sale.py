"""Один чек на два візити пацієнта до різних лікарів + товар: обидва візити закриваються, матеріали списуються,
з/п кожному лікарю — за його послугою."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appointment, CbService, Doctor, Patient, Product, Sale, StockDoc, TechCard, TechCardLine
from app.services import salary_plans as spl, payroll_docs as pd, techcards as tc, sales as sl


def test_two_visits_one_check():
    settings.admin_password = "owner-pass-1"
    t0 = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        xs = spl.items(db)
        pl = {"id": 981, **spl.blank("Двовізитний оклад")}
        pl["comm"].update(on=True, services=[10.0, "paid"], products=[10.0, "paid"])
        spl._save(db, xs + [pl])
        d1 = Doctor(name="Перша Лікарка", active=True, extra={"salary": "Двовізитний оклад"})
        d2 = Doctor(name="Друга Лікарка", active=True, extra={"salary": "Двовізитний оклад"})
        pt = Patient(full_name="Двовізитна Пацієнтка")
        mat = Product(name="Двовізит матеріал", name_norm="двовізит матеріал", warehouse="single", cost=10)
        cream = Product(name="Двовізит крем", name_norm="двовізит крем", warehouse="vitrina", cost=500, price_manual=1500)
        db.add_all([d1, d2, pt, mat, cream]); db.flush()
        card = TechCard(name="Двовізит техкарта", cats=list(tc.DEFAULT_CATS), lines=[TechCardLine(cat="Одноразка", product_id=mat.id, qty=3)])
        db.add(card); db.flush()
        db.add_all([CbService(id=9601, name="Процедура А", price=2000, techcard_id=card.id), CbService(id=9602, name="Процедура Б", price=3000)])
        db.commit()
        ids = dict(d1=d1.id, d2=d2.id, pt=pt.id, cream=cream.id)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        a1 = c.post("/appointments", json={"patient_id": ids["pt"], "doctor_id": ids["d1"], "start": t0.isoformat(timespec="minutes"), "force": True,
                                           "services": [{"id": 9601, "name": "Процедура А", "price": 2000, "minutes": 60}]}).json()["id"]
        a2 = c.post("/appointments", json={"patient_id": ids["pt"], "doctor_id": ids["d2"], "start": (t0 + timedelta(hours=1)).isoformat(timespec="minutes"),
                                           "force": True, "services": [{"id": 9602, "name": "Процедура Б", "price": 3000, "minutes": 30}]}).json()["id"]
        h = c.get(f"/sales/new?appt={a1}").text
        assert f"＋ Візит №{a2}" in h                                   # підказка додати другий візит
        h = c.get(f"/sales/new?appt={a1},{a2}").text
        assert "Перша Лікарка" in h and "Друга Лікарка" in h and "9602" in h
        assert f"Разом з іншими візитами (2)" in c.get(f"/calendar/visit?appt={a1}").text
        r = c.post("/sales", json={"patient_id": ids["pt"], "appointment_ids": [a1, a2], "items": [
            {"kind": "service", "ref_id": 9601, "name": "Процедура А", "qty": 1, "price": 2000, "doctor_id": ids["d1"]},
            {"kind": "service", "ref_id": 9602, "name": "Процедура Б", "qty": 1, "price": 3000, "doctor_id": ids["d2"]},
            {"kind": "product", "ref_id": ids["cream"], "name": "Двовізит крем", "qty": 1, "price": 1500, "doctor_id": ids["d2"]}],
            "payments": [{"method": "Готівка", "amount": 6500}]})
        assert r.status_code == 200, r.text
        sid = r.json()["id"]
        # той самий візит другим чеком — не можна
        r2 = c.post("/sales", json={"patient_id": ids["pt"], "appointment_ids": [a2], "items": [
            {"kind": "service", "ref_id": 9602, "name": "Процедура Б", "qty": 1, "price": 3000, "doctor_id": ids["d2"]}], "payments": [{"method": "Готівка", "amount": 3000}]})
        assert r2.status_code == 400 and "уже розраховано" in r2.text
    with SessionLocal() as db:
        assert db.get(Appointment, a1).sale_id == sid and db.get(Appointment, a2).sale_id == sid
        assert db.get(Appointment, a1).status == db.get(Appointment, a2).status == "paid"
        assert not db.query(StockDoc).filter(StockDoc.ext_no == f"visit:{a1}", StockDoc.status == "posted").count()   # ще не завершено
    c.post(f"/appointments/{a1}/status", data={"status": "done"})
    with SessionLocal() as db:
        assert db.query(StockDoc).filter(StockDoc.ext_no == f"visit:{a1}", StockDoc.status == "posted").count() == 1
        rows = {r_["doctor"].name: r_ for r_ in pd.compute(db, date.today() - timedelta(days=1), date.today() + timedelta(days=1))}
        assert rows["Перша Лікарка"]["calc"] == 200 and rows["Друга Лікарка"]["calc"] == 300 + 150
        sl.refund(db, db.get(Sale, sid), "тест")
        tc.sync(db)
        assert db.get(Appointment, a1).sale_id is None and db.get(Appointment, a2).sale_id is None
        assert not db.query(StockDoc).filter(StockDoc.ext_no == f"visit:{a1}", StockDoc.status == "posted").count()
