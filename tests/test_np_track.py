from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import SessionLocal, SiteOrder, Task


def test_np_track_flow(monkeypatch):
    from app.services import np_track as npt, shop_front as sf, shop_cabinet as sc
    out = []
    monkeypatch.setattr(sc, "_deliver", lambda db, phone, text, *a, **k: (out.append(text) or "sms"))
    monkeypatch.setattr(sf, "notify", lambda *a, **k: 0)
    codes = {}
    monkeypatch.setattr(npt, "_codes", lambda c, ttns, http=None: {t: codes[t] for t, _ in ttns if t in codes})
    with TestClient(app):
        with SessionLocal() as db:
            mk = lambda n, t: SiteOrder(source="crm", ext_id=n, number=n, created_at=datetime.now(), status="Відправлено", paid=True, total=500,
                                        name="Т П", phone="380501110000", items=[], raw={"ttn": t})
            a, b, r = mk("NP1", "2045001"), mk("NP2", "2045002"), mk("NP3", "2045003")
            db.add_all([a, b, r]); db.commit()
            c = sf.cfg(db); c.update(order_msg_on=True, np_key="k"); sf.save_cfg(db, c)
            codes.update({"2045001": {"code": 7, "text": "Прибув у відділення", "wh": "12"},
                          "2045002": {"code": 9, "text": "Відправлення отримано", "wh": ""},
                          "2045003": {"code": 102, "text": "Відмова одержувача", "wh": ""}})
            rep = npt.job(db, sf.cfg(db)); db.commit()
            assert rep["arrived"] == 1 and rep["received"] == 1 and rep["refused"] == 1
            assert out == ["Замовлення NP1 чекає у відділенні НП №12"]
            assert b.status == "Отримано" and b.raw.get("review_due") and b.raw.get("recv_at")
            assert db.scalar(select(Task).where(Task.source_key == f"shop_np:{r.id}"))
            npt.job(db, sf.cfg(db)); db.commit()                          # повторно — без дублів
            assert len(out) == 1 and len(db.scalars(select(Task).where(Task.source_key == f"shop_np:{r.id}")).all()) == 1
            b.raw = {**b.raw, "recv_at": (datetime.now() - timedelta(days=4)).isoformat(timespec="minutes")}; db.commit()
            assert npt.job(db, sf.cfg(db))["closed"] == 1 and b.status == "Закрито"
            c = sf.cfg(db); c.update(order_msg_on=False, np_key=""); sf.save_cfg(db, c)


def test_cart_sms_short(monkeypatch):
    from app.services import shop_cabinet as sc
    from app.integrations import messaging as m
    sent = []
    monkeypatch.setattr(m, "send_turbosms", lambda phone, text, *a, **k: (sent.append(text) or type("R", (), {"ok": True})()))
    with TestClient(app):
        with SessionLocal() as db:
            assert sc._deliver(db, "380501112299", "довгий текст для бота", "🛒 ", sms="Ваш кошик збережено. Оформити: molodist.store/checkout/") == "sms"
    assert sent == ["Ваш кошик збережено. Оформити: molodist.store/checkout/"] and len(sent[0]) <= 70
