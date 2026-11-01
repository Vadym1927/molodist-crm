"""Автономний режим: записи й оплати каси CRM зараховуються у виконання бюті-плану."""
from datetime import date, datetime, timedelta

from app.models import SessionLocal, Appointment, BeautyPlan, CbService, Doctor, Patient, PlanItem, Sale, SaleItem
from app.services import autonomy, kpi


def test_plan_counts_crm():
    with SessionLocal() as db:
        d = Doctor(name="План Лікар", active=True)
        p = Patient(full_name="План Пацієнтка")
        db.add_all([d, p, CbService(id=94001, name="Біоревіталізація Rejuran", price=6000)]); db.flush()
        plan = BeautyPlan(patient_id=p.id, doctor_id=d.id, session_at=datetime.now() - timedelta(days=3), status="active",
                          items=[PlanItem(kind="procedure", name="Біоревіталізація Rejuran", qty=3, unit_price=6000)])
        db.add(plan); db.commit()
        autonomy.save(db, autonomous=True, since=(date.today() - timedelta(days=2)).isoformat())
        s = Sale(patient_id=p.id, status="paid", at=datetime.now() - timedelta(days=1), subtotal=6000, total=6000, discount=0)
        s.items.append(SaleItem(kind="service", ref_id=94001, name="Біоревіталізація Rejuran", qty=1, price=6000, discount=0, total=6000, doctor_id=d.id))
        db.add(s)
        db.add(Appointment(patient_id=p.id, doctor_id=d.id, start=datetime.now() + timedelta(days=10), end=datetime.now() + timedelta(days=10, hours=1),
                           status="booked", services=[{"id": 94001, "name": "Біоревіталізація Rejuran", "price": 6000}]))
        db.commit()
        ps = kpi.compute_plan(db, db.get(BeautyPlan, plan.id))
        st = ps.items[0]
        assert st.done == 1 and st.booked == 1 and ps.is_booked
        assert next(x for x in kpi.all_statuses(db) if x.plan.id == plan.id).items[0].done == 1
        autonomy.save(db, autonomous=False)
        assert kpi.compute_plan(db, db.get(BeautyPlan, plan.id)).items[0].done == 0
