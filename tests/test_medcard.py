import io
from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, MedRecord, MedTemplate, PatientFile, User
from app.security import hash_password


def _png() -> bytes:
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (8, 8), (200, 100, 100)).save(b, "PNG"); return b.getvalue()


def test_medcard_and_photos():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Медична Пацієнтка", phone="+380501230000")
        db.add(p); db.commit(); pid = p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            if not db.query(User).filter_by(login="mc-admin").first():
                db.add(User(login="mc-admin", name="Адмін МК", role="admin", password_hash=hash_password("admin-pass-1"), active=True))
                db.commit()
        assert "Медична картка" in c.get(f"/patients/{pid}/medcard").text
        c.post(f"/patients/{pid}/medcard/summary", data={"med_allergies": "лідокаїн", "phototype": "III"})
        r = c.post(f"/patients/{pid}/medcard/record", data={"kind": "procedure", "at": "2026-10-20T12:30", "title": "Біоревіталізація",
                   "protocol": "Профайло 2 мл, 5 точок BAP", "drug_0": "Profhilo", "batch_0": "PF123", "dose_0": "2 мл",
                   "recommendations": "SPF 50", "consent": "1", "save_template": "Профайло — протокол", "photo_stage": "before", "photo_zone": "Обличчя"},
                   files=[("photos", ("a.png", _png(), "image/png"))], follow_redirects=False)
        assert r.status_code == 303
        page = c.get(f"/patients/{pid}/medcard").text
        assert "Біоревіталізація" in page and "PF123" in page and "лідокаїн" in page and "Профайло — протокол" in page
        assert "Профайло 2 мл" in c.get(f"/patients/{pid}/medcard/print").text
        c.post(f"/patients/{pid}/photos", data={"stage": "after", "zone": "Обличчя"}, files=[("files", ("b.png", _png(), "image/png"))])
        ph = c.get(f"/patients/{pid}/photos").text
        assert 'id="cmp"' in ph and "Обличчя" in ph
        with SessionLocal() as db:
            rec = db.query(MedRecord).filter_by(patient_id=pid).one(); rid = rec.id
            assert rec.drugs[0]["name"] == "Profhilo" and rec.consent and db.query(MedTemplate).count() >= 1
            fs = db.query(PatientFile).filter_by(patient_id=pid).all()
            assert {f.stage for f in fs} == {"before", "after"} and any(f.record_id == rid for f in fs)
        bad = c.post(f"/patients/{pid}/medcard/record/{rid}/delete", data={"del_password": "x"}, follow_redirects=False)
        assert "delerr=" in bad.headers["location"]
        c.get("/logout")
        c.post("/login", data={"login": "mc-admin", "password": "admin-pass-1"})
        assert c.get(f"/patients/{pid}/medcard").status_code == 403                 # адміну медкартка недоступна
        assert "лідокаїн" not in c.get(f"/patients/{pid}").text
