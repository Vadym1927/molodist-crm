from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appointment, Doctor, Patient, Sale, SaleItem, SalePayment
from app.services import day_stats


def test_day_stats():
    settings.admin_password = "owner-pass-1"
    t = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            d = Doctor(name="Стат Лікар", active=True)
            p1, p2 = Patient(full_name="Новий Пацієнт"), Patient(full_name="Старий Пацієнт")
            db.add_all([d, p1, p2]); db.flush()
            db.add_all([Appointment(patient_id=p2.id, doctor_id=d.id, start=t - timedelta(days=30), end=t - timedelta(days=30, hours=-1), status="done"),
                        Appointment(patient_id=p1.id, doctor_id=d.id, start=t, end=t + timedelta(hours=1), status="booked", source="online",
                                    services=[{"id": 1, "name": "Пілінг", "price": 2000}]),
                        Appointment(patient_id=p2.id, doctor_id=d.id, start=t + timedelta(hours=2), end=t + timedelta(hours=3), status="cancelled",
                                    services=[{"id": 2, "name": "LED", "price": 500}])])
            s = Sale(patient_id=p1.id, status="paid", at=t, subtotal=3300, total=3000, discount=300)
            s.items += [SaleItem(kind="service", name="Пілінг", qty=1, price=2000, discount=200, total=1800, doctor_id=d.id),
                        SaleItem(kind="product", name="Крем", qty=1, price=1000, discount=100, total=900, doctor_id=d.id),
                        SaleItem(kind="other", name="Сертифікат 300", qty=1, price=300, discount=0, total=300, doctor_id=d.id)]
            s.payments.append(SalePayment(method="Готівка", amount=3000))
            db.add(s); db.commit()
            r = day_stats.compute(db, date.today())
        assert r["patients"]["new"] == 1 and r["patients"]["total"] == 1
        assert r["income"]["fact"] == 3000 and r["income"]["plan"] == 2000 and r["income"]["with_discount"] == 1
        assert r["cancel_day"] == {"n": 1, "sum": 500} and r["kinds"]["cert"]["sum"] == 300 and r["kinds"]["product"]["sum"] == 900
        h = c.get(f"/calendar/day-stats?d={date.today()}").text
        assert "Нові пацієнти" in h and "Плановий дохід" in h and "Візитів перенесено" in h and "Стат Лікар" in h
        assert "Статистика дня" in c.get("/calendar").text
