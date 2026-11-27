from datetime import datetime

from app.models import SessionLocal, Call
from app.services import telephony as tel


def test_typed_scoring_and_kpi(monkeypatch):
    with SessionLocal() as db:
        d = tel._raw(db); d.update(ai_on=True, ai_engine="crm"); tel._save(db, d)
        c1 = Call(provider="ringostat", direction="in", status="answered", duration=90, at=datetime.now(), employee="Тип Адмін",
                  transcript="Адміністратор: MOLODIST, добрий день\nПацієнт: Підтвердіть запис на завтра")
        c2 = Call(provider="ringostat", direction="in", status="answered", duration=30, at=datetime.now(), employee="Тип Адмін",
                  transcript="Пацієнт: Це постачальник")
        db.add_all([c1, c2]); db.commit()
        prompts = []

        def fake(prompt):
            prompts.append(prompt)
            if "Підтвердіть" in prompt:
                return {"type": "existing", "booked": True, "comment": "Швидко підтвердила, не нагадала про підготовку.",
                        "criteria": [{"name": "Привітання", "max": 15, "score": 15}, {"name": "Точність", "max": 25, "score": 25},
                                     {"name": "Альтернатива", "max": 20, "score": None}, {"name": "Підготовка", "max": 15, "score": 0},
                                     {"name": "Домовленість", "max": 25, "score": 25}]}
            return {"type": "other", "comment": "Дзвінок постачальника"}
        tel.evaluate(db, c1, claude=fake)
        tel.evaluate(db, c2, claude=fake)
        assert "[existing]" in prompts[0] and "null" in prompts[0]
        assert c1.ai_score == round(100 * 65 / 80, 1) and c1.ai_meta["call_type"] == "existing"   # «альтернатива» не застосовна
        assert c2.ai_score is None and c2.ai_meta["call_type"] == "other"
        k = next(x for x in tel.kpi(db, datetime.now().date(), datetime.now().date()) if x["name"] == "Тип Адмін")
        assert k["rated"] == 1 and k["score_srv"] == c1.ai_score and k["score_sale"] is None


def test_manual_score_and_no_kpi():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        c = Call(provider="ringostat", direction="in", status="answered", duration=60, at=datetime.now(), employee="Ручна Адмін",
                 transcript="Адміністратор: MOLODIST", ai_score=15)
        db.add(c); db.commit(); cid = c.id
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Оцінка вручну" in cl.get("/telephony").text
        cl.post(f"/telephony/calls/{cid}/manual", data={"score": "85", "no_kpi": "1", "note": "нормальна розмова"})
    with SessionLocal() as db:
        c = db.get(Call, cid)
        assert c.ai_score == 85 and c.ai_meta["no_kpi"] and c.ai_meta["manual_score"]["was"] == 15
        assert not any(x["name"] == "Ручна Адмін" and x["rated"] for x in tel.kpi(db, datetime.now().date(), datetime.now().date()))
