from datetime import date, datetime, timedelta

from app.models import Appointment, Patient, SessionLocal
from app.services import care_bot as cb


def test_2h_and_birthday(monkeypatch):
    sent = []
    from app.integrations import messaging
    monkeypatch.setattr(messaging, "send_to_patient", lambda p, t, **k: sent.append((p.id, t)) or messaging.SendResult(True, "telegram"))
    now = datetime(2026, 11, 20, 10, 0)
    with SessionLocal() as db:
        p = Patient(full_name="Нагадайко Олена Іванівна", phone="+380501119955", birthday=date(1990, 11, 20))
        db.add(p); db.flush()
        a = Appointment(patient_id=p.id, start=now + timedelta(minutes=110), end=now + timedelta(minutes=170), status="booked")
        db.add(a); db.commit()
        c = cb.cfg(db); c["features"]["remind_2h"] = True; c["features"]["birthday"] = True; c["test_only"] = False; cb.save_cfg(db, c)
        assert cb.send_2h(db, now)["sent"] == 1 and cb.send_2h(db, now)["sent"] == 0
        assert "Олена" in sent[0][1] and "11:50" in sent[0][1]
        assert cb.send_birthdays(db, now)["sent"] >= 1 and cb.send_birthdays(db, now)["sent"] == 0
        assert any("днем народження" in t for _, t in sent)
