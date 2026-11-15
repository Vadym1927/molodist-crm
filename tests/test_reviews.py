"""Відгуки й NPS: запит після візиту → форма → «усі 5» (Google) / рекламація (задача, сповіщення) → картка, дзвіночок, KPI, звіт."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.integrations import messaging
from app.models import SessionLocal, Appointment, Doctor, Patient, Review, Task, User
from app.services import reviews as rv, reports as rp, tg_notify, notifications


def test_review_flow(monkeypatch):
    settings.admin_password = "owner-pass-1"
    sent, tg = [], []
    monkeypatch.setattr(messaging, "send_to_patient", lambda p, text, prefer=None, **k: sent.append((p.id, text, prefer)) or messaging.SendResult(True, "telegram"))
    monkeypatch.setattr(tg_notify, "send_text", lambda chat, text: tg.append((chat, text)) or True)
    now = datetime.now()
    with TestClient(app):          # старт застосунку створює власника — до появи інших користувачів
        pass
    with SessionLocal() as db:
        d = Doctor(name="Лікар Відгук")
        p1, p2 = Patient(full_name="Відгук Олена Пʼять", telegram_chat_id="1"), Patient(full_name="Відгук Ірина Чотири", phone="380501234567")
        ow = User(login="rv_owner_t", name="Адмін Відгук", role="admin", password_hash="x", telegram_chat_id="900")
        db.add_all([d, p1, p2, ow])
        db.flush()
        a1 = Appointment(patient_id=p1.id, doctor_id=d.id, start=now - timedelta(hours=2), end=now - timedelta(hours=1), status="done", services=[])
        a2 = Appointment(patient_id=p2.id, doctor_id=d.id, start=now - timedelta(hours=3), end=now - timedelta(hours=2), status="done", services=[])
        a3 = Appointment(patient_id=p2.id, doctor_id=d.id, start=now - timedelta(minutes=20), end=now - timedelta(minutes=5), status="done", services=[])
        a4 = Appointment(patient_id=p1.id, doctor_id=d.id, start=now - timedelta(hours=5), end=now - timedelta(hours=4), status="cancelled", services=[])
        db.add_all([a1, a2, a3, a4])
        db.commit()
        assert rv.run(db).get("off")                                   # вимкнено за замовчуванням — нічого не шле
        c = rv.cfg(db)
        c.update(on=True, google_url="https://g.page/r/test/review")
        rv.save_cfg(db, c)
        res = rv.run(db)
        assert res == {"requests": 2, "sent": 2} and len(sent) == 2 and "/r/" in sent[0][1] and sent[0][2] == ["telegram", "viber", "sms"]
        assert rv.run(db)["requests"] == 0                             # повторно не шле (візит уже опитано, пацієнту писали нещодавно)
        t1 = db.query(Review).filter_by(patient_id=p1.id).one().token
        t2 = db.query(Review).filter_by(patient_id=p2.id).one().token
        pid1, pid2, did = p1.id, p2.id, d.id
    with TestClient(app) as c:
        assert c.get("/r/nope").status_code == 404
        h = c.get(f"/r/{t1}").text
        assert "Як минув ваш візит" in h and "Олена" in h and "Лікар Відгук" in h
        assert "Поставте" in c.post(f"/r/{t1}", data={"text": "x"}).text
        five = {f"s_{q['key']}": "5" for q in rv.QUESTIONS}
        r = c.post(f"/r/{t1}", data={**five, "nps": "10", "text": "Все чудово"}, follow_redirects=True)
        assert "Скопіювати й відкрити Google" in r.text and "Все чудово" in r.text
        assert c.post(f"/r/{t1}/google").json()["ok"] and "Дякуємо за відгук" in c.get(f"/r/{t1}").text
        r = c.post(f"/r/{t2}", data={**five, "s_admin": "4", "nps": "6", "text": "Довго чекала"}, follow_redirects=True)
        assert "Керівник простору" in r.text and "Google" not in r.text
        c.post(f"/r/{t2}", data={**five, "nps": "10"})                  # друга відповідь не перезаписує
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        card = c.get(f"/patients/{pid2}").text
        assert "Відгуки пацієнта" in card and "Довго чекала" in card and "рекламація" in card and "Анонімно" in card
        assert "перейшов у Google" in c.get(f"/patients/{pid1}").text
        assert c.get("/settings/reviews").status_code == 200 and "Як минув" in c.get("/settings/reviews/preview").text
        c.post("/settings/reviews", data={"on": "1", "delay_min": "45", "min_days": "0", "order": "viber", "text": "{name}, оцініть: {link}",
                                          "q": ["Лікар", "Сервіс", "", "", "", ""], "google_url": "https://g.page/r/x/review"})
    with SessionLocal() as db:
        cf = rv.cfg(db)
        assert cf["delay_min"] == 45 and cf["order"] == "viber" and [q["title"] for q in cf["questions"]] == ["Лікар", "Сервіс"]
        r2 = db.query(Review).filter_by(patient_id=pid2).one()
        assert r2.complaint and r2.avg == 4.75 and r2.nps == 6 and r2.text == "Довго чекала"
        t = db.get(Task, r2.task_id)
        assert t.kind == "complaint" and t.patient_id == pid2 and t.status == "open"
        assert tg and tg[0][0] == "900" and "Рекламація" in tg[0][1] and "Довго чекала" not in tg[0][1]      # текст відгуку в Telegram не йде
        r1 = db.query(Review).filter_by(patient_id=pid1).one()
        assert not r1.complaint and r1.google_at and r1.task_id is None
        ow = db.query(User).filter_by(login="rv_owner_t").one()
        bell = [x for x in notifications.items(db, ow) if x["kind"] == "review"]
        assert len(bell) == 2 and any("Рекламація" in x["title"] for x in bell) and all("#t-reviews" in x["url"] for x in bell)
        st = rv.doctor_stats(db, did, datetime.combine(date.today(), datetime.min.time()) - timedelta(days=1), datetime.now() + timedelta(hours=1))
        assert st["review_n"] == 2 and st["review_avg"] == 4.88 and st["complaints"] == 1 and st["review_nps"] == 0
        t_ = date.today()
        n = rp.build(db, "nps", t_ - timedelta(days=1), t_)
        row = next(x for x in n.rows if x["name"] == "Лікар Відгук")
        assert row["n"] == 2 and row["bad"] == 1 and row["g"] == 1 and n.tiles[0][1] == 0
        assert rp.build(db, "nps", t_ - timedelta(days=1), t_, cat="complaints").rows[0]["name"] == "Відгук Ірина Чотири"
        assert rp.build(db, "nps", t_ - timedelta(days=1), t_, cat="questions").rows and rp.build(db, "nps", t_ - timedelta(days=1), t_, cat="list").rows
        assert rp.build(db, "rooms", t_ - timedelta(days=1), t_).rows and rp.build(db, "rooms", t_ - timedelta(days=1), t_, cat="hours").rows
