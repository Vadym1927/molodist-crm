from datetime import date, datetime, timedelta

from app.models import (SessionLocal, Appeal, Appointment, AuditLog, Call, ChatMessage, Doctor, Patient, StaffShift, Task, User)
from app.services import reports as rp


def test_staff_reports():
    t = date.today()
    now = datetime.now()
    day = datetime.combine(t, datetime.min.time())
    with SessionLocal() as db:
        doc = Doctor(name="Лікар Завантаження")
        pat = Patient(full_name="Пацієнт Зрив")
        adm = User(login="adm_kpi_t", name="Адмін КПІ", role="admin", password_hash="x")
        db.add_all([doc, pat, adm])
        db.flush()
        db.add(StaffShift(doctor_id=doc.id, day=t, start="10:00", end="14:00"))
        svc = [{"id": None, "name": "Чистка", "price": 1000}]
        db.add(Appointment(patient_id=pat.id, doctor_id=doc.id, start=day.replace(hour=10), end=day.replace(hour=11), status="done",
                           services=svc, created_by=adm.id))
        db.add(Appointment(patient_id=pat.id, doctor_id=doc.id, start=day.replace(hour=12), end=day.replace(hour=13), status="cancelled",
                           services=svc, cancel_reason="Захворіла"))
        db.add(Appointment(patient_id=pat.id, doctor_id=doc.id, start=day.replace(hour=13), end=day.replace(hour=14), status="no_show",
                           services=svc))
        db.add(Appointment(patient_id=pat.id, doctor_id=doc.id, start=day + timedelta(days=3, hours=10), end=day + timedelta(days=3, hours=11),
                           status="booked", services=svc))
        db.add(Call(at=now, provider="t", direction="in", status="answered", duration=120, user_id=adm.id, ai_score=80, phone="380501112233"))
        db.add(Call(at=now, provider="t", direction="in", status="missed", phone="380509998877"))
        db.add(Appeal(name="Зв 1", status="Записаний", responsible_id=adm.id))
        db.add(Appeal(name="Зв 2", status="Відмова", responsible_id=adm.id))
        u = datetime.utcnow()
        db.add(ChatMessage(at=u - timedelta(minutes=30), channel="instagram", thread="kpi_t1", direction="in", text="Привіт"))
        db.add(ChatMessage(at=u - timedelta(minutes=20), channel="instagram", thread="kpi_t1", direction="out", text="Вітаю", user_id=adm.id, status="sent"))
        db.add(ChatMessage(at=u - timedelta(minutes=5), channel="instagram", thread="kpi_t2", direction="in", text="?"))
        db.add(AuditLog(action="appointment.status", user_id=adm.id, details={"status": "confirmed"}))
        db.add(Task(title="Зроблена КПІ", assignee_id=adm.id, status="done", done_at=u, due_date=t))
        db.add(Task(title="Прострочена КПІ", assignee_id=adm.id, status="open", due_date=t - timedelta(days=2)))
        db.commit()

        r = rp.build(db, "load", t, t)
        row = next(x for x in r.rows if x["name"] == "Лікар Завантаження")
        assert row["sh"] == 4 and row["busy"] == 1 and row["load"] == 25 and row["free"] == 3 and row["per_h"] == 250
        assert any(x["name"] == "10:00–11:00" and x["busy"] >= 1 for x in rp.build(db, "load", t, t, cat="hours").rows)
        assert rp.build(db, "load", t, t, cat="wd").rows

        k = rp.build(db, "admin_kpi", t, t)
        row = next(x for x in k.rows if x["name"] == "Адмін КПІ")
        assert row["c_in"] == 1 and row["talk"] == 2 and row["score"] == 80 and row["ap"] == 2 and row["ap_ok"] == 1 and row["conv"] == 50
        assert row["rec"] == 1 and row["conf"] == 1 and row["chat"] == 1 and row["rt"] == 10 and row["tasks"] == 1
        assert "без відповіді" in k.tiles[2][4]

        ts = rp.build(db, "tasks", t, t)
        row = next(x for x in ts.rows if x["name"] == "Адмін КПІ")
        assert row["done"] == 1 and row["ontime"] == 100 and row["over"] == 1
        assert any(x["name"] == "Прострочена КПІ" and x["days"] == 2 for x in rp.build(db, "tasks", t, t, cat="overdue").rows)
        assert rp.build(db, "tasks", t, t, cat="kinds").rows

        c = rp.build(db, "cancels", t, t)
        row = next(x for x in c.rows if x["name"] == "Лікар Завантаження")
        assert row["all"] == 3 and row["c"] == 1 and row["n"] == 1 and row["sum"] == 2000 and row["re"] == 2
        assert {"Захворіла", "Неявка"} <= {x["name"] for x in rp.build(db, "cancels", t, t, cat="reasons").rows}
        assert len([x for x in rp.build(db, "cancels", t, t, cat="list").rows if x["name"] == "Пацієнт Зрив"]) == 2
