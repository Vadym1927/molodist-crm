import json
import re
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app
from app.models import DkOrder, DkProfile, Doctor, M360Assessment, PayLink, Patient, SalaryAdj, SessionLocal, ShopCode
from app.services import dr_kolgan as dk


def _login(c, phone, monkeypatch):
    sent = {}
    import app.services.shop_cabinet as sc
    monkeypatch.setattr(sc, "_deliver", lambda db, ph, text, *a, **k: sent.setdefault("t", text) and "sms")
    r = c.post("/_site/dr-kolgan/api/auth/send", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = re.search(r"\d{6}", sent["t"]).group(0)
    r = c.post("/_site/dr-kolgan/api/auth/verify", json={"phone": phone, "code": code})
    assert r.status_code == 200 and "dk_s" in r.cookies
    c.cookies.set("dk_s", r.cookies["dk_s"])


def test_dk_cabinet_flow(monkeypatch):
    with TestClient(app, base_url="https://testserver") as c:
        assert c.get("/_site/dr-kolgan/api/auth/me").status_code == 401
        assert c.get("/_site/dr-kolgan/cabinet/", follow_redirects=False).status_code == 302
        assert c.get("/_site/dr-kolgan/").status_code == 200
        _login(c, "+380671119911", monkeypatch)
        assert c.post("/_site/dr-kolgan/api/auth/profile", json={"firstName": "Ірина"}).status_code == 400
        r = c.post("/_site/dr-kolgan/api/auth/profile", json={"firstName": "Ірина", "lastName": "Тест", "birthDate": "1990-05-01", "gender": "Жінка"})
        assert r.json()["profile"]["firstName"] == "Ірина"
        assert c.get("/_site/dr-kolgan/cabinet/", follow_redirects=False).status_code == 200
        assert c.post("/_site/dr-kolgan/api/auth/materials", json={"id": "mole-control"}).json()["ok"]
        assert c.post("/_site/dr-kolgan/api/auth/materials", json={"id": "full-checkup"}).status_code == 400    # платний — лише після оплати
        assert c.post("/_site/dr-kolgan/api/auth/tests/bmi", json={"heightCm": 170, "weightKg": 65}).json()["result"]["bmi"] == 22.5
        h = c.post("/_site/dr-kolgan/api/auth/tests/hads", json={"answers": [3] * 14}).json()["result"]
        assert "anxiety" in h
        assert len(c.get("/_site/dr-kolgan/api/auth/tests/hads").json()["results"]) == 1
        assert c.get("/_site/dr-kolgan/api/auth/protected/full-checkup/page/1.jpg").status_code == 403
    with SessionLocal() as db:
        p = db.query(Patient).filter(Patient.phone.like("%671119911")).one()
        assert p.source == "Сайт Дарʼї Колган" and db.query(DkProfile).filter_by(patient_id=p.id).one().gender == "Жінка"
        from app.models import ShopAccount, ShopSession
        assert not db.query(ShopAccount).filter_by(patient_id=p.id).count() and not db.query(ShopSession).filter_by(patient_id=p.id).count()


def test_dk_paid_guide_grants_access_and_salary():
    with SessionLocal() as db:
        d = db.query(Doctor).filter(Doctor.name.like("%Колган%")).first() or Doctor(name="Колган Дарʼя Олександрівна")
        db.add(d)
        p = Patient(full_name="Покупець Гайду", phone="+380671119922")
        db.add(p); db.flush()
        o = DkOrder(patient_id=p.id, product="full-checkup", amount=390)
        db.add(o); db.flush()
        pl = PayLink(provider="mono", amount=390, purpose="Гайд", patient_id=p.id, status="paid", paid_amount=390, info={"dk_order": o.id})
        db.add(pl); db.flush()
        dk.paid(db, pl); dk.paid(db, pl)
        db.commit()
        assert o.status == "paid" and dk.has_material(db, p.id, "full-checkup")
        adj = db.get(SalaryAdj, o.salary_adj_id)
        assert adj.amount == 390 and adj.doctor_id == d.id and adj.article == dk.SALARY_ARTICLE
        assert db.query(SalaryAdj).filter(SalaryAdj.comment.like(f"%№{o.id}")).count() == 1
        f = dk.SECURE / "full-checkup" / "pages" / "01.jpg"
        made = not f.exists()
        if made:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"\xff\xd8test")
        try:
            assert dk.guide_page(db, p.id, "full-checkup", 1) is not None
            assert dk.guide_page(db, p.id, "full-checkup", 99) is None
        finally:
            if made:
                f.unlink()


def test_dk_import_and_reports(tmp_path):
    data = {"m360": [{"id": "11111111-2222-3333-4444-555555555555", "status": "completed", "created_at": "2026-10-01T10:00:00",
                      "submitted_at": "2026-10-01T10:15:00", "overall_score": 64, "overall_status": "Needs attention", "top_risks": ["Сон"],
                      "patient": {"first_name": "Стара", "last_name": "Анкета", "phone": "+380671119933"}, "answers": [],
                      "modules": [{"module_key": "recovery", "module_name": "Відновлення", "module_score": 5.5}], "factors": []}],
            "profiles": [{"phone": "+380671119944", "firstName": "Старий", "lastName": "Кабінет", "birthDate": "1985-01-01", "gender": "Жінка",
                          "registeredAt": "2026-09-01T10:00:00Z", "materials": [{"id": "healthy-plate", "grantedAt": "2026-09-01T10:05:00Z"}],
                          "bmi": [{"calculatedAt": "2026-09-02T10:00:00Z", "bmi": 23.1, "category": "Нормальна маса тіла"}], "hads": []}],
            "orders": [{"reference": "DKCHECKUP-1", "phone": "+380671119944", "productId": "full-checkup", "amount": 39000, "status": "Approved",
                        "createdAt": "2026-09-03T10:00:00Z"}]}
    with SessionLocal() as db:
        n = dk.import_old(db, data)
        assert n["m360"] == 1 and n["profiles"] == 1 and n["orders"] == 1
        assert dk.import_old(db, data)["m360"] == 0                     # повторно — без дублів
        a = db.query(M360Assessment).filter_by(token="11111111-2222-3333-4444-555555555555").one()
        p = db.get(Patient, a.patient_id)
        db.expunge(a)
        assert "Тест 360" in (p.client_groups or [])
    from app.config import settings
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r1 = c.get("/reports/dr-kolgan?df=2025-01-01&dt_=2026-12-31")
        assert r1.status_code == 200 and "Сайт Дарʼї Колган" in r1.text and "Старий" in r1.text and "Анкета" not in r1.text   # 360 — окремо
        r2 = c.get("/reports/molodist-360?df=2025-01-01&dt_=2026-12-31")
        assert r2.status_code == 200 and "Анкета" in r2.text
        pr = c.get(f"/patients/{a.patient_id}")
        assert pr.status_code == 200 and "Молодість 360" in pr.text


def test_qr_redirect_counts():
    with TestClient(app) as c:
        r = c.get("/_site/q/home?utm_content=box", follow_redirects=False)
        assert r.status_code == 302 and "molodist.store" in r.headers["location"] and "utm_source=qr" in r.headers["location"]
        r = c.get("/_shop/q/cbox", follow_redirects=False)
        assert r.status_code == 302 and "cbox.mobi" in r.headers["location"]
        assert c.get("/_site/q/nope", follow_redirects=False).status_code == 404


def test_dk_post_to_page_redirects():
    with TestClient(app) as c:
        r = c.post("/_site/dr-kolgan/guides/full-checkup/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/dr-kolgan/guides/full-checkup/"


def test_dk_revoke():
    with SessionLocal() as db:
        p = Patient(full_name="Забрати Доступ", phone="+380671119955")
        db.add(p); db.flush()
        dk.grant(db, p.id, "mole-control")
        assert dk.revoke(db, p.id, "mole-control") == 1 and not dk.has_material(db, p.id, "mole-control")


def test_dk_refund_removes_salary():
    with SessionLocal() as db:
        d = db.query(Doctor).filter(Doctor.name.like("%Колган%")).first() or Doctor(name="Колган Дарʼя")
        db.add(d)
        p = Patient(full_name="Повернення Гайду", phone="+380671119966")
        db.add(p); db.flush()
        o = DkOrder(patient_id=p.id, product="full-checkup", amount=390)
        db.add(o); db.flush()
        pl = PayLink(provider="mono", amount=390, purpose="Гайд", patient_id=p.id, status="paid", paid_amount=390, info={"dk_order": o.id})
        db.add(pl); db.flush()
        dk.paid(db, pl); db.commit()
        aid = o.salary_adj_id
        assert dk.refund(db, o.id) and o.status == "refunded" and db.get(SalaryAdj, aid) is None
        assert not dk.refund(db, o.id)


def test_dk_login_not_in_shop_customers():
    from app.models import ShopAccount
    from app.services import shop_customers as sc
    with SessionLocal() as db:
        p = Patient(full_name="Лише Дарʼя", phone="+380671119977")
        db.add(p); db.flush()
        db.add(ShopAccount(patient_id=p.id, created_at=datetime.utcnow()))
        db.add(DkProfile(patient_id=p.id, first_name="Лише"))
        db.commit()
        assert not any(x["patient"] and x["patient"].id == p.id for x in sc.rows(db)["rows"])


def test_dk_detach_shop():
    from app.models import ShopAccount, ShopSession
    with SessionLocal() as db:
        p = Patient(full_name="Відʼєднати", phone="+380671119988")
        db.add(p); db.flush()
        db.add(ShopAccount(patient_id=p.id, created_at=datetime.utcnow()))
        db.add(ShopSession(token_hash="x" * 64, patient_id=p.id, created_at=datetime.utcnow(), expires_at=datetime.utcnow()))
        db.add(DkProfile(patient_id=p.id))
        db.commit()
        assert dk.detach_shop(db) >= 1
        assert not db.query(ShopAccount).filter_by(patient_id=p.id).count() and not db.query(ShopSession).filter_by(patient_id=p.id).count()
