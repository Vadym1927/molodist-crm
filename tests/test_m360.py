from fastapi.testclient import TestClient

from app.main import app
from app.models import M360Assessment, Patient, SessionLocal
from app.services.m360 import core as m3


def _all_answers():
    out = []
    for m in m3.QUESTIONNAIRE["modules"]:
        for f in m["factors"]:
            for q in f["questions"]:
                o = q["options"][0]
                out.append({"module_key": m["key"], "factor_key": f["key"], "question_key": q["key"], "answer_value": o["value"],
                            "answer_label": o["label"], "answer_score": 999})          # бал із браузера ігнорується
    return out


def test_m360_full_flow_links_patient_and_builds_pdf():
    with TestClient(app) as c:
        assert "MOLODIST 360" in c.get("/_site/molodist-360/").text
        assert c.get("/_site/molodist-360/static/app.css").status_code == 200
        assert c.get("/_site/molodist-360/static/../../main.py").status_code == 404
        assert c.post("/_site/molodist-360/api/register", data={"first_name": "Тест", "phone": "0671112299"}).status_code == 400   # без згоди
        r = c.post("/_site/molodist-360/api/register", data={"first_name": "Олена", "last_name": "Тестова", "phone": "067 111 22 99",
                                                              "email": "o@example.com", "consent_personal_data": "true"})
        assert r.status_code == 200
        tok = r.json()["assessment_id"]
        for a in _all_answers():
            assert c.post(f"/_site/molodist-360/api/assessments/{tok}/answers", json=a).json()["ok"]
        assert c.post(f"/_site/molodist-360/api/assessments/{tok}/answers", json={"question_key": "x", "answer_value": "y"}).status_code == 400
        res = c.post(f"/_site/molodist-360/api/assessments/{tok}/submit", json={}).json()
        assert 0 <= res["overall_score"] <= 100 and res["modules"]
        assert c.get(f"/_site/molodist-360/api/results/{tok}").json()["overall_score"] == res["overall_score"]
        pdf = c.get(f"/_site/molodist-360/assets/pdfs/{tok}.pdf")
        assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
        assert "Олена" in c.get(f"/_site/molodist-360/assets/answers/{tok}.html").text
        assert c.get("/_site/molodist-360/api/results/nonexistenttoken123").status_code == 404
    with SessionLocal() as db:
        a = db.query(M360Assessment).filter_by(token=tok).one()
        p = db.get(Patient, a.patient_id)
        assert p and p.phone.endswith("671112299") and a.status == "completed"
