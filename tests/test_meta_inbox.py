"""Вебхук Meta (Messenger / Instagram) → центр спілкування; ручні відповіді; публічні privacy / data-deletion."""
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, ChatMessage, Appeal, Patient
from app.services import meta_inbox as mi, channels as ch


class FakeHttp:
    def __init__(self):
        self.posts = []

    def get(self, url, params=None):
        class R:
            def json(self_inner):
                return {"username": "anna_test", "name": "Анна"}
        return R()

    def post(self, url, params=None, json=None):
        self.posts.append((url, json))

        class R:
            def json(self_inner):
                return {"recipient_id": "1", "message_id": "m_out_1"}
        return R()


def _setup(db):
    c = next(x for x in ch.channels(db) if x["type"] == "meta")
    ch.update(db, c["id"], None, True, {}, {"page_token": "EAAtest", "app_secret": "s3cret"})
    return c


def test_webhook_verify_ingest_reply():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        _setup(db)
        vt = mi.verify_token(db)
        db.add(Patient(full_name="Анна Тестова", instagram="https://instagram.com/anna_test/")); db.commit()
    with TestClient(app) as c:
        r = c.get("/api/public/meta/webhook", params={"hub.mode": "subscribe", "hub.verify_token": vt, "hub.challenge": "12345"})
        assert r.status_code == 200 and r.text == "12345"
        assert c.get("/api/public/meta/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "bad", "hub.challenge": "1"}).status_code == 403
        body = json.dumps({"object": "instagram", "entry": [{"id": "PAGE1", "messaging": [
            {"sender": {"id": "IGSID77"}, "recipient": {"id": "PAGE1"}, "timestamp": 1762450000000,
             "message": {"mid": "mid.1", "text": "Скільки коштує чистка?"}}]}]}).encode()
        assert c.post("/api/public/meta/webhook", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403
    with SessionLocal() as db:
        n = mi.ingest(db, json.loads(body), http=FakeHttp())
        assert n == 1 and mi.ingest(db, json.loads(body), http=FakeHttp()) == 0          # дубль не пишемо
        m = db.query(ChatMessage).one()
        assert m.name.startswith("@anna_test") and m.patient_id and m.appeal_id
        assert db.get(Appeal, m.appeal_id).source == "Instagram"
        fh = FakeHttp()
        out = mi.reply(db, "IGSID77", "Добрий день! 1500 ₴", user_id=None, http=fh)
        assert out.status == "sent" and fh.posts and fh.posts[0][1]["recipient"]["id"] == "IGSID77"
        mi.set_replies(db, False)
        out2 = mi.reply(db, "IGSID77", "ще", http=FakeHttp())
        assert out2.status == "paused"
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    with TestClient(app) as c:
        assert c.post("/api/public/meta/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/chats?t=IGSID77").text
        assert "Скільки коштує чистка?" in page and "@anna_test" in page
        assert "Політика конфіденційності" in TestClient(app).get("/privacy").text
        assert "Видалення даних" in TestClient(app).get("/data-deletion").text


def _ev(sid, mid, **msg):
    return {"object": "instagram", "entry": [{"id": "PAGE1", "messaging": [{"sender": {"id": sid}, "recipient": {"id": "PAGE1"},
                                                                          "timestamp": 1762450000000, "message": {"mid": mid, **msg}}]}]}


def test_ignore_non_direct_and_appeal_rules():
    from datetime import datetime
    from app.models import Task
    with SessionLocal() as db:
        _setup(db)
        fh = FakeHttp()
        # не Direct: реакція, сторіз, лайк, ехо адміна, згадка
        assert mi.ingest(db, {"object": "instagram", "entry": [{"id": "PAGE1", "messaging": [
            {"sender": {"id": "U1"}, "reaction": {"mid": "x", "action": "react", "reaction": "love"}}]}]}, http=fh) == 0
        assert mi.ingest(db, _ev("U1", "m1", text="🔥", reply_to={"story": {"id": "s1", "url": "u"}}), http=fh) == 0
        assert mi.ingest(db, _ev("U1", "m2", attachments=[{"type": "like_heart"}]), http=fh) == 0
        assert mi.ingest(db, _ev("U1", "m3", text="Пишу першою", is_echo=True), http=fh) == 0
        assert mi.ingest(db, _ev("U1", "m4", attachments=[{"type": "story_mention", "payload": {"url": "u"}}]), http=fh) == 0
        assert db.query(ChatMessage).count() == 0 and db.query(Appeal).count() == 0
        # справжнє вхідне → звернення; друге вхідне — те саме відкрите звернення
        assert mi.ingest(db, _ev("U1", "m5", text="Хочу на консультацію"), http=fh) == 1
        assert mi.ingest(db, _ev("U1", "m6", text="Можна завтра?"), http=fh) == 1
        assert db.query(Appeal).count() == 1
        a = db.query(Appeal).one()
        a.closed_at = datetime.utcnow(); db.commit()
        assert mi.ingest(db, _ev("U1", "m7", text="Ще питання"), http=fh) == 1
        assert db.query(Appeal).count() == 2                    # закрите → нове звернення
        # пацієнт з відкритою задачею → нового звернення не створюємо
        p = Patient(full_name="Задачна Пацієнтка", instagram="zadacha"); db.add(p); db.commit()
        db.add(Task(title="Передзвонити", patient_id=p.id, status="open")); db.commit()

        class H2(FakeHttp):
            def get(self, url, params=None):
                class R:
                    def json(s):
                        return {"username": "zadacha"}
                return R()
        assert mi.ingest(db, _ev("U2", "m8", text="Привіт"), http=H2()) == 1
        m = db.query(ChatMessage).filter_by(thread="U2").one()
        assert m.patient_id == p.id and m.appeal_id is None and db.query(Appeal).count() == 2


def test_hook_log_and_summary():
    from app.services import meta_inbox as mi
    from app.models import SessionLocal
    with SessionLocal() as db:
        mi.hook_log(db, "event", mi.summarize({"object": "instagram", "entry": [{"id": "1", "messaging": [
            {"sender": {"id": "2"}, "message": {"mid": "x", "text": "hi"}}, {"sender": {"id": "2"}, "read": {"mid": "x"}}]}]}))
        ev = mi.hook_events(db)
    assert ev and ev[0]["kind"] == "event" and "message×1" in ev[0]["note"] and "read×1" in ev[0]["note"]
    assert "hi" not in ev[0]["note"]


def test_suggest_translit_and_close_thread():
    from app.services import appeals as ap
    with SessionLocal() as db:
        p = Patient(full_name="Тестенко Олена Іванівна", phone="+380500000777")
        db.add(p); db.commit()
        assert mi.to_cyr("Olena Testenko") == "олена тестенко"
        assert p.id in [x.id for x in mi.suggest(db, "Olena Testenko")]
        a = ap.create(db, {"name": "x", "source": "Facebook"})
        db.add(ChatMessage(channel="facebook", thread="777000111", direction="in", text="hi", appeal_id=a.id)); db.commit()
        st = next(s["name"] for s in ap.dirs(db)["statuses"] if s.get("type") == "other")
        assert mi.close_thread(db, "777000111", st, "Спам / робот", None) == 1
        db.refresh(a)
        assert a.closed_at and a.deny_reason == "Спам / робот"
        assert next(t for t in mi.threads(db) if t["thread"] == "777000111")["closed"]


def test_subscription_dict_fields():
    class R:
        def __init__(s, js): s._js = js
        def json(s): return s._js
    class H:
        def get(s, url, params=None): return R({"data": [{"name": "MOLODIST CRM", "subscribed_fields": [{"name": "messages", "version": "v27.0"}]}]})
    with SessionLocal() as db:
        _setup(db)
        ok, msg = mi.subscription(db, H())
    assert ok and "messages" in msg


def test_sessions_split_and_templates():
    from app.services import appeals as ap, reply_templates as rt
    from datetime import datetime, timedelta
    with SessionLocal() as db:
        p = Patient(full_name="Сесієнко Марія", phone="+380500000888"); db.add(p); db.commit()
        a = ap.create(db, {"name": "x", "source": "Instagram", "patient_id": p.id})
        t0 = datetime(2026, 11, 1, 10, 0)
        db.add_all([ChatMessage(at=t0, channel="instagram", thread="888001", direction="in", text="1", patient_id=p.id, appeal_id=a.id),
                    ChatMessage(at=t0 + timedelta(minutes=5), channel="instagram", thread="888001", direction="out", text="2", patient_id=p.id, appeal_id=a.id, status="sent")])
        db.commit()
        st = next(s["name"] for s in ap.dirs(db)["statuses"] if s.get("type") == "success")
        mi.close_thread(db, "888001", st, None, None)
        db.add(ChatMessage(at=datetime.utcnow(), channel="instagram", thread="888001", direction="in", text="3", patient_id=p.id)); db.commit()
        msgs = db.scalars(__import__("sqlalchemy").select(ChatMessage).where(ChatMessage.thread == "888001").order_by(ChatMessage.at, ChatMessage.id)).all()
        ss = mi.sessions(msgs, {a.id: db.get(Appeal, a.id)})
        assert len(ss) == 2 and not ss[0]["closed"] and ss[1]["closed"] and ss[1]["result"] == st and len(ss[1]["msgs"]) == 2
        ctx = rt.context(db, p, None, "Олена Адмін")
        assert ctx["імʼя"] == "Марія" and ctx["адмін"] == "Олена"
        assert rt.render("Вітаємо, {імʼя}!", rt.context(db, None, "@nick", None)) == "Вітаємо!"
        assert rt.for_channel(db, "instagram")
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        _sess_http(c, p)


def _sess_http(c, p):
    from app.services import reply_templates as rt
    c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
    r = c.get("/settings/reply-templates"); assert r.status_code == 200 and "Шаблони відповідей" in r.text
    r = c.post("/settings/reply-templates", data={"id": ["a1"], "title": ["Тест"], "cat": ["Ціни"], "text": ["Привіт, {імʼя}"], "ch_0": ["instagram"]},
               follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert [t["title"] for t in rt.for_channel(db, "instagram")] == ["Тест"] and not rt.for_channel(db, "sms")
    r = c.get("/chats?t=888001"); assert r.status_code == 200 and "Сесієнко" in r.text and "Шаблони" in r.text and "Діалог закрито" in r.text
    r = c.get(f"/patients/{p.id}"); assert r.status_code == 200 and "Діалоги Instagram" in r.text and "триває" in r.text


def test_admin_echo_saved_as_outgoing():
    with SessionLocal() as db:
        fh = FakeHttp()
        assert mi.ingest(db, _ev("U9", "e1", text="Добрий день, скільки коштує?"), http=fh) == 1
        echo = {"object": "instagram", "entry": [{"id": "PAGE1", "messaging": [{"sender": {"id": "PAGE1"}, "recipient": {"id": "U9"},
                "timestamp": 1762450100000, "message": {"mid": "e2", "text": "Від 1 200 ₴", "is_echo": True}}]}]}
        assert mi.ingest(db, echo, http=fh) == 1
        assert mi.ingest(db, echo, http=fh) == 0                      # дубль
        out = db.query(ChatMessage).filter(ChatMessage.mid == "e2").one()
        first = db.query(ChatMessage).filter(ChatMessage.mid == "e1").one()
        assert out.direction == "out" and out.thread == "U9" and out.appeal_id == first.appeal_id
