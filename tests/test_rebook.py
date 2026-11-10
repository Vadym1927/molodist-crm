from datetime import date, datetime, timedelta

from app.models import SessionLocal, Appointment, Doctor, Patient, Visit
from app.services import rebook


def test_not_rebooked_report():
    today = date.today()
    with SessionLocal() as db:
        d = Doctor(name="Лікар Перезапис Тест", position="Лікар-косметолог", active=True)
        a = Patient(full_name="Перезапис Без Запису", phone="+380000000901")
        b = Patient(full_name="Перезапис Із Записом", phone="+380000000902")
        db.add_all([d, a, b]); db.flush()
        t = datetime.combine(today - timedelta(days=5), datetime.min.time()).replace(hour=11)
        for p in (a, b):
            db.add(Visit(patient_id=p.id, source="cb_service_sale", external_id=f"rb-{p.id}", starts_at=t, status="done",
                         master_name=d.name, service_name="Гігієна", amount=1000))
        db.add(Appointment(patient_id=b.id, doctor_id=d.id, start=datetime.now() + timedelta(days=3), end=datetime.now() + timedelta(days=3, hours=1),
                           services=[], status="booked"))
        db.commit()
        v = rebook.build(db, today.year, None, d.id)
        doc = next(x for x in v["docs"] if x["doctor"].id == d.id)
        names = [r["name"] for r in doc["sel"]]
        assert "Перезапис Без Запису" in names and "Перезапис Із Записом" not in names
        assert doc["by_bucket"]["hot"] >= 1
        assert rebook.to_xlsx(v)[:2] == b"PK"
