from app.models import Patient, PatientFile, SessionLocal
from app.services import cb_photos


def test_audit_finds_foreign_files():
    with SessionLocal() as db:
        a = Patient(full_name="Аудит Фото А", phone="+380000000701", cleverbox_id=990001)
        b = Patient(full_name="Аудит Фото Б", phone="+380000000702", cleverbox_id=990002)
        db.add_all([a, b]); db.flush()
        db.add(PatientFile(patient_id=a.id, kind="photo", path="/x/1.jpg", cb_file_id=880001))   # файл Б лежить у А
        db.add(PatientFile(patient_id=b.id, kind="photo", path="/x/2.jpg", cb_file_id=880002))   # правильно
        db.commit()
        aid = a.id
    rep = cb_photos.audit([[990002, 880001, "", "", "", "img"], [990002, 880002, "", "", "", "img"], [990001, 880003, "", "", "", "img"]])
    assert rep["wrong_patients"] == 1 and rep["rows"][0]["patient_id"] == aid and rep["rows"][0]["owners"] == ["Аудит Фото Б"]
    assert rep["missing_in_crm"] == 1
