import io

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, PatientFile, User
from app.security import hash_password
from app.services import patient_photos as pp


def _png() -> bytes:
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (8, 8), (10, 100, 100)).save(b, "PNG"); return b.getvalue()


def test_guess():
    assert pp.guess(PatientFile(kind="photo", path="x/1_zgoda.jpg", caption="Згода на процедуру")) == "consent"
    assert pp.guess(PatientFile(kind="doc", path="x/analiz.pdf")) == "doc"
    assert pp.guess(PatientFile(kind="photo", path="x/a.jpg", zone="Живіт")) == "body"
    assert pp.guess(PatientFile(kind="photo", path="x/a.jpg")) == "face"


def test_library_category_delete():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Фото Пацієнтка", phone="+380501239999"); db.add(p); db.commit(); pid = p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post(f"/patients/{pid}/photos", data={"category": "body", "back": f"/patients/{pid}/photos"},
               files=[("files", ("a.png", _png(), "image/png")), ("files", ("b.png", _png(), "image/png"))])
        with SessionLocal() as db:
            ids = [f.id for f in db.query(PatientFile).filter_by(patient_id=pid).order_by(PatientFile.id)]
            assert len(ids) == 2 and {f.category for f in db.query(PatientFile).filter_by(patient_id=pid)} == {"body"}
        page = c.get(f"/patients/{pid}/photos").text
        assert 'id="cmp"' in page and "Оберіть два фото" in page
        assert "Фото пацієнтка".lower() in c.get("/settings/patient-photos?tab=body").text.lower()
        assert "Фото пацієнтів" in c.get("/settings").text
        c.post("/files/category", data={"ids": [ids[0]], "category": "consent", "back": "/settings/patient-photos?tab=body"})
        with SessionLocal() as db:
            assert db.get(PatientFile, ids[0]).category == "consent"
        bad = c.post("/files/delete", data={"ids": [ids[1]], "del_password": "x"}, follow_redirects=False)
        assert "delerr=" in bad.headers["location"]
        with SessionLocal() as db:
            db.query(User).filter(User.login == settings.owner_login).one().delete_pw_hash = hash_password("del-1111"); db.commit()
        c.post("/files/delete", data={"ids": [ids[1]], "del_password": "del-1111", "back": f"/patients/{pid}/photos"})
        with SessionLocal() as db:
            assert db.get(PatientFile, ids[1]) is None and db.get(PatientFile, ids[0]) is not None
            db.query(User).filter(User.login == settings.owner_login).one().delete_pw_hash = None; db.commit()
