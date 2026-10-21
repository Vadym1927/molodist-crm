"""Скрипти в задачах, канали (бот пацієнтів + TurboSMS), API бота, розсилки з атрибуцією."""
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.integrations import messaging
from app.main import app
from app.models import (SessionLocal, Patient, Task, MessageLog, Campaign, CampaignRecipient, Visit, Check,
                        BeautyPlan)
from app.services import scripts as sc, campaigns as camp

PDF = Path(__file__).parent / "sample_plan.pdf"
needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="потрібен tests/sample_plan.pdf")


class FakeHTTP:
    """Підміна httpx.post у messaging: Telegram Bot API і TurboSMS."""
    def __init__(self):
        self.calls, self.blocked = [], set()

    def post(self, url, json=None, data=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json, "data": data, "headers": headers})
        if "api.telegram.org" in url:
            if data["chat_id"] in self.blocked:
                return R(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"})
            return R(200, {"ok": True, "result": {"message_id": len(self.calls)}})
        if url.endswith("message/send.json"):
            return R(200, {"response_code": 0, "response_status": "OK", "response_result": [
                {"phone": json["recipients"][0], "response_code": 0, "message_id": f"ts-{len(self.calls)}"}]})
        if url.endswith("message/status.json"):
            return R(200, {"response_result": [{"message_id": m, "status": "Delivered", "channel": "viber"}
                                               for m in json["messages"]]})
        if url.endswith("user/balance.json"):
            return R(200, {"response_result": {"balance": 512.5}})
        return R(404, {})

    def of(self, kind):
        return [c for c in self.calls if kind in c["url"]]


class R:
    def __init__(self, code, js):
        self.status_code, self._js = code, js

    def json(self):
        return self._js


@pytest.fixture
def net(monkeypatch):
    f = FakeHTTP()
    monkeypatch.setattr(messaging.httpx, "post", f.post)
    for k, v in dict(patient_bot_token="pbt", turbosms_token="tst", turbosms_sender_sms="MOLODIST",
                     turbosms_sender_viber="MOLODIST", admin_password="owner-pass-1").items():
        monkeypatch.setattr(settings, k, v)
    monkeypatch.setattr(messaging, "is_quiet_time", lambda now=None: False)
    return f


@pytest.fixture
def c(net):
    from app import security
    security.login_limiter.fails.clear(); security.ip_limiter.fails.clear()
    with TestClient(app) as client:
        client.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        yield client


def test_helpers():
    assert messaging.normalize_phone("067 111-22-33") == "380671112233"
    assert messaging.normalize_phone("+38 (050) 123 45 67, 0931112233") == "380501234567"
    assert messaging.normalize_phone("671112233") == "380671112233"
    assert messaging.normalize_phone("12345") is None
    assert messaging.sms_parts("а" * 70) == 1 and messaging.sms_parts("а" * 71) == 2 and messaging.sms_parts("a" * 160) == 1
    assert sc.first_name("Олена Даниленко") == "Олена" and sc.first_name("Даниленко Олена") == "Олена"
    assert sc.first_name("Шевчук Марія") == "Марія"


@needs_pdf
def test_task_script_and_send(c, net):
    c.post("/upload", files={"files": ("plan.pdf", PDF.read_bytes(), "application/pdf")})
    with SessionLocal() as db:
        plan = db.query(BeautyPlan).one()
        plan.patient.phone = "0671112233"
        db.add(Task(title="Записати", kind="not_booked", patient_id=plan.patient_id, plan_id=plan.id, status="open",
                    assignee_role="admin"))
        db.commit()
        tid = db.query(Task).one().id
    page = c.get("/tasks").text
    assert "Скрипт і повідомлення" in page
    with SessionLocal() as db:
        scripts = sc.for_task(db, db.get(Task, tid), "Анна Адмін")
    call = next(s for s in scripts if s.channel == "call").text
    msg = next(s for s in scripts if s.channel == "message").text
    assert "Олена" in call and "Анна" in call and "Гігієна обличчя" in call and "з 5 процедур" in call
    assert "Дарʼя Колган" in call and "курс" in call.lower()
    assert not re.search(r"\{[^}]+\}", call + msg)                   # усі підстановки заповнені
    assert "Пролактин" not in call + msg and "Достінекс" not in call + msg   # мед. нотатки не потрапляють
    # відправка з задачі: пацієнтка не в боті → Viber з резервом SMS, без маркетингового футера
    r = c.post(f"/tasks/{tid}/send", data={"text": msg, "channel": "auto", "close": "1"}, follow_redirects=False)
    assert r.status_code == 303
    body = net.of("send.json")[-1]["json"]
    assert body["recipients"] == ["380671112233"] and "viber" in body and body["sms"]["hybrid_ttl"] > body["viber"]["ttl"]
    assert settings.marketing_footer not in body["viber"]["text"]
    assert net.of("send.json")[-1]["headers"]["Authorization"] == "Bearer tst"
    with SessionLocal() as db:
        t = db.get(Task, tid)
        assert t.status == "done" and "Надіслано повідомлення (viber)" in t.result
        log = db.query(MessageLog).one()
        assert log.status == "sent" and log.marketing is False and log.task_id == tid
    assert "Звʼязок з пацієнткою" in c.get(f"/patients/{plan.patient_id}").text


def test_bot_api_and_campaign(c, net):
    # ключ для бота пацієнтів
    import re as _re
    shown = c.post("/access/api/keys", data={"name": "OpenClaw", "scopes": ["bot"]}).text
    key = _re.search(r"<pre[^>]*>(mcrm_[\w-]+)</pre>", shown).group(1)
    h = {"X-API-Key": key}
    with SessionLocal() as db:
        now = datetime.now()
        a = Patient(full_name="Анна Телеграмна", phone="0501111111", cb_last_visit=now - timedelta(days=200))
        b = Patient(full_name="Ірина Вайберна", phone="0502222222", cb_last_visit=now - timedelta(days=150))
        d = Patient(full_name="Олена Відписана", phone="0503333333", cb_last_visit=now - timedelta(days=120))
        e = Patient(full_name="Марія Нещодавня", phone="0504444444", cb_last_visit=now - timedelta(days=100))
        f = Patient(full_name="Ольга Безномера", cb_last_visit=now - timedelta(days=300))
        g = Patient(full_name="Тетяна Заблокувала", phone="0505555555", cb_last_visit=now - timedelta(days=95))
        db.add_all([a, b, d, e, f, g]); db.commit()
        db.add(MessageLog(patient_id=e.id, channel="sms", text="x", status="sent", marketing=True)); db.commit()
        ids = {p.full_name: p.id for p in (a, b, d, e, f, g)}

    # бот привʼязує Telegram за телефоном з контакту
    assert c.post("/api/v1/bot/link", headers=h, json={"chat_id": "1001", "phone": "+380501111111", "username": "anna"}).json()["linked"]
    assert c.post("/api/v1/bot/link", headers=h, json={"chat_id": "1006", "phone": "380505555555"}).json()["linked"]
    assert c.post("/api/v1/bot/link", headers=h, json={"chat_id": "1003", "phone": "0503333333"}).json()["linked"]
    assert c.post("/api/v1/bot/unsubscribe", headers=h, json={"chat_id": "1003"}).json()["ok"]
    nf = c.post("/api/v1/bot/link", headers=h, json={"chat_id": "2000", "phone": "0990000000"}).json()
    assert nf["linked"] is False
    new = c.post("/api/v1/bot/link", headers=h, json={"chat_id": "2000", "phone": "0990000000", "name": "Нова Клієнтка",
                                                       "create_if_missing": True}).json()
    assert new["linked"] and new["created"]
    view = c.get("/api/v1/bot/patient/1001", headers=h).json()
    assert view["name"] == "Анна" and view["subscribed"] is True
    assert c.get("/api/v1/patients", headers=h).status_code == 403          # ключ бота не читає базу
    net.blocked.add("1006")

    # розсилка «сплячим»: Telegram → Viber
    c.post("/campaigns", data={"name": "Сплячі", "segment": "sleeping", "text": "{імʼя}, ми скучили! Запрошуємо на діагностику.",
                               "ch_telegram": "1", "ch_viber": "1"})
    with SessionLocal() as db:
        cid = db.query(Campaign).one().id
    page = c.get(f"/campaigns/{cid}").text
    assert "Хто отримає" in page and "відписані — 1" in page and "отримували розсилку" in page
    assert "немає Telegram/телефону" in page                                 # Ольга без номера
    with SessionLocal() as db:
        aud = camp.build_audience(db, db.get(Campaign, cid))
        names = {p.full_name: ch for p, ch in aud.rows}
    assert names == {"Анна Телеграмна": "telegram", "Ірина Вайберна": "viber", "Тетяна Заблокувала": "telegram"}

    # адмін може підготувати, але не запустити
    c.post("/access/users", data={"login": "adm", "name": "Адмін", "role": "admin", "password": "password-123", "active": "1"})
    c.get("/logout"); c.post("/login", data={"login": "adm", "password": "password-123"})
    assert c.get(f"/campaigns/{cid}").status_code == 200
    assert c.post(f"/campaigns/{cid}/launch", data={"confirm": "1"}).status_code == 403
    c.get("/logout"); c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})

    from app.services import jobs
    import app.main as m
    orig = jobs.start
    m.jobs.start = lambda title, fn, *a, **k: fn(None, *a, **k)          # синхронно в тесті
    try:
        assert c.post(f"/campaigns/{cid}/launch", data={"confirm": "1"}, follow_redirects=False).status_code == 303
    finally:
        m.jobs.start = orig
    with SessionLocal() as db:
        cc = db.get(Campaign, cid)
        assert cc.status == "done"
        st = {r.patient.full_name: (r.channel, r.status) for r in cc.recipients}
        assert st["Анна Телеграмна"] == ("telegram", "sent") and st["Ірина Вайберна"] == ("viber", "sent")
        # Тетяна заблокувала бота → Viber/SMS не обрано для неї в розсилці (лише telegram) → помилка + відвʼязка
        assert st["Тетяна Заблокувала"][1] == "failed"
        assert db.get(Patient, ids["Тетяна Заблокувала"]).telegram_chat_id is None
        assert db.query(MessageLog).filter(MessageLog.marketing.is_(True)).count() == 4    # 3 нові + 1 старий
    tg = [x for x in net.of("api.telegram.org") if x["data"]["chat_id"] == "1001"][-1]["data"]["text"]
    assert tg.startswith("Анна, ми скучили") and settings.marketing_footer not in tg
    vb = net.of("send.json")[-1]["json"]
    assert vb["viber"]["text"].startswith("Ірина") and settings.marketing_footer in vb["viber"]["text"]
    assert vb["sequence_id"].startswith(f"crm-c{cid}-")

    # атрибуція: Ірина записалась після розсилки і заплатила
    with SessionLocal() as db:
        db.add(Visit(source="cb_visit", external_id="777-1", patient_id=ids["Ірина Вайберна"], service_name="Консультація",
                     starts_at=datetime.now() + timedelta(days=3), status="booked"))
        db.add(Check(id=9001, date=datetime.now(), amount=1500, patient_id=ids["Ірина Вайберна"]))
        db.commit()
        n = camp.refresh_statuses(db)
        s = camp.stats(db, db.get(Campaign, cid))
    assert n == 1 and s.sent == 2 and s.booked == 1 and s.revenue == 1500 and s.conversion == 50.0
    assert "Записались" in c.get("/campaigns").text and "512" in c.get("/campaigns").text   # баланс TurboSMS
    # повторна розсилка — ліміт частоти: отримали лише ті, кому не доставилось (Тетяна, тепер через Viber)
    c.post(f"/campaigns/{cid}/copy")
    with SessionLocal() as db:
        cp = db.query(Campaign).order_by(Campaign.id.desc()).first()
        assert [(p.full_name, ch) for p, ch in camp.build_audience(db, cp).rows] == [("Тетяна Заблокувала", "viber")]


def test_scripts_page_owner_only(c):
    assert "Довідник процедур" in c.get("/scripts").text
    c.post("/scripts/notes", data={"keyword": "плазмоліфтинг|prp", "why": "PRP курсом раз на 2 тижні.", "benefit": "свіжість"})
    with SessionLocal() as db:
        assert sc.note_for(db, "Плазмоліфтинг обличчя").why.startswith("PRP")
