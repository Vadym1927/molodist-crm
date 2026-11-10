"""Telegram Business: акаунт клініки → центр спілкування, звернення «Telegram», вихідні адміністратора, відповідь з CRM."""
from app.models import SessionLocal, Appeal, ChatMessage, Patient
from app.services import tg_business as tb, meta_inbox as mi


def test_business_flow():
    with SessionLocal() as db:
        c = tb.on_connection(db, {"id": "bc-1", "user": {"id": 1000, "first_name": "MOLODIST", "username": "molodist_ua"},
                                  "is_enabled": True, "rights": {"can_reply": True}})
        assert c["enabled"] and c["can_reply"] and c["user_id"] == 1000
        p = Patient(full_name="Телеграм Пацієнтка", phone="+380500007799", telegram_username="tg_patient_x")
        db.add(p); db.commit()
        m = tb.ingest(db, {"message_id": 1, "date": 1794300000, "chat": {"id": 555, "type": "private", "username": "tg_patient_x"},
                           "from": {"id": 555, "first_name": "Оля", "username": "tg_patient_x"}, "text": "Скільки коштує чистка?"})
        assert m and m.direction == "in" and m.patient_id == p.id and m.thread == "tg:555"
        a = db.get(Appeal, m.appeal_id)
        assert a.source == "Telegram"
        assert tb.ingest(db, {"message_id": 1, "date": 1794300000, "chat": {"id": 555, "type": "private"},
                              "from": {"id": 555, "first_name": "Оля"}, "text": "дубль"}) is None
        o = tb.ingest(db, {"message_id": 2, "date": 1794300100, "chat": {"id": 555, "type": "private", "username": "tg_patient_x"},
                           "from": {"id": 1000, "first_name": "MOLODIST"}, "text": "Від 1 200 ₴"})
        assert o.direction == "out" and o.appeal_id == a.id
        assert tb.ingest(db, {"message_id": 3, "chat": {"id": -100, "type": "group"}, "from": {"id": 5}, "text": "x"}) is None

        class H:
            def post(self, url, data=None):
                assert data["business_connection_id"] == "bc-1" and data["chat_id"] == "555"
                return type("R", (), {"json": lambda s: {"ok": True, "result": {"message_id": 9, "chat": {"id": 555}}}})()
        r = mi.reply(db, "tg:555", "Записати вас на суботу?", None, http=H())
        assert r.status == "sent" and r.mid == "555:9"
        assert any(t["thread"] == "tg:555" and t["channel"] == "telegram" for t in mi.threads(db))


def test_meta_signature_two_keys(monkeypatch):
    import hashlib, hmac
    with SessionLocal() as db:
        monkeypatch.setattr(mi, "_sig_keys", lambda db_: ["fb-secret", "ig-secret"])
        body = b'{"object":"instagram","entry":[]}'
        sig = "sha256=" + hmac.new(b"ig-secret", body, hashlib.sha256).hexdigest()
        assert mi.check_signature(db, body, sig)
        assert not mi.check_signature(db, body, "sha256=" + "0" * 64)
        assert "Instagram App Secret" in mi.signature_hint(db, body)


def test_ig_login_token_names_reply(monkeypatch):
    from app.services import meta_inbox as mi_
    with SessionLocal() as db:
        db.add(ChatMessage(channel="instagram", thread="IGS77", direction="in", text="Привіт", mid="igm-77"))
        db.commit()
        monkeypatch.setattr(mi_, "_ig_token", lambda db_: "IGTOK")
        monkeypatch.setattr(mi_, "replies_enabled", lambda db_: True)

        class H:
            def get(self, url, params=None):
                assert "graph.instagram.com" in url and params["access_token"] == "IGTOK"
                return type("R", (), {"json": lambda s: {"username": "olia_k", "name": "Оля"}})()
            def post(self, url, params=None, json=None):
                assert url.endswith("/me/messages") and "graph.instagram.com" in url and json["recipient"]["id"] == "IGS77"
                return type("R", (), {"json": lambda s: {"message_id": "m1"}})()
        assert mi_.refresh_names(db, http=H()) >= 1
        assert db.scalars(__import__("sqlalchemy").select(ChatMessage).where(ChatMessage.thread == "IGS77")).first().name == "@olia_k · Оля"
        r = mi_.reply(db, "IGS77", "Вітаю!", None, http=H())
        assert r.status == "sent" and r.mid == "m1"
