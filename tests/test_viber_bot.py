"""Viber-бот → центр спілкування: підключення, підпис вебхука, вхідні, контакт, відповідь з CRM."""
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.main import app
from app.models import SessionLocal, ChatMessage, Appeal, Patient
from app.services import channels as ch, viber_bot as vb, meta_inbox as mi


class H:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url.rsplit("/", 1)[-1], json, headers))
        res = {"get_account_info": {"status": 0, "name": "MOLODIST", "uri": "molodist", "subscribers_count": 12},
               "set_webhook": {"status": 0}, "send_message": {"status": 0, "message_token": 555}}[url.rsplit("/", 1)[-1]]

        class R:
            def json(s):
                return res
        return R()


def _setup(db):
    c = next((x for x in ch.channels(db) if x["type"] == "viber_bot"), None) or ch.add(db, "viber_bot", "Viber-бот")
    ch.update(db, c["id"], None, True, {}, {"token": "vbtok"})
    return c


def test_connect_ingest_reply():
    with SessionLocal() as db:
        c = _setup(db)
        h = H()
        ok, msg = ch.test(db, c["id"], h)
        assert ok and "12 підписників" in msg
        assert h.calls[1][0] == "set_webhook" and vb.hook_id(db) in h.calls[1][1]["url"] and h.calls[0][2]["X-Viber-Auth-Token"] == "vbtok"
        assert vb.link(db) == "viber://pa?chatURI=molodist"
        db.add(Patient(full_name="Вайберенко Ольга", phone="+380500000991")); db.commit()
        ev = {"event": "message", "timestamp": 1762450000000, "message_token": 1001,
              "sender": {"id": "VB1==", "name": "Ольга"}, "message": {"type": "text", "text": "Скільки коштує пілінг?"}}
        m = vb.ingest(db, ev)
        assert m and m.thread == "vb:VB1==" and m.channel == "viber" and m.appeal_id
        assert vb.ingest(db, ev) is None                                   # дубль
        assert db.get(Appeal, m.appeal_id).source == "Viber"
        ct = {"event": "message", "message_token": 1002, "sender": {"id": "VB1==", "name": "Ольга"},
              "message": {"type": "contact", "contact": {"name": "Ольга", "phone_number": "380500000991"}}}
        m2 = vb.ingest(db, ct)
        assert m2.patient_id and db.get(Appeal, m2.appeal_id).phone
        h2 = H()
        out = mi.reply(db, "vb:VB1==", "Від 1 200 ₴", http=h2)
        assert out.status == "sent" and h2.calls[0][1]["receiver"] == "VB1==" and out.mid == "vb:555"
        hook = vb.hook_id(db)
    body = json.dumps({"event": "message", "message_token": 1003, "sender": {"id": "VB2", "name": "Ірина"},
                       "message": {"type": "text", "text": "Привіт"}}).encode()
    with TestClient(app) as cl:
        assert cl.post(f"/api/public/viber/webhook/{hook}", content=body, headers={"X-Viber-Content-Signature": "bad"}).status_code == 403
        assert cl.post("/api/public/viber/webhook/nope", content=body).status_code == 404
        sig = hmac.new(b"vbtok", body, hashlib.sha256).hexdigest()
        assert cl.post(f"/api/public/viber/webhook/{hook}", content=body, headers={"X-Viber-Content-Signature": sig}).json()["status"] == 0
    with SessionLocal() as db:
        assert db.query(ChatMessage).filter_by(thread="vb:VB2").count() == 1
