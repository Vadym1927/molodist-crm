"""Перенесення медкарт CleverBox → CRM: ключ, клієнти, імпорт з полями форми, повторне надсилання без дублів."""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, MedRecord, Doctor
from app.services import med_import as mim

ROW = {"id": "92697", "date_add": "1794398566", "client_id": "90597", "type": "consult", "form_id": "15", "deleted": "0",
       "subtitle": "", "cfl_109": "60%-100%", "anamnes": "без реакцій", "final": "SPF 50", "date": "11.11.2026", "time": "14:02",
       "author": {"id": "49", "title": "Чугунова Ганна"}, "_type": "Консультація", "_ctype": "Щоденник (стратосфера)", "files": False,
       "_doctor": {"id": "49", "title": "Чугунова Ганна"}}
FORMS = {"15": [{"name": "cfl_109", "title": "Парамерти:"}, {"name": "anamnes", "title": "Анамнез"}, {"name": "final", "title": "Висновки та рекомендації"}]}


def test_ingest_and_endpoint():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        if not db.query(Patient).filter_by(cleverbox_id=90597).first():
            db.add(Patient(full_name="Медімпорт Аліна", phone="+380500009597", cleverbox_id=90597))
        if not db.query(Doctor).filter_by(name="Чугунова Ганна").first():
            db.add(Doctor(name="Чугунова Ганна"))
        db.commit()
        r = mim.ingest(db, [ROW, {**ROW, "id": "92698", "deleted": "1"}, {**ROW, "id": "92699", "client_id": "1"}], FORMS)
        assert r["new"] == 1 and r["skipped_deleted"] == 1 and r["no_patient"] == 1
        m = db.query(MedRecord).filter_by(cb_id=92697).one()
        assert m.kind == "consultation" and m.anamnesis == "без реакцій" and m.recommendations == "SPF 50"
        assert "Парамерти: 60%-100%" in m.protocol and "::" not in m.protocol and m.title == "Щоденник (стратосфера)" and m.doctor_id
        assert m.at.strftime("%d.%m.%Y %H:%M") == "11.11.2026 14:02"
        assert mim.ingest(db, [ROW], FORMS)["updated"] == 1 and db.query(MedRecord).filter_by(cb_id=92697).count() == 1
    with TestClient(app) as c:
        assert c.post("/api/public/medcard/import", json={"rows": [ROW]}, headers={"X-Import-Key": "bad"}).status_code == 403
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        k = c.post("/data/medcard-import/key").json()["key"]
        h = {"X-Import-Key": k, "Origin": "https://molodist.cbox.online"}
        assert 90597 in c.get("/api/public/medcard/import/clients", headers=h).json()["ids"]
        r = c.post("/api/public/medcard/import", json={"rows": [ROW], "forms": FORMS}, headers=h)
        assert r.status_code == 200 and r.json()["updated"] == 1
        assert r.headers["access-control-allow-origin"] == "https://molodist.cbox.online"
        pid = SessionLocal().query(Patient).filter_by(cleverbox_id=90597).one().id
        t = c.get(f"/patients/{pid}/medcard").text
        assert "Щоденник (стратосфера)" in t and "з CleverBox" in t


def test_run_files_links_record(monkeypatch):
    from app.services import cb_photos
    from app.models import PatientFile

    class J:
        def log(self, m):
            pass

    def fake_run(job, files, by):
        with SessionLocal() as db:
            for f in files:
                p = db.query(Patient).filter_by(cleverbox_id=f[0]).one()
                db.add(PatientFile(patient_id=p.id, kind="photo", path="x.jpg", cb_file_id=f[1]))
            db.commit()
        return {"saved": len(files)}
    monkeypatch.setattr(cb_photos, "run", fake_run)
    with SessionLocal() as db:
        if not db.query(Patient).filter_by(cleverbox_id=90597).first():
            db.add(Patient(full_name="Медімпорт Аліна", phone="+380500009597", cleverbox_id=90597)); db.commit()
        mim.ingest(db, [ROW], FORMS)
    rep = mim.run_files(J(), [[92697, 99565, "https://cbox.x.upcloudobjects.com/a.jpg", "IMG.jpeg", "16.03.2024 16:20", "img"], [1, 2, "u", "n"]])
    assert rep["saved"] == 1 and rep["linked"] == 1
    with SessionLocal() as db:
        m = db.query(MedRecord).filter_by(cb_id=92697).one()
        assert db.query(PatientFile).filter_by(cb_file_id=99565).one().record_id == m.id
