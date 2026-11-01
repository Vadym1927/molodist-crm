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


def test_adopted_not_double():
    from app.models import Visit
    with SessionLocal() as db:
        p = Patient(full_name="Подвійна Перевірка")
        db.add_all([p, CbService(id=94101, name="Ботулінотерапія лоб", price=4000)]); db.flush()
        plan = BeautyPlan(patient_id=p.id, session_at=datetime.now() - timedelta(days=3), status="active",
                          items=[PlanItem(kind="procedure", name="Ботулінотерапія лоб", qty=2, unit_price=4000)])
        t = datetime.now() + timedelta(days=5)
        db.add_all([plan, Visit(source="cb_visit", external_id="55501-94101", patient_id=p.id, service_id=94101, service_name="Ботулінотерапія лоб",
                                starts_at=t, ends_at=t + timedelta(hours=1), status="booked")])
        db.commit()
        autonomy.save(db, autonomous=True, since=(date.today() - timedelta(days=2)).isoformat())
        from app.services import appointments as aps
        assert aps.adopt_cb(db, 55501)
        db.expire_all()
        assert kpi.compute_plan(db, db.get(BeautyPlan, plan.id)).items[0].booked == 1
