from datetime import datetime

import httpx

from app.models import SessionLocal, Call
from app.services import stt_gladia as stt, telephony as tel


def test_roles_admin_by_clinic_words_and_merge():
    u = [{"start": 0, "speaker": 1, "text": "Алло"}, {"start": 1, "speaker": 0, "text": "Добрий день, MOLODIST, Вікторія."},
         {"start": 3, "speaker": 0, "text": "Чим допомогти?"}, {"start": 5, "speaker": 1, "text": "Хочу на консультацію"}]
    assert stt._roles(u, "out") == "Пацієнт: Алло\nАдміністратор: Добрий день, MOLODIST, Вікторія. Чим допомогти?\nПацієнт: Хочу на консультацію"
    assert stt._roles([{"start": 0, "speaker": 3, "text": "Слухаю вас"}, {"start": 1, "speaker": 4, "text": "Ціна?"}], "in").startswith("Адміністратор:")


def test_run_transcribes_and_evaluates(monkeypatch):
    def handler(req: httpx.Request):
        u = str(req.url)
        if "ringostat" in u:
            return httpx.Response(200, content=b"ID3audio")
        if u.endswith("/upload"):
            assert req.headers["x-gladia-key"] == "gk-test"
            return httpx.Response(200, json={"audio_url": "https://api.gladia.io/file/1"})
        if u.endswith("/pre-recorded"):
            return httpx.Response(201, json={"id": "j1", "result_url": "https://api.gladia.io/v2/pre-recorded/j1"})
        return httpx.Response(200, json={"status": "done", "result": {"transcription": {"utterances": [
            {"start": 0, "speaker": 0, "text": "MOLODIST, добрий день"}, {"start": 2, "speaker": 1, "text": "Скільки коштує IPL?"}]}}})
    real = httpx.Client
    monkeypatch.setattr(stt.httpx, "Client", lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    scored = []
    monkeypatch.setattr(tel, "evaluate", lambda db, c: scored.append(c.id))
    with SessionLocal() as db:
        d = tel._raw(db); d.update(ai_on=True, ai_engine="crm"); tel._save(db, d)
        stt.set_key(db, "gk-test")
        c = Call(provider="ringostat", direction="in", status="answered", duration=95, at=datetime.now(),
                 record_url="https://app.ringostat.com/rec/1.mp3")
        short = Call(provider="ringostat", direction="in", status="answered", duration=8, at=datetime.now(),
                     record_url="https://app.ringostat.com/rec/2.mp3")
        db.add_all([c, short]); db.commit()
        assert stt.run(db) >= 1
        db.refresh(c); db.refresh(short)
        assert c.transcript == "Адміністратор: MOLODIST, добрий день\nПацієнт: Скільки коштує IPL?" and c.ai_meta["stt"] == "gladia"
        assert c.id in scored and short.transcript is None
        stt.set_key(db, None)
        assert stt.key(db) == ""


def test_stt_stats_counts_queue_and_errors():
    from datetime import datetime
    with SessionLocal() as db:
        db.add(Call(provider="ringostat", direction="in", at=datetime.now(), status="answered", record_url="https://x.ringostat.com/r.mp3", duration=60,
                    ai_meta={"stt_error": "HTTP 403", "stt_tries": 1}))
        db.add(Call(provider="ringostat", direction="in", at=datetime.now(), status="answered", record_url="https://x.ringostat.com/r2.mp3", duration=60,
                    transcript="Адміністратор: добрий день", ai_meta={"stt": "gladia"}))
        db.commit()
        st = stt.stats(db)
    assert st["done"] >= 1 and st["queue"] >= 1 and st["errors"] >= 1 and "403" in st["last_error"]
