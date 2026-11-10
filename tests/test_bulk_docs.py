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
        arc2 = _zip([("380000000801__001__Архів Тест А Anti-age.pdf", pdf)])
        sc2 = bulk_docs.scan(db, [("p.zip", arc2)], "plans")
        assert sc2["plans"] == 1 and sc2["rows"][0]["via"] == "phone" and sc2["rows"][0]["patient_id"] == aid
        rep3 = bulk_docs.apply(db, [("p.zip", arc2)], "plans")
        assert rep3["plans"] == 1
        from app.models import BeautyPlan
        assert db.query(BeautyPlan).filter_by(patient_id=aid).count() == 1


def test_bulk_docs_dups():
    with SessionLocal() as db:
        p = Patient(full_name="Дубль Тест", phone="+380000000811")
        db.add(p); db.flush()
        for _ in range(2):
            db.add(PatientFile(patient_id=p.id, kind="doc", path="/nope/x.pdf", caption="380000000811__009__Дубль Тест 1.pdf"))
        db.add(PatientFile(patient_id=p.id, kind="doc", path="/nope/y.pdf", caption="звичайний.pdf"))
        db.add(PatientFile(patient_id=p.id, kind="doc", path="/nope/y.pdf", caption="звичайний.pdf"))
        db.commit()
        assert len([f for f in bulk_docs.find_dups(db)["files"] if f.patient_id == p.id]) == 1
        bulk_docs.remove_dups(db)
        assert db.query(PatientFile).filter_by(patient_id=p.id).count() == 3


def test_bulk_docs_name_mismatch():
    assert bulk_docs.name_matches("380975010540__028__Калишенко Софія Сергіївна 09.2025.pdf", "Калишенко Софія Сергіївна")
    assert bulk_docs.name_matches("380630247170__052__Солом_яна_Ольга_Володимирівна_03_2026.pdf", "Ольга Солом'яна")
    assert not bulk_docs.name_matches("380975010540__028__Калишенко Софія Сергіївна 09.2025.pdf", "Мартинюк Лариса Борисівна")
    with SessionLocal() as db:
        m = Patient(full_name="Невідповідна Лариса", phone="+380000000821")
        db.add(m); db.commit()
        arc = _zip([("380000000821__001__Інша Особа Петрівна.pdf", b"%PDF fake")])
        sc = bulk_docs.scan(db, [("m.zip", arc)], "medical")
        assert sc["mismatch"] == 1
        rep = bulk_docs.apply(db, [("m.zip", arc)], "medical")
        assert rep["files"] == 0 and rep["no_patient"] == 1


def test_plans_check_runs():
    with SessionLocal() as db:
        rows = bulk_docs.plans_check(db)
        assert isinstance(rows, list)
        assert isinstance(bulk_docs.fix_plan_dates(db), list)
        assert isinstance(bulk_docs.plans_unmatched(db), list)
        a = bulk_docs.plans_audit(db)
        assert "plans" in a and "consult_without_plan" in a
