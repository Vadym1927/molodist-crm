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
