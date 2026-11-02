import httpx

from app.models import SessionLocal, Patient, PatientFile
from app.services import cb_photos


class J:
    def log(self, m):
        pass


def test_cb_photos(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    with SessionLocal() as db:
        db.add(Patient(full_name="Фото Пацієнт", cleverbox_id=1231)); db.commit()
    monkeypatch.setattr(httpx.Client, "get", lambda self, url: httpx.Response(200, content=b"\xff\xd8jpegdata", request=httpx.Request("GET", url)))
    files = [[1231, 11736, "https://cbox.5jk9u.upcloudobjects.com/molodist/clients/1231_1.jpg", "photo.jpg", "14.06.2026 10:10", "img", "jpg", "Бондар"],
             [1231, 11737, "", "analiz.pdf", "14.06.2026 10:10", "file", "pdf", "Бондар"],
             [1231, 11740, "https://cbox.5jk9u.upcloudobjects.com/molodist/clients/1231_2.pdf", "analiz2.pdf", "14.06.2026 10:11", "file", "pdf", "Бондар"],
             [9999, 11738, "https://cbox.5jk9u.upcloudobjects.com/x.jpg", "x.jpg", "", "img", "jpg", ""],
             [1231, 11739, "https://evil.example.com/x.jpg", "x.jpg", "", "img", "jpg", ""],
             [1231, 11741, "https://cbox.5jk9u.upcloudobjects.com/molodist//photo/15987_1.jpg", "15987_1.jpg", "31.10.2026 18:53", "img", "after"]]
    r = cb_photos.run(J(), files)
    assert r["saved"] == 3 and r["not_image"] == 2 and r["no_patient"] == 1
    assert cb_photos.run(J(), files)["exists"] == 3
    with SessionLocal() as db:
        f = db.query(PatientFile).filter(PatientFile.cb_file_id == 11736).one()
        assert f.kind == "photo" and str(f.taken_at) == "2026-06-14"
        assert db.query(PatientFile).filter(PatientFile.cb_file_id == 11740).one().kind == "doc"
        assert db.query(PatientFile).filter(PatientFile.cb_file_id == 11741).one().stage == "after"


def test_cb_photos_by_record(monkeypatch, tmp_path):
    from datetime import datetime
    from app.config import settings
    from app.models import Visit
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    with SessionLocal() as db:
        p = Patient(full_name="До Після", cleverbox_id=7771); db.add(p); db.commit()
        db.add(Visit(source="cb_visit", external_id="15987-3", patient_id=p.id, starts_at=datetime(2026, 10, 31, 18), status="done")); db.commit()
        pid = p.id
    monkeypatch.setattr(httpx.Client, "get", lambda self, url: httpx.Response(200, content=b"\xff\xd8jpeg", request=httpx.Request("GET", url)))
    r = cb_photos.run(J(), [[15987, 14114, "https://cbox.5jk9u.upcloudobjects.com/molodist/before/15987_1.jpg", "15987_1.jpg", "31.10.2026 18:53", "img", "before"],
                            [1, 14115, "https://cbox.5jk9u.upcloudobjects.com/molodist/after/1_1.jpg", "1_1.jpg", "", "img", "after"]], "record")
    assert r["saved"] == 1 and r["no_patient"] == 1
    with SessionLocal() as db:
        f = db.query(PatientFile).filter(PatientFile.cb_file_id == 14114).one()
        assert f.patient_id == pid and f.stage == "before"


def test_cb_photos_relink(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    with SessionLocal() as db:
        a = Patient(full_name="Пацієнт А", cleverbox_id=8801); b = Patient(full_name="Пацієнт Б", cleverbox_id=8802)
        db.add_all([a, b]); db.commit()
        db.add(PatientFile(patient_id=a.id, kind="photo", path="x.jpg", cb_file_id=55501)); db.commit(); bid = b.id
    r = cb_photos.run(J(), [[8802, 55501, "https://cbox.5jk9u.upcloudobjects.com/molodist/before/1.jpg", "1.jpg", "", "img", "before"]])
    assert r["relinked"] == 1
    with SessionLocal() as db:
        assert db.query(PatientFile).filter(PatientFile.cb_file_id == 55501).one().patient_id == bid
