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


def test_ringostat_utm_sync(monkeypatch):
    import httpx
    from app.services import telephony as tel_
    today = date.today()
    at = datetime.combine(today - timedelta(days=1), datetime.min.time()).replace(hour=14, minute=5)
    with SessionLocal() as db:
        monkeypatch.setattr(tel_, "_raw", lambda db_: {"provider": "ringostat"})
        monkeypatch.setattr(tel_, "secret", lambda d, prov, key: "k" if key == "auth_key" else "1")
        c = Call(provider="ringostat", at=at + timedelta(minutes=2), direction="in", phone="380500007701", status="answered")
        db.add(c); db.commit()

        class R:
            status_code = 200
            text = ""
            def __init__(self, rows): self._r = rows
            def json(self): return self._r

        class H:
            def get(self, url, params=None, headers=None):
                assert headers["Auth-key"] == "k"
                if "landing" in params["fields"]:
                    return type("E", (), {"status_code": 400, "text": "bad field", "json": lambda s: {}})()
                day = params["from"][:10]
                return R([{"calldate": at.strftime("%Y-%m-%d %H:%M:%S"), "caller": "380500007701", "dst": "380441112233", "disposition": "ANSWERED",
                           "billsec": "40", "uniqueid": "u1", "call_type": "in", "utm_source": "google", "utm_medium": "cpc",
                           "utm_campaign": "Пошук | Клініка", "utm_term": "косметолог київ", "utm_content": ""},
                          {"calldate": at.strftime("%Y-%m-%d %H:%M:%S"), "caller": "380500007702", "dst": "380441112233", "disposition": "NO ANSWER",
                           "billsec": "0", "uniqueid": "u2", "call_type": "in", "utm_source": "(direct)", "utm_medium": "(none)",
                           "utm_campaign": "", "utm_term": "", "utm_content": ""}] if day <= at.strftime("%Y-%m-%d") < params["to"][:10] else [])
        rep = tel_.sync_utm(db, today - timedelta(days=2), today, http=H())
        db.refresh(c)
        assert c.utm_campaign == "Пошук | Клініка" and c.utm_source == "google" and rep["created"] == 1 and rep["updated"] >= 1
        ls = ga.leads(db, today - timedelta(days=2), today)
        assert any(l["campaign"] == "Пошук | Клініка" for l in ls)


def test_ga4_match_calls(monkeypatch):
    from app.models import GaEvent
    from app.services import ga4
    today = date.today()
    t = datetime.combine(today - timedelta(days=1), datetime.min.time()).replace(hour=15, minute=20)
    with SessionLocal() as db:
        c = ga4.cfg(db); c.update(property="123", events=["click_phone"]); ga4.save_cfg(db, c)
        monkeypatch.setattr(ga4, "_report", lambda db_, body, http=None, c=None: [
            {"dateHourMinute": t.strftime("%Y%m%d%H%M"), "eventName": "click_phone", "sessionSource": "google", "sessionMedium": "cpc",
             "sessionCampaignName": "Пошук | Клініка", "sessionGoogleAdsKeyword": "дерматолог київ", "eventCount": "1"},
            {"dateHourMinute": (t + timedelta(hours=1)).strftime("%Y%m%d%H%M"), "eventName": "click_phone", "sessionSource": "google", "sessionMedium": "cpc",
             "sessionCampaignName": "Пошук | Клініка", "sessionGoogleAdsKeyword": "", "eventCount": "1"},
            {"dateHourMinute": (t + timedelta(hours=1)).strftime("%Y%m%d%H%M"), "eventName": "click_phone", "sessionSource": "(direct)", "sessionMedium": "(none)",
             "sessionCampaignName": "(not set)", "sessionGoogleAdsKeyword": "(not set)", "eventCount": "1"}])
        assert ga4.sync(db, today - timedelta(days=2), today)["new"] == 3
        c1 = Call(provider="ringostat", at=t + timedelta(seconds=50), direction="in", phone="380500008801", status="answered")
        c2 = Call(provider="ringostat", at=t + timedelta(hours=1, seconds=40), direction="in", phone="380500008802", status="answered")
        c3 = Call(provider="ringostat", at=t + timedelta(hours=3), direction="in", phone="380500008803", status="answered")
        db.add_all([c1, c2, c3]); db.commit()
        rep = ga4.match_calls(db, today - timedelta(days=2), today)
        assert rep["matched"] >= 2
        assert c1.attr == "ga4" and c1.utm_campaign == "Пошук | Клініка" and c1.utm_term == "дерматолог київ"
        assert c2.attr == "ga4?" and c3.attr is None and c3.utm_source is None
        ls = ga.leads(db, today - timedelta(days=2), today)
        assert any(l["phone"] == "380500008801" and l["attr"] == "ga4" for l in ls)
        try:
            ga4.set_key(db, "{}")
            assert False
        except ValueError:
            pass


def test_ga4_lead_events_and_appeals():
    from app.models import GaEvent, Appeal
    from app.services import ga4
    today = date.today()
    t = datetime.combine(today - timedelta(days=1), datetime.min.time()).replace(hour=11, minute=10)
    with SessionLocal() as db:
        c = ga4.cfg(db); c.update(property="123", events=["phone_click"], lead_events=["click_telegram", "click_booking"]); ga4.save_cfg(db, c)
        db.add(GaEvent(at=t, event="click_telegram", source="google", medium="cpc", campaign="Поиск | Клініка", keyword="косметолог", count=1))
        db.add(GaEvent(at=t, event="click_booking", source="google", medium="cpc", campaign="Поиск | Клініка", keyword="", count=3))
        shift = datetime.now() - datetime.utcnow()
        a1 = Appeal(created_at=t - shift + timedelta(minutes=3), phone="380500008811", source="Telegram-бот", status="Новий")
        a2 = Appeal(created_at=t - shift + timedelta(minutes=3), phone="380500008812", source="Instagram", status="Новий")
        db.add_all([a1, a2]); db.commit()
        rep = ga4.match_appeals(db, today - timedelta(days=2), today)
        assert rep["matched"] == 1 and "utm_campaign=Поиск | Клініка" in a1.utm and a2.utm is None
        sm = ga4.summary(db, today - timedelta(days=2), today)
        assert sm["paid_other"] == 4 and sm["by_event"]["click_booking"] == 3 and sm["paid"] == 0
        # клік по месенджеру не привʼязується до дзвінка
        cl = Call(provider="ringostat", at=t + timedelta(seconds=30), direction="in", phone="380500008813", status="answered")
        db.add(cl); db.commit()
        ga4.match_calls(db, today - timedelta(days=2), today)
        assert cl.attr is None
        ls = ga.leads(db, today - timedelta(days=2), today)
        assert any(l["phone"] == "380500008811" and l["campaign"] == "Поиск | Клініка" and l["attr"] == "ga4" for l in ls)


def test_daily_topup_item():
    from datetime import date as _d
    with SessionLocal() as db:
        cf = ga.cfg(db); cf["daily_topup"] = 1800.0; cf["daily_days"] = [0, 1, 2, 3, 4, 5, 6]; ga.save_cfg(db, cf)
        r = ga.refresh(db)
        it = db.get(PayItem, r["pay_item"]) if r["pay_item"] else db.scalar(select(PayItem).where(PayItem.ref == f"gads:daily:{_d.today().isoformat()}"))
        assert it and it.due == _d.today() and it.amount in (1800.0, 3600.0)
        assert ga.refresh(db)["pay_item"] is None
        cf = ga.cfg(db); cf["daily_topup"] = 0.0; ga.save_cfg(db, cf)


def test_conv_breakdown():
    from datetime import date as _d, timedelta as _td
    d = (_d.today() - _td(days=1)).isoformat()
    with SessionLocal() as db:
        ga.ingest(db, {"rows": [["campaign", d, "880", "Конв Тест", "", "ENABLED", 10, 100, 100.0, 2, 0],
                                ["conv", d, "Конв Тест|click_telegram", "click_telegram", "Конв Тест", "", 0, 0, 0, 2, 0, {"all": 2}],
                                ["tconv", d, "масаж обличчя|click_telegram", "масаж обличчя", "Конв Тест", "click_telegram", 0, 0, 0, 1, 0, {"all": 1, "action": "click_telegram"}]]})
        v = ga.dashboard(db, _d.today() - _td(days=3), _d.today())
        assert any(a["name"] == "click_telegram" and a["all"] >= 2 for a in v["conv_acts"])
        assert any(t["term"] == "масаж обличчя" for t in v["conv_terms"])
    assert "conversion_action_name" in ga.SCRIPT


def test_daily_topup_to_target():
    from datetime import date as _d, datetime as _dt
    with SessionLocal() as db:
        ref = f"gads:daily:{_d.today().isoformat()}"
        for x in db.scalars(select(PayItem).where(PayItem.ref == ref)):
            db.delete(x)
        db.commit()
        cf = ga.cfg(db); cf.update(daily_topup=1800.0, daily_days=[0, 1, 2, 3, 4, 5, 6], target_balance=5400.0, vat_pct=20.0,
                                   real_balance={"amount": 2458.0, "at": _dt.now().isoformat()}); ga.save_cfg(db, cf)
        r = ga.refresh(db)
        it = db.scalar(select(PayItem).where(PayItem.ref == ref))
        assert it and it.amount == 2500.0                 # 5400 − 2458×1.2 = 2450.4 → 2500
        db.delete(it); db.commit()
        cf = ga.cfg(db); cf["real_balance"] = {"amount": 5000.0, "at": _dt.now().isoformat()}; ga.save_cfg(db, cf)
        r = ga.refresh(db)
        assert r["pay_item"] is None and not db.scalar(select(PayItem).where(PayItem.ref == ref))
        cf = ga.cfg(db); cf.update(daily_topup=0.0, target_balance=0.0, real_balance=None); ga.save_cfg(db, cf)


def test_page_with_google_balance():
    from datetime import datetime as _dt
    from fastapi.testclient import TestClient
    from app.main import app
    from app.config import settings
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        cf = ga.cfg(db); cf["real_balance"] = {"amount": 2458.0, "at": _dt.now().isoformat()}; ga.save_cfg(db, cf)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.get("/marketing/google")
        assert r.status_code == 200 and "з ПДВ" in r.text and "2" in r.text
    with SessionLocal() as db:
        cf = ga.cfg(db); cf["real_balance"] = None; ga.save_cfg(db, cf)
