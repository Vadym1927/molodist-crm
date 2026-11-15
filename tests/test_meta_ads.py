"""Meta Ads: статистика кабінету, лід із referral у Direct, воронка до оплати."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appeal, Appointment, ChatMessage, Patient, Sale
from app.services import meta_ads as ma, meta_inbox as mi


def _ev(sid, mid, text, ref=None, own="OWN1"):
    msg = {"mid": mid, "text": text}
    if ref:
        msg["referral"] = ref
    return {"object": "instagram", "entry": [{"id": own, "messaging": [{"sender": {"id": sid}, "recipient": {"id": own},
                                                                         "timestamp": int(datetime.utcnow().timestamp() * 1000), "message": msg}]}]}


def test_meta_ads_flow(monkeypatch):
    settings.admin_password = "owner-pass-1"
    monkeypatch.setattr(mi, "_profile", lambda *a, **k: "Реклама Тест")
    t = date.today()
    with SessionLocal() as db:
        r = ma.ingest(db, [{"day": str(t), "ad_id": "AD77", "ad_name": "Акне банер", "adset": "Жінки 18–45", "campaign": "Акне",
                            "impressions": 1000, "clicks": 20, "spend": "10.00", "views3": 250}], rate=40)
        assert r["new"] == 1 and ma.ingest(db, [{"day": str(t), "ad_id": "AD77", "impressions": 2000, "clicks": 40, "spend": 20, "views3": 500}])["updated"] == 1
        assert mi.ingest(db, _ev("U1", "m1", "Скільки коштує?", {"ad_id": "AD77", "source": "ADS", "type": "OPEN_THREAD",
                                                                 "ads_context_data": {"ad_title": "Акне банер"}})) == 1
        assert mi.ingest(db, _ev("U1", "m2", "І коли можна?")) == 1
        # клік по рекламі окремою подією → перше повідомлення отримує мітку
        ev = _ev("U2", "x", "")
        ev["entry"][0]["messaging"][0].pop("message")
        ev["entry"][0]["messaging"][0]["referral"] = {"ad_id": "AD77", "source": "ADS"}
        mi.ingest(db, ev)
        assert mi.ingest(db, _ev("U2", "m3", "Добрий день")) == 1
        assert mi.ingest(db, _ev("U3", "m4", "Не з реклами")) == 1
        ms = db.query(ChatMessage).filter(ChatMessage.ad.is_not(None)).all()
        assert sorted(m.mid for m in ms) == ["m1", "m3"]
        m1 = next(m for m in ms if m.mid == "m1")
        if m1.appeal_id:
            assert "ad_id=AD77" in (db.get(Appeal, m1.appeal_id).utm or "")
        p = Patient(full_name="Реклама Пацієнт")
        db.add(p)
        db.flush()
        for m in db.query(ChatMessage).filter_by(thread="U1"):
            m.patient_id = p.id
        now = datetime.now()
        db.add(Appointment(patient_id=p.id, start=now + timedelta(hours=2), end=now + timedelta(hours=3), status="booked", services=[]))
        db.add(Sale(patient_id=p.id, at=now + timedelta(minutes=5), status="paid", subtotal=1800, total=1800))
        db.commit()
        v = ma.dashboard(db, t - timedelta(days=1), t)
        tt = v["tot"]
        assert tt["impressions"] == 2000 and tt["cost"] == 20 and tt["uah"] == 800 and tt["hook"] == 25 and tt["ctr"] == 2
        assert tt["leads"] == 2 and tt["booked"] == 1 and tt["payers"] == 1 and tt["revenue"] == 1800 and tt["cpl"] == 400 and tt["roas"] == 2.25
        assert v["rows"][0]["name"] == "Акне банер" and v["rows"][0]["leads"] == 2
        assert ma.dashboard(db, t, t, group="day")["rows"][0]["day"] == t and ma.dashboard(db, t, t, group="campaign")["rows"][0]["name"] == "Акне"
        hook = ma.cfg(db)["hook"]
    with TestClient(app) as c:
        assert c.post("/api/public/ads/meta/wrong", json={"rows": []}).status_code == 404
        assert c.post(f"/api/public/ads/meta/{hook}", json={"rows": [{"day": str(t), "ad_id": "AD78", "ad_name": "Чистка", "spend": 5, "impressions": 100, "clicks": 3}]}).json()["new"] == 1
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get("/marketing/meta").text
        assert "Meta Ads" in h and "Акне банер" in h and "Реклама Пацієнт" in h and "Підключення рекламного кабінету" in h
        assert "/marketing/meta" in c.get("/reports").text
