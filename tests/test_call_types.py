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
