from datetime import date

from app.models import SessionLocal
from app.models import Patient, PatientFile
from app.services import ingest

TXT = """Висновок за результатами проведеного аналізу шкіри
Деталі клініки
КЛІНІКА ПРАКТИКУЮЧИЙ
MOLODIST Дарʼя Колган
Лікар
ЕЛЕКТРОННА ПОШТА
ТЕЛЕФОН
0933756437
Інформація про клієнта
НАЗВА
Тестана Аналізенко
ЕЛЕКТРОННА ПОШТА
ТЕЛЕФОН
Деталі сеансу
НАЗВА ДАТА ПРОВЕДЕННЯ СЕСІЇ
21.08.2026, 15:22
КОМЕНТАРІ
"""


def test_parse_analysis_text():
    a = ingest.parse_analysis_text(TXT)
    assert a["name"] == "Тестана Аналізенко" and a["phone"] is None
    assert a["at"].strftime("%d.%m.%Y %H:%M") == "21.08.2026 15:22"
    assert a["doctor"] == "Дарʼя Колган"
    assert ingest.parse_analysis_text("Бюті-план Загальні витрати") is None


def test_attach_analysis(tmp_path):
    with SessionLocal() as db:
        p = Patient(full_name="Аналізенко Тестана Петрівна", phone="+380501112233")
        db.add(p)
        db.commit()
        an = ingest.parse_analysis_text(TXT)
        f1 = tmp_path / "a.pdf"
        f1.write_bytes(b"x")
        r = ingest.attach_analysis(db, an, str(f1), "a.pdf")
        assert r.patient_id == p.id and "прикріплено" in r
        pf = db.query(PatientFile).filter_by(patient_id=p.id).one()
        assert pf.caption == "Аналіз шкіри OBSERV · 21.08.2026" and pf.category == "doc" and pf.taken_at == date(2026, 8, 21)
        f2 = tmp_path / "b.pdf"
        f2.write_bytes(b"x")
        r2 = ingest.attach_analysis(db, an, str(f2), "b.pdf")
        assert "вже є" in r2 and not f2.exists()
        assert db.query(PatientFile).filter_by(patient_id=p.id).count() == 1


def test_attach_doc_by_caption_and_text(tmp_path, monkeypatch):
    with SessionLocal() as db:
        p = Patient(full_name="Висновченко Марта Іванівна", phone="+380671234567")
        db.add(p)
        db.commit()
        f1 = tmp_path / "x.pdf"
        f1.write_bytes(b"x")
        r = ingest.attach_doc(db, str(f1), "Консультативний висновок.pdf", "Висновченко Марта", kind="pdf")
        assert r.patient_id == p.id and "прикріплено" in r
        monkeypatch.setattr(ingest, "_pdf_text", lambda path: "Консультативний висновок\nПацієнт: Марта Висновченко\nТел. 067 123 45 67")
        f2 = tmp_path / "scan_001.pdf"
        f2.write_bytes(b"x")
        r2 = ingest.attach_doc(db, str(f2), "scan_001.pdf", None, kind="pdf")
        assert r2.patient_id == p.id
        r3 = ingest.ingest_doc(db, b"doc", "zzz.docx", "без імені")
        assert r3.patient_id is None and r3.file_id


def test_tg_ingest_settings():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as client:
        client.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        _tg_ingest_flow(client)


def _tg_ingest_flow(client):
    r = client.get("/settings/tg-ingest")
    import re
    assert r.status_code == 200 and "файли з чатів" in r.text, re.findall(r"<title>.*?</title>", r.text)
    r = client.post("/settings/tg-ingest", data={"add_id": "-100555", "add_title": "Консультації", "reply": "1"}, follow_redirects=False)
    assert r.status_code == 303
    from app.services import tg_ingest
    with SessionLocal() as db:
        assert "-100555" in tg_ingest.enabled(db)
