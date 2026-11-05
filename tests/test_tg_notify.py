"""Сповіщення співробітникам у Telegram: налаштування, тексти, планувальник."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, User, Patient, Appointment, Doctor, Task
from app.services import tg_notify as tn, alerts


def test_texts_and_run(monkeypatch):
    settings.admin_password = "owner-pass-1"
    sent = []
    monkeypatch.setattr(alerts, "_api", lambda m, **kw: sent.append(kw["data"]) or True)
    from app.security import ensure_owner
    with SessionLocal() as db:
        ensure_owner(db)                                       # власник — першим, інакше не ввійти
        d = Doctor(name="Сповіщ Лікарка", active=True); db.add(d); db.commit()
        u_adm = User(name="Сповіщ Адмін", login="tn_adm", role="admin", telegram_chat_id="111", password_hash="x")
        u_doc = User(name="Сповіщ Лікарка", login="tn_doc", role="doctor", telegram_chat_id="222", doctor_id=d.id, password_hash="x")
        db.add_all([u_adm, u_doc]); db.commit()
        p = Patient(full_name="Іменинниця Тест", phone="380000000099", birthday=date(1990, date.today().month, date.today().day), doctor_id=d.id)
        db.add(p); db.commit()
        st = datetime.combine(date.today(), datetime.min.time()).replace(hour=11)
        db.add(Appointment(patient_id=p.id, doctor_id=d.id, start=st, end=st + timedelta(hours=1), status="booked", services=[{"name": "Чистка", "price": 1500}]))
        db.add(Task(title="Передзвонити", assignee_id=u_adm.id, due_date=date.today() - timedelta(days=1), status="open"))
        db.commit()
        assert "Іменинниця" in tn.text_birthdays(db, u_adm, "admin") and "Вітаємо" in tn.text_birthdays(db, u_adm, "admin")
        assert "Чистка" in tn.text_today(db, u_doc, "doctor") and "380000000099" not in tn.text_today(db, u_doc, "doctor")
        assert "Передзвонити" in tn.text_tasks(db, u_adm, "admin") and "Прострочені" in tn.text_tasks(db, u_adm, "admin")
        assert "Підсумок дня" in tn.text_day_close(db, u_adm, "admin") and "Тиждень" in tn.text_week(db, u_adm, "owner")
        assert "Контроль бюті-планів" in tn.text_plan_control(db, u_adm, "owner") or "немає" in tn.text_plan_control(db, u_adm, "owner")
        # планувальник: адмінам о 08:30 «записи на сьогодні» — один раз
        now = datetime.combine(date.today(), datetime.min.time()).replace(hour=8, minute=35)
        n1 = tn.run(db, now)
        assert n1 >= 2 and any(x["chat_id"] == "111" and "Записи на" in x["text"] for x in sent)
        assert tn.run(db, now) == 0                     # повторно того ж дня — нічого
        assert tn.run(db, now.replace(hour=3)) == 0     # поза вікном часу — нічого
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        pg = c.get("/settings/tg-notify").text
        assert "Сповіщення в Telegram" in pg and "Сповіщ Адмін" in pg and "admin_today_time" in pg
        r = c.post("/settings/tg-notify", data={"admin_today_time": "07:45", "admin_today_on": "1", "owner_week_time": "10:00", "owner_week_dow": "tue", "admin_tasks_time": "09:00",
                                                "chiefs": [str(u_doc.id)], "greeting": "Зі святом!", "bday_days": "5", "action": "save"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            c_ = tn.cfg(db)
            assert c_["roles"]["admin"]["today"]["time"] == "07:45" and c_["roles"]["owner"]["week"]["dow"] == "tue" and c_["chiefs"] == [u_doc.id]
            assert c_["greeting"] == "Зі святом!" and c_["bday_days"] == 5 and c_["roles"]["admin"]["tasks"]["on"] is False
            assert [u.id for u in tn.recipients(db, "chief")] == [u_doc.id]
