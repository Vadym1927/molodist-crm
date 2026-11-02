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
             [1231, 11737, "", "analiz.pdf", "14.06.2026 10:10", "doc", "pdf", "Бондар"],
             [9999, 11738, "https://cbox.5jk9u.upcloudobjects.com/x.jpg", "x.jpg", "", "img", "jpg", ""],
             [1231, 11739, "https://evil.example.com/x.jpg", "x.jpg", "", "img", "jpg", ""]]
    r = cb_photos.run(J(), files)
    assert r["saved"] == 1 and r["not_image"] == 2 and r["no_patient"] == 1
    assert cb_photos.run(J(), files)["exists"] == 1
    with SessionLocal() as db:
        f = db.query(PatientFile).one()
        assert f.kind == "photo" and f.cb_file_id == 11736 and str(f.taken_at) == "2026-06-14"
