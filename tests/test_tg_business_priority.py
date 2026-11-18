import httpx

from fastapi.testclient import TestClient

from app.main import app
from app.models import ChatMessage, Patient, SessionLocal


def test_service_messages_go_to_clinic_telegram_first(monkeypatch):
    from app.integrations import messaging as m
    from app.services import tg_business as tb, shop_cabinet as sc
    sent = []
    monkeypatch.setattr(m, "sending_enabled", lambda: True)
    monkeypatch.setattr(tb, "send", lambda db, th, text, http=None: (sent.append((th, text)) or (True, "1:1", None)))
    monkeypatch.setattr(m, "send_turbosms", lambda *a, **k: sent.append(("sms", a[1])) or m.SendResult(True, "sms"))
    with TestClient(app):
        monkeypatch.setattr(m.settings, "telegram_bot_token", "x")
        with SessionLocal() as db:
            tb.save_cfg(db, {**tb.cfg(db), "connection_id": "c1", "enabled": True, "can_reply": True})
            p = Patient(full_name="ТГ Бізнес Тест", phone="380731119900")
            q = Patient(full_name="Без чату", phone="380731119901")
            db.add_all([p, q]); db.flush()
            db.add(ChatMessage(channel="telegram", thread="tg:5551", direction="in", text="привіт", patient_id=p.id)); db.commit()
            r = m.send_to_patient(p, "Ваш чек: …")
            assert r.ok and r.channel == "tg_business" and sent[-1] == ("tg:5551", "Ваш чек: …")
            assert db.query(ChatMessage).filter(ChatMessage.thread == "tg:5551", ChatMessage.direction == "out").count() == 1
            assert m.send_to_patient(p, "Акція!", marketing=True).channel != "tg_business"      # розсилки — не сюди
            assert sc._deliver(db, "380731119900", "Код: 1234") == "telegram" and sent[-1] == ("tg:5551", "Код: 1234")
            assert sc._deliver(db, "380731119901", "Код: 5678") == "sms" and sent[-1] == ("sms", "Код: 5678")
            tb.save_cfg(db, {**tb.cfg(db), "enabled": False})


def test_tg_avatar(monkeypatch):
    import httpx
    from app.services import meta_inbox as mi
    from app.config import settings
    monkeypatch.setattr(settings, "telegram_bot_token", "x")
    mi._AV.pop("tg:777", None)
    def h(req):
        u = str(req.url)
        if "getUserProfilePhotos" in u:
            return httpx.Response(200, json={"ok": True, "result": {"photos": [[{"file_id": "s", "width": 160}, {"file_id": "b", "width": 640}]]}})
        if "getFile" in u:
            assert "file_id=s" in u
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/a.jpg"}})
        return httpx.Response(200, content=b"\xff\xd8jpg", headers={"content-type": "application/octet-stream"})
    with SessionLocal() as db:
        res = mi.avatar(db, "tg:777", httpx.Client(transport=httpx.MockTransport(h)))
    assert res == (b"\xff\xd8jpg", "image/jpeg")
