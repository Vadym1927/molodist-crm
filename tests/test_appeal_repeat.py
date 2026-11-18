from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import Appeal, AppealNote, SessionLocal


def test_appeal_repeat_and_merge():
    from app.services import appeals as ap
    with SessionLocal() as db:
        a = ap.create(db, {"phone": "+380954616299", "source": "Дзвінок", "cause": "Ціна", "interest": "мезотерапія"})
        b = ap.create(db, {"phone": "380954616299", "source": "Дзвінок", "cause": "Запис на процедуру"})
        assert b.id == a.id and getattr(b, "repeat", False) and "Повтор" in (a.marks or [])
        assert any("Повторне звернення" in n.text for n in db.query(AppealNote).filter(AppealNote.appeal_id == a.id))
        ap.set_status(db, a, "Записаний")
        c = ap.create(db, {"phone": "380954616299", "source": "Instagram"})        # перше закрите → нове звернення
        assert c.id != a.id
        # старі дублікати (до автообʼєднання) — кнопкою
        old1 = Appeal(phone="0671230099", source="Дзвінок", status="Новий", created_at=datetime.utcnow() - timedelta(days=2))
        old2 = Appeal(phone="+380671230099", source="Дзвінок", status="Новий")
        db.add_all([old1, old2]); db.commit()
        db.add(AppealNote(appeal_id=old2.id, text="передзвонити", kind="note")); db.commit()
        assert ap.merge_repeats(db, old1) == 1
        assert old2.deleted_at and db.query(AppealNote).filter(AppealNote.appeal_id == old1.id, AppealNote.text == "передзвонити").count() == 1
        ap1 = old1.id
    settings.admin_password = "owner-pass-ar"
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-ar"})
        assert cl.get(f"/appeals/{ap1}").status_code == 200
        r = cl.post("/appeals", data={"phone": "0671230099", "name": "Тест", "source": "Дзвінок", "back": "/appeals"}, follow_redirects=False)
        assert r.status_code == 303 and f"open={ap1}" in r.headers["location"]
