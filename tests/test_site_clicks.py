"""Кліки з реклами (тег GTM) → дзвінок / звернення з месенджера → пацієнт → офлайн-конверсія з gclid і цінністю в Google Ads."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.main import app
from app.models import SessionLocal, Appeal, Call, Check, Patient, SiteClick
from app.services import google_ads as ga, site_leads as sl, appeals as ap


def test_click_to_call_appeal_and_offline():
    now = datetime.now()
    with SessionLocal() as db:
        hook = sl.cfg(db)["hook"]
    with TestClient(app) as c:
        assert c.post(f"/api/public/click/{hook}", content='{"kind":"telegram","utm_source":"(direct)"}').text == "ok"   # не реклама — пропуск
        assert c.post(f"/api/public/click/{hook}", content='{"kind":"phone","gclid":"GCLID-PH-1","utm_campaign":"Тест Кліки","cid":"1.2"}').text == "ok"
        assert c.post(f"/api/public/click/{hook}", content='{"kind":"telegram","gclid":"GCLID-TG-1","utm_source":"google","utm_medium":"cpc","utm_campaign":"Тест Кліки"}').text == "ok"
        assert c.post("/api/public/click/wrong", content='{"kind":"phone"}').status_code == 404
    with SessionLocal() as db:
        clicks = db.query(SiteClick).filter(SiteClick.gclid.like("GCLID-%")).all()
        assert {x.kind for x in clicks} == {"phone", "telegram"}
        p = Patient(full_name="Клік Реклама Пацієнт", phone="+380500007731")
        db.add(p); db.flush()
        cl = Call(provider="ringostat", at=datetime.now() + timedelta(minutes=2), direction="in", phone="380500007731", status="answered", patient_id=p.id)
        db.add(cl)
        a = ap.create(db, {"name": "Тг Пацієнт", "phone": "+380500007732", "source": "Telegram"})
        db.commit()
        rep = sl.match_clicks(db, date.today(), date.today())
        assert rep["calls"] >= 1 and rep["appeals"] >= 1
        db.refresh(cl); db.refresh(a)
        assert cl.gclid == "GCLID-PH-1" and cl.utm_campaign == "Тест Кліки" and cl.attr == "click"
        assert "gclid=GCLID-TG-1" in a.utm and "attr=click" in a.utm
        ls = [r for r in ga.leads(db, date.today(), date.today()) if r.get("gclid") in ("GCLID-PH-1", "GCLID-TG-1")]
        assert len(ls) == 2
        # пацієнт прийшов і заплатив учора → офлайн-конверсія
        cl.at = now - timedelta(days=2)
        db.add(Check(id=99887799, date=now - timedelta(days=1), amount=4500, patient_id=p.id, deleted=False))
        cf = ga.cfg(db); cf["offline_on"] = True; ga.save_cfg(db, cf)
        db.commit()
        rows = [r for r in ga.offline_rows(db) if r["gclid"] == "GCLID-PH-1"]
        assert rows and rows[0]["value"] == 4500 and rows[0]["name"] and "+" in rows[0]["time"]
        ga.offline_ack(db, ["GCLID-PH-1"])
        assert not [r for r in ga.offline_rows(db) if r["gclid"] == "GCLID-PH-1"]
    assert "/click/" in sl.gtm_tag("https://x/api/public/lead/h") and "_gcl_aw" in sl.GTM
    assert "uploadOffline" in ga.script("https://x/h") and "forOfflineConversions" in ga.SCRIPT


def test_gclid_from_cookie():
    m = sl.marks({"cookies": "TILDAUTM=utm_source%3Dgoogle; _gcl_aw=GCL.1731234567.Cj0KCQabc_DEF"})
    assert m["gclid"] == "Cj0KCQabc_DEF"
