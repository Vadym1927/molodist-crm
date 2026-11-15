from datetime import date, datetime, timedelta

from app.models import SessionLocal, Appointment, ChatMessage, MessageLog, Patient, User
from app.services import reports as rp


def test_comm_reports():
    t = date.today()
    u = datetime.utcnow()
    with SessionLocal() as db:
        adm = User(login="chat_adm_t", name="Чат Адмін", role="admin", password_hash="x")
        p = Patient(full_name="Чат Пацієнт")
        db.add_all([adm, p])
        db.flush()
        db.add(ChatMessage(at=u - timedelta(minutes=50), channel="instagram", thread="cs1", direction="in", text="Привіт", name="Оля"))
        db.add(ChatMessage(at=u - timedelta(minutes=40), channel="instagram", thread="cs1", direction="out", text="Вітаю", user_id=adm.id, status="sent"))
        db.add(ChatMessage(at=u - timedelta(minutes=30), channel="instagram", thread="cs1", direction="in", text="Ціна?"))
        db.add(ChatMessage(at=u - timedelta(minutes=20), channel="facebook", thread="cs2", direction="in", text="Є хто?", name="Ігор"))
        db.add(MessageLog(at=u - timedelta(minutes=15), patient_id=p.id, user_id=adm.id, channel="sms", text="Чекаємо", status="sent"))
        db.add(MessageLog(at=u - timedelta(minutes=14), patient_id=p.id, user_id=adm.id, channel="telegram", text="x", status="failed", error="немає чату"))
        db.add(Appointment(patient_id=p.id, start=datetime.now() + timedelta(days=1), end=datetime.now() + timedelta(days=1, hours=1), status="booked",
                           services=[], created_at=u - timedelta(minutes=5)))
        db.commit()
        c = rp.build(db, "chat_stats", t - timedelta(days=1), t)
        tiles = {x[0]: x for x in c.tiles}
        assert tiles["Переписок"][1] >= 2 and tiles["Без відповіді"][1] >= 2 and c.rows
        st = rp.build(db, "chat_stats", t - timedelta(days=1), t, cat="staff")
        row = next(x for x in st.rows if x["name"] == "Чат Адмін")
        assert row["out"] == 1 and row["rt"] == 10 and row["fast"] == 100
        op = rp.build(db, "chat_stats", t - timedelta(days=1), t, cat="open")
        assert {"Оля", "Ігор"} <= {x["name"] for x in op.rows}
        assert rp.build(db, "chat_stats", t - timedelta(days=1), t, cat="hours").rows and rp.build(db, "chat_stats", t - timedelta(days=1), t, cat="wd").rows
        m = rp.build(db, "messages", t - timedelta(days=1), t)
        by = {x["name"]: x for x in m.rows}
        assert by["SMS"]["ok"] == 1 and by["SMS"]["bk"] == 1 and by["Telegram"]["fail"] == 1
        assert any(x["err"] == "немає чату" for x in rp.build(db, "messages", t - timedelta(days=1), t, cat="errors").rows)
        assert rp.build(db, "messages", t - timedelta(days=1), t, cat="staff").rows[0]["name"] == "Чат Адмін"
        assert rp.build(db, "messages", t - timedelta(days=1), t, cat="kind").rows
