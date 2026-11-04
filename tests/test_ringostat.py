from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Call, Patient
from app.services import telephony as tel


def test_ringostat_events_flow():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add(Patient(full_name="Дзвінкова Пацієнтка", phone="+380671112233")); db.commit()
        tel.save(db, "ringostat", {"project_id": "1", "auth_key": "k"}, "Дзвінок", True, False)
        hook = tel.public(db)["hook"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Webhooks 2.0" in c.get("/settings/telephony").text
        u = f"/api/public/telephony/ringostat/{hook}"
        body = {"call_id": "$cdr_id", "uid": "777", "caller": "380671112233", "callee": "0443334455", "type": "in", "status": "$disposition"}
        assert c.post(u + "?event=ringing", json=body).json()["ok"]
        live = c.get("/telephony/live").json()["calls"]
        assert live and live[0]["name"] == "Дзвінкова Пацієнтка" and live[0]["status"] == "ringing"
        c.post(u + "?event=answer", json=body)
        c.post(u + "?event=end", json={**body, "status": "PROPER", "dialog": "95", "record_link": "https://rec/1.wav", "employee_fio": "Адмін Оля"})
        assert c.get("/telephony/live").json()["calls"] == []
    with SessionLocal() as db:
        calls = db.query(Call).all()
        assert len(calls) == 1 and calls[0].status == "answered" and calls[0].duration == 95 and calls[0].record_url.endswith("1.wav")
        assert calls[0].employee == "Адмін Оля" and calls[0].patient_id


def test_call_ai_popup_create_patient_and_kpi():
    from datetime import date
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        tel.save(db, "ringostat", {"project_id": "1", "auth_key": "k"}, "Дзвінок", True, False)
        tel.save_extra(db, [], True, "критерії", 70, False)
        hook = tel.public(db)["hook"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        u = f"/api/public/telephony/ringostat/{hook}"
        body = {"uid": "900", "caller": "380509998877", "type": "in"}
        c.post(u + "?event=ringing", json=body)
        lv = c.get("/telephony/live").json()["calls"][0]
        assert lv["pid"] is None
        r = c.post(f"/telephony/calls/{lv['id']}/patient", json={"last": "Новенька", "first": "Ірина"}).json()
        assert r["ok"] and r["name"] == "Новенька Ірина"
        c.post(u + "?event=end", json={**body, "status": "PROPER", "dialog": "60", "employee_fio": "Оля Адмін"})
        c.post(u + "?event=ai", json={"uid": "900", "caller": "380509998877", "summary": "Запис на чистку",
                                       "transcript": "WEBVTT\n\n00:00.000 --> 00:02.000\n<v Адмін>Добрий день, MOLODIST\n"})
    with SessionLocal() as db:
        cl = db.query(Call).one()
        assert cl.summary == "Запис на чистку" and "Адмін: Добрий день" in cl.transcript and cl.patient_id
        tel.evaluate(db, cl, claude=lambda prompt: {"score": 55, "comment": "Не запропонувала час", "booked": False})
        assert cl.ai_score == 55
        k = tel.kpi(db, date.today().replace(day=1), date.today())
        assert k[0]["name"] == "Оля Адмін" and k[0]["score"] == 55 and k[0]["low"] == 1
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/telephony").text
        assert "KPI дзвінків" in t and "AI 55/100" in t and "Діалог (" in t and "Добрий день, MOLODIST" in t
        assert "Дзвінки · 1" in c.get(f"/patients/{cl.patient_id}").text


def test_agent_api_calls_ai():
    from app.models import ApiKey
    from app.security import new_api_key
    with SessionLocal() as db:
        tel.save_extra(db, [], True, "критерії", 70, False, "agent")
        c = Call(provider="ringostat", phone="380501112233", direction="in", status="answered", duration=60, record_url="https://r/1.wav")
        db.add(c); db.commit(); cid = c.id
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="openclaw", prefix=prefix, key_hash=hsh, scopes=["calls"])); db.commit()
    with TestClient(app) as cl:
        h = {"X-API-Key": raw}
        j = cl.get("/api/v1/calls/pending-ai", headers=h).json()
        assert j["criteria"] == "критерії" and j["calls"][0]["id"] == cid and j["calls"][0]["phone_last4"] == "2233"
        r = cl.post(f"/api/v1/calls/{cid}/ai", headers=h, json={"transcript": "Адмін: Добрий день", "score": 50, "comment": "слабко", "booked": False})
        assert r.json()["ai_score"] == 50
        assert cl.get("/api/v1/calls/pending-ai", headers=h).json()["calls"] == []
        assert cl.get("/api/v1/patients", headers=h).status_code == 403


def test_ringing_without_event_param():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        tel.save(db, "ringostat", {"project_id": "1", "auth_key": "k"}, "Дзвінок", True, False)
        hook = tel.public(db)["hook"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post(f"/api/public/telephony/ringostat/{hook}", json={"caller": "380631110000", "status": "$disposition", "dialog": "$billsec"})
        assert c.get("/telephony/live").json()["calls"]


def test_ringostat_form_fields_like_openclaw():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        tel.save(db, "ringostat", {"project_id": "1", "auth_key": "k"}, "Дзвінок", True, False)
        hook = tel.public(db)["hook"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post(f"/api/public/telephony/ringostat/{hook}?event=ringing",
               json={"call_number": "380731110099", "recive_number": "380933756437", "reciver": "", "ID": "ua20_-1.1", "type": "in", "project": "210608", "connected": ""})
        lv = c.get("/telephony/live").json()["calls"]
        assert lv and lv[0]["phone"] == "380731110099"


def test_popup_extra_and_notifications():
    from datetime import datetime, timedelta
    from app.models import Appointment
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Інфо Пацієнтка", phone="+380671230099", cb_note="Любить зелений чай"); db.add(p); db.commit()
        db.add(Appointment(patient_id=p.id, start=datetime.now() - timedelta(days=20), end=datetime.now() - timedelta(days=20, hours=-1),
                           services=[{"name": "Чистка"}], status="done")); db.commit()
        tel.save(db, "ringostat", {"project_id": "1", "auth_key": "k"}, "Дзвінок", False, False)
        hook = tel.public(db)["hook"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        u = f"/api/public/telephony/ringostat/{hook}"
        c.post(u + "?event=ringing", json={"call_number": "380671230099", "ID": "x1", "type": "in"})
        lv = c.get("/telephony/live").json()["calls"][0]
        assert lv["tag"] and "Чистка" in lv["last"] and lv["note"] == "Любить зелений чай" and lv["visits"] == 1
        c.post(u + "?event=end", json={"call_number": "380671230099", "ID": "x1", "type": "in", "status": "NO ANSWER", "dialog": "0"})
        h = c.get("/notifications").text
        assert "Пропущений дзвінок" in h and "Інфо Пацієнтка" in h


def test_popup_fields_setting():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add(Patient(full_name="Поля Пацієнтка", phone="+380671239999", cb_note="Замітка")); db.commit()
        tel.save(db, "ringostat", {"project_id": "1", "auth_key": "k"}, "Дзвінок", False, False)
        hook = tel.public(db)["hook"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Що показувати у вікні" in c.get("/settings/telephony").text
        c.post("/settings/telephony", data={"provider": "ringostat", "source": "Дзвінок", "popup_cfg": "1", "popup_fields": ["tag"]})
        c.post(f"/api/public/telephony/ringostat/{hook}?event=ringing", json={"call_number": "380671239999", "ID": "pf1", "type": "in"})
        lv = c.get("/telephony/live").json()["calls"][0]
        assert lv["tag"] and lv["note"] is None and lv["last"] is None


def test_agent_failed_record_requeues():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import SessionLocal, Call, ApiKey
    from app.security import new_api_key
    from app.services import telephony as tel
    with SessionLocal() as db:
        tel.save_extra(db, [], True, "критерії", 70, False, "agent")
        c = Call(provider="ringostat", phone="380501119999", direction="in", status="answered", duration=50, record_url="https://r/2.wav")
        db.add(c); db.commit(); cid = c.id
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="openclaw-rq", prefix=prefix, key_hash=hsh, scopes=["calls"])); db.commit()
    with TestClient(app) as cl:
        h = {"X-API-Key": raw}
        for i in range(3):
            assert cid in [x["id"] for x in cl.get("/api/v1/calls/pending-ai", headers=h).json()["calls"]]
            cl.post(f"/api/v1/calls/{cid}/ai", headers=h, json={"comment": "Запис недоступний"})
        assert cid not in [x["id"] for x in cl.get("/api/v1/calls/pending-ai", headers=h).json()["calls"]]   # після 3 спроб — стоп
        with SessionLocal() as db:
            tel.requeue(db, db.get(Call, cid))
        assert cid in [x["id"] for x in cl.get("/api/v1/calls/pending-ai", headers=h).json()["calls"]]


def test_staff_and_lines():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, Call, User
    from app.services import telephony as tel
    from datetime import date
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            uid = db.query(User).filter(User.login == settings.owner_login).one().id
            hook = tel.public(db)["hook"]
        r = c.post("/settings/telephony/staff", data={"s_user": [str(uid)], "s_ext": ["777001"], "s_alias": ["Оля Адмін"],
                                                      "l_number": ["+380 44 111 22 33"], "l_name": ["Основна"], "l_user": [str(uid)]}, follow_redirects=False)
        assert r.status_code == 303
        c.post(f"/api/public/telephony/ringostat/{hook}?event=end", json={"uid": "st1", "caller": "380509990001", "callee": "380441112233",
                                                                            "status": "PROPER", "dialog": "40", "type": "in", "manager": "777001", "employee_fio": ""})
        c.post(f"/api/public/telephony/ringostat/{hook}?event=end", json={"uid": "st2", "caller": "380509990002", "callee": "380441112233",
                                                                            "status": "NO ANSWER", "dialog": "0", "type": "in", "manager": "0"})
        with SessionLocal() as db:
            a = db.query(Call).filter(Call.ext_id == "st1").one(); b = db.query(Call).filter(Call.ext_id == "st2").one()
            assert a.user_id == uid and a.line == "380441112233" and b.user_id is None
            name = db.get(User, uid).name
            k = {x["name"]: x for x in tel.kpi(db, date.today(), date.today())}
            assert k[name]["ans"] >= 1 and k[name]["miss"] >= 1          # пропущений — на відповідального за лінію
        page = c.get("/settings/telephony").text
        assert "Співробітники й лінії" in page and 'value="777001"' in page
        j = c.get("/telephony").text
        assert "відповідальний" in j and "Основна" in j


def test_manual_send_old_call_to_ai():
    from datetime import datetime, timedelta
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, Call, ApiKey
    from app.security import new_api_key
    from app.services import telephony as tel
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        tel.save_extra(db, [], True, "критерії", 70, False, "agent")
        c = Call(provider="ringostat", at=datetime.now() - timedelta(days=20), phone="380501117777", direction="in", status="answered",
                 duration=80, record_url="https://r/old.wav", ai_at=datetime.utcnow(), ai_comment="Запис недоступний")
        db.add(c); db.commit(); cid = c.id
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="openclaw-man", prefix=prefix, key_hash=hsh, scopes=["calls"])); db.commit()
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = {"X-API-Key": raw}
        assert cid not in [x["id"] for x in cl.get("/api/v1/calls/pending-ai", headers=h).json()["calls"]]
        assert "Відправити на AI-оцінку" in cl.get("/telephony").text
        assert cl.post(f"/telephony/calls/{cid}/evaluate").json()["ok"]
        assert cid in [x["id"] for x in cl.get("/api/v1/calls/pending-ai", headers=h).json()["calls"]]     # стара — але вручну
        assert "у черзі на AI-оцінку" in cl.get("/telephony").text


def test_ai_alert_recipients(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, Call, User
    from app.services import telephony as tel, alerts
    settings.admin_password = "owner-pass-1"
    sent = []
    monkeypatch.setattr(alerts, "_api", lambda m, **kw: sent.append(kw["data"]["chat_id"]) or True)
    monkeypatch.setattr(alerts, "send", lambda text: sent.append("owner") or 1)
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            u = User(login="al_admin", name="Алерт Адмін", role="admin", password_hash="x", telegram_chat_id="5550001"); db.add(u); db.commit(); uid = u.id
        assert "Відповідальний за обробку" in c.get("/settings/telephony").text
        c.post("/settings/telephony", data={"provider": "ringostat", "source": "Дзвінок", "ai_on": "1", "ai_alert": "1", "ai_alert_below": "60",
                                            "ai_alert_cfg": "1", "ai_alert_to": ["taker"], "ai_engine": "agent"})
        with SessionLocal() as db:
            assert tel.public(db)["ai_alert_to"] == ["taker"]
            cl = Call(provider="ringostat", phone="380501116666", direction="in", status="answered", duration=40, record_url="https://r/a.wav", user_id=uid)
            db.add(cl); db.commit()
            tel.agent_result(db, cl, {"score": 30, "comment": "слабко", "transcript": "Адмін: ..."})
        assert sent == ["5550001"]


def test_call_record_proxy(monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    from app.main import app
    from app.models import SessionLocal, Call, ApiKey
    from app.security import new_api_key

    class R:
        status_code, content, headers = 200, b"RIFFwav", {"content-type": "audio/wav"}

    class C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): assert "ringostat.com" in url; return R()
    monkeypatch.setattr(httpx, "Client", C)
    with SessionLocal() as db:
        a = Call(provider="ringostat", phone="380501115555", direction="in", status="answered", record_url="https://app.ringostat.com/recordings/x.wav?token=t")
        b = Call(provider="ringostat", phone="380501115556", direction="in", status="answered", record_url="https://evil.example.com/x.wav")
        db.add_all([a, b]); db.commit(); ia, ib = a.id, b.id
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="openclaw-rec", prefix=prefix, key_hash=hsh, scopes=["calls"])); db.commit()
    with TestClient(app) as cl:
        h = {"X-API-Key": raw}
        r = cl.get(f"/api/v1/calls/{ia}/record", headers=h)
        assert r.status_code == 200 and r.content == b"RIFFwav"
        assert cl.get(f"/api/v1/calls/{ib}/record", headers=h).status_code == 404
        assert cl.get(f"/api/v1/calls/{ia}/record").status_code in (401, 403)


def test_ai_criteria_rows_and_low_call_task(monkeypatch):
    from datetime import datetime as _dt
    from app.services import telephony as tel
    from app.models import SessionLocal as _SL, Call, Task, User
    rows = tel.crit_rows(tel.DEFAULT_CRITERIA)
    assert len(rows) == 6 and sum(r["pts"] for r in rows) == 100 and rows[0]["title"] == "Привітання"
    txt = tel.crit_text(rows, tel.crit_tail(tel.DEFAULT_CRITERIA))
    assert tel.crit_rows(txt) == rows and "не записано" in txt
    sent = []
    monkeypatch.setattr("app.services.alerts.send", lambda t: sent.append(t) or 1)
    with _SL() as db:
        u = db.query(User).first()
        tel.save_extra(db, [], True, txt, 60, True, "crm", None, None, str(u.id) if u else "taker", True)
        c = Call(provider="ringostat", at=_dt.now(), direction="in", status="answered", phone="380000000001", ai_score=40, ai_comment="погано", user_id=u.id if u else None)
        db.add(c); db.commit()
        tel.alert_low(db, c); tel.alert_low(db, c)
        ts = db.query(Task).filter(Task.source_key == f"call_low:{c.id}").all()
        assert len(ts) == 1 and ts[0].kind == "call_review" and ts[0].priority == "high"


def test_popup_custom_fields():
    from app.services import telephony as tel
    from app.models import SessionLocal as _SL, Patient
    with _SL() as db:
        tel.save_extra(db, [], False, tel.DEFAULT_CRITERIA, 60, True, "crm", None, None, None, None,
                       [{"key": "drink", "label": "Напій"}, {"key": "text", "text": "Запропонуйте акцію"}, {"key": "bogus"}])
        cfg = tel.public(db)["popup_custom"]
        assert [r["key"] for r in cfg] == ["drink", "text"]
        p = Patient(full_name="Поп Ап", drink="капучино", cb_mark="vip")
        assert tel.popup_extra_value(p, "drink") == "капучино" and tel.popup_extra_value(p, "mark") == "VIP"
        assert tel.popup_extra_value(p, "email") is None and tel.popup_extra_value(None, "text", "Привіт") == "Привіт"
