from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, ApiKey, Appointment, Patient, PatientFile, Task, Subscription, GiftCertificate


def test_care_bot_cabinet_and_answers(monkeypatch):
    from app.security import new_api_key
    from app.services import care_bot as cb
    from app.integrations import messaging
    sent = []
    monkeypatch.setattr(messaging, "sending_enabled", lambda: True)
    monkeypatch.setattr(messaging.settings, "patient_bot_token", "x")
    monkeypatch.setattr(messaging.httpx, "post", lambda url, **k: sent.append(k["data"]) or type("R", (), {"status_code": 200, "json": lambda s: {"ok": True, "result": {"message_id": 1}}})())
    monkeypatch.setattr("app.services.tg_notify.send_text", lambda ch, t: sent.append({"admin": t}) or True)
    with SessionLocal() as db:
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="care-bot", prefix=prefix, key_hash=hsh, scopes=["bot"]))
        p = Patient(full_name="Бот Тест Пацієнтка", phone="+380679990011", telegram_chat_id="777001", cb_balance=350)
        db.add(p); db.flush()
        tomorrow = datetime.now().replace(hour=11, minute=0, second=0, microsecond=0) + timedelta(days=1)
        a = Appointment(patient_id=p.id, start=tomorrow, end=tomorrow + timedelta(minutes=60), status="booked",
                        services=[{"id": 1, "name": "IPL Все обличчя", "price": 3900, "minutes": 60}])
        a2 = Appointment(patient_id=p.id, start=tomorrow + timedelta(days=3), end=tomorrow + timedelta(days=3, hours=1), status="booked",
                         services=[{"id": 2, "name": "Пілінг"}])
        db.add_all([a, a2])
        db.add(Subscription(id=990001, patient_id=p.id, type_name="Масаж курс 6", visits_total=6, visits_used=2, paid=10200,
                            valid_to=(datetime.now() + timedelta(days=100)).date()))
        db.add(GiftCertificate(id=990002, patient_id=p.id, code="T-1", type_name="Сертифікат 2000", nominal=2000, balance=2000,
                               valid_to=(datetime.now() + timedelta(days=30)).date()))
        db.commit()
        pid, aid, aid2 = p.id, a.id, a2.id
    h = {"X-API-Key": raw}
    settings.admin_password = "owner-pass-cb"
    with TestClient(app) as c:
        cfgj = c.get("/api/v1/bot/config", headers=h).json()
        assert cfgj["features"] and not any(cfgj["features"].values())               # усе вимкнено за замовчуванням
        rec = c.get("/api/v1/bot/patient/777001/records", headers=h).json()
        assert [x["id"] for x in rec["upcoming"]] == [aid, aid2] and rec["upcoming"][0]["actions"] == ["confirm", "reschedule", "cancel"]
        w = c.get("/api/v1/bot/patient/777001/wallet", headers=h).json()
        assert w["balance"] == 350 and w["subscriptions"][0]["visits_left"] == 4 and w["certificates"][0]["balance"] == 2000
        assert c.get("/api/v1/bot/patient/000/records", headers=h).status_code == 404
        r = c.post("/api/v1/bot/patient/777001/files", headers=h, files={"file": ("analiz.pdf", b"%PDF-1.4 test", "application/pdf")})
        assert r.json()["ok"] and not r.json()["duplicate"]
        assert c.post("/api/v1/bot/patient/777001/files", headers=h, files={"file": ("x.pdf", b"%PDF-1.4 test", "application/pdf")}).json()["duplicate"]
        fl = c.get("/api/v1/bot/patient/777001/files", headers=h).json()["files"]
        assert len(fl) == 1 and c.get(f"/api/v1/bot/patient/777001/files/{fl[0]['id']}", headers=h).content == b"%PDF-1.4 test"
        m = c.post("/api/v1/bot/patient/777001/message", headers=h, json={"to": "admin", "text": "Можна перенести на пʼятницю?"}).json()
        assert m["ok"] and m["task_id"]
        # нагадування: вимкнено → не шле; примусово — з кнопками
        with SessionLocal() as db:
            assert cb.send_reminders(db)["skipped"]
            r = cb.send_reminders(db, force=True)
            assert r["sent"] == 1 and "mcrm:confirm:" in sent[-1]["reply_markup"]
            assert cb.send_reminders(db, force=True)["sent"] == 0                      # двічі не нагадуємо
        ans = c.post(f"/api/v1/bot/appointment/{aid}/answer", headers=h, json={"chat_id": "777001", "action": "confirm"}).json()
        assert ans["ok"] and ans["status"] == "confirmed"
        with SessionLocal() as db:
            from app.models import AuditLog
            assert db.query(AuditLog).filter(AuditLog.action == "appointment.status", AuditLog.entity_id == pid).first().details["via"].startswith("Бот турботи")
        assert c.post(f"/api/v1/bot/appointment/{aid}/answer", headers=h, json={"chat_id": "999", "action": "cancel"}).status_code == 404
        mv = c.post(f"/api/v1/bot/appointment/{aid2}/answer", headers=h, json={"chat_id": "777001", "action": "reschedule", "wish": "наступного тижня вранці"}).json()
        assert mv["ok"] and mv["status"] == "booked"
        cn = c.post(f"/api/v1/bot/appointment/{aid2}/answer", headers=h, json={"chat_id": "777001", "action": "cancel", "reason": "захворіла"}).json()
        assert cn["status"] == "cancelled"
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-cb"})
        pg = c.get("/settings/care-bot")
        assert pg.status_code == 200 and "пацієнтів підписані на бот" in pg.text
        c.post("/settings/care-bot", data={"f_reminders": "1", "remind_hour": "10", "remind_text": "Нагадуємо {when}"}, follow_redirects=False)
        with SessionLocal() as db:
            assert cb.cfg(db)["features"]["reminders"] and not cb.cfg(db)["features"]["cabinet"]
            c_ = cb.cfg(db); c_["features"]["reminders"] = False; cb.save_cfg(db, c_)
    with SessionLocal() as db:
        f = db.query(PatientFile).filter(PatientFile.patient_id == pid).one()
        assert f.category == "lab"
        assert db.query(Task).filter(Task.patient_id == pid, Task.kind == "bot").count() == 3     # аналізи, повідомлення, перенесення
        assert db.get(Appointment, aid2).cancel_reason.startswith("Скасовано пацієнтом у боті")
