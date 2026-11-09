import io
import zipfile
from pathlib import Path

from app.models import Patient, PatientFile, SessionLocal
from app.services import bulk_docs


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def test_bulk_docs_scan_and_apply():
    with SessionLocal() as db:
        a = Patient(full_name="Архів Тест А", phone="+380000000801", cleverbox_id=970001)
        b = Patient(full_name="Архів Тест Б", phone="+380000000802")
        db.add_all([a, b]); db.flush()
        aid, bid = a.id, b.id
        db.commit()
    pdf = Path("tests/sample_plan.pdf").read_bytes()
    arc = _zip([("висновки/970001 висновок.pdf", b"%PDF-1.4 fake"), (f"{bid}_фото.jpg", b"\xff\xd8\xff"), ("999999.pdf", b"x"), ("__MACOSX/._x", b"y")])
    with SessionLocal() as db:
        sc = bulk_docs.scan(db, [("a.zip", arc)], "medical")
        assert sc["total"] == 3 and sc["no_patient"] == 1
        r = {x["name"]: x for x in sc["rows"]}
        assert r["970001 висновок.pdf"]["patient_id"] == aid and r["970001 висновок.pdf"]["category"] == "doc"
        assert r[f"{bid}_фото.jpg"]["patient_id"] == bid and r[f"{bid}_фото.jpg"]["via"] == "crm"
        rep = bulk_docs.apply(db, [("a.zip", arc)], "medical")
        assert rep["files"] == 2 and rep["no_patient"] == 1
        assert db.query(PatientFile).filter_by(patient_id=aid).count() == 1
        rep2 = bulk_docs.apply(db, [("a.zip", arc)], "medical")
        assert rep2["skipped_dup"] == 2
        arc2 = _zip([("970001 BeautyPlan.pdf", pdf)])
        sc2 = bulk_docs.scan(db, [("p.zip", arc2)], "plans")
        assert sc2["plans"] == 1
        rep3 = bulk_docs.apply(db, [("p.zip", arc2)], "plans")
        assert rep3["plans"] == 1
        from app.models import BeautyPlan
        assert db.query(BeautyPlan).filter_by(patient_id=aid).count() == 1
