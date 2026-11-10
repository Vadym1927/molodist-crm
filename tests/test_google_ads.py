from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.models import SessionLocal, AdsStat, Call, Check, PayItem, Patient
from app.services import google_ads as ga, telephony as tel


def test_google_ads_flow():
    today = date.today()
    d1 = today - timedelta(days=3)
    with SessionLocal() as db:
        p = Patient(full_name="Реклама Тест Пацієнт", phone="+380500009911")
        db.add(p); db.flush()
        rep = ga.ingest(db, {"account": {"id": "111-222-3333", "name": "Test", "currency": "UAH"},
                             "rows": [["campaign", d1.isoformat(), "77", "Ботулінотерапія Київ", "", "ENABLED", 40, 1000, 800.0, 2, 0, {"budget": 300}],
                                      ["campaign", (today - timedelta(days=1)).isoformat(), "77", "Ботулінотерапія Київ", "", "ENABLED", 10, 300, 200.0, 0, 0],
                                      ["keyword", d1.isoformat(), "1~2", "ботокс київ", "Ботулінотерапія Київ", "PHRASE", 20, 400, 500.0, 1, 0]]})
        assert rep["new"] == 3
        assert ga.ingest(db, {"rows": [["campaign", d1.isoformat(), "77", "Ботулінотерапія Київ", "", "ENABLED", 41, 1000, 800.0, 2, 0]]})["updated"] == 1
        c = tel.ingest(db, "ringostat", {"call_id": "ga-1", "caller": "380500009911", "status": "ANSWERED", "dialog": "60", "type": "in",
                                         "utm_source": "google", "utm_medium": "cpc", "utm_campaign": "Ботулінотерапія Київ", "utm_term": "ботокс київ",
                                         "gclid": "$gclid"}, "end")
        c.at = datetime.combine(d1, datetime.min.time()).replace(hour=12)
        assert c.utm_source == "google" and c.gclid is None and c.utm_campaign == "Ботулінотерапія Київ"
        db.add(Check(id=99887766, date=datetime.combine(d1 + timedelta(days=1), datetime.min.time()), amount=6000, patient_id=p.id, deleted=False))
        db.commit()
        v = ga.dashboard(db, today - timedelta(days=10), today)
        t = v["tot"]
        assert t["leads"] >= 1 and t["cost"] >= 1000 and t["revenue"] >= 6000
        camp = next(x for x in v["camps"] if x["key"] == "77")
        assert camp["leads"] == 1 and camp["revenue"] == 6000 and camp["cash"] == 1200.0 and camp["roas"] == 5.0
        assert any(k["leads"] == 1 for k in v["keywords"])
        # баланс: залишок 300 на вчора, витрата вчора 200 → 100; середня ≈ 143/день → мало → платіж у календарі
        cf = ga.cfg(db)
        cf["balance_base"] = {"day": (today - timedelta(days=1)).isoformat(), "amount": 300.0}
        cf["topup_amount"] = 12000.0
        ga.save_cfg(db, cf)
        r = ga.refresh(db)
        assert r["balance"]["amount"] == 100.0 and r["balance"]["low"] and r["pay_item"]
        it = db.get(PayItem, r["pay_item"])
        assert it.amount == 12000.0 and it.group == "marketing"
        assert ga.refresh(db)["pay_item"] is None            # другий раз не дублює
        it.status, it.amount_fact, it.paid_at = "paid", 12000.0, today
        db.commit()
        b = ga.refresh(db)["balance"]
        assert b["amount"] == 100.0 + 10000.0 and not b["low"]
        assert "__HOOK__" not in ga.script("https://x/y") and "https://x/y" in ga.script("https://x/y")
