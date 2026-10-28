"""Сертифікати: імпорт CleverBox, стан, задача до згорання, картка пацієнта, звіт."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Patient, GiftCertificate, Task
from app.services import certificates as cs
from app.services.tasks import generate_auto

HEAD = ["ID / код", "Code", "Тип gift card", "Client ID", "Клієнт", "Seller", "дата продажу", "філія", "Ціна", "знижка",
        "до сплати", "Номінал", "Баланс", "Дійсний до", "Коментар", "Статус"]


def test_certificates_flow():
    settings.admin_password = "owner-pass-1"
    soon = (date.today() + timedelta(days=5)).strftime("%d.%m.%Y")
    with SessionLocal() as db:
        p = Patient(full_name="Сертифікатна Олена", cleverbox_id=8800555); db.add(p); db.commit()
        pid = p.id
        rows = [["9001", "6607", "Cертифікат 2000", "8800555", "Сертифікатна Олена", "Кучеренко Яна",
                 datetime.now().strftime("%d.%m.%y %H:%M"), "Molodist_", "2000", "0", "2000", "2000", "1500.00", soon, "Подарунок", "активований"],
                ["9002", "1111", "Cертифікат 3000", "", "Without client", "Рокун Вікторія", "05.09.25 18:22", "", "3000", "0",
                 "3000", "3000", "0.00", "04.11.2025", "", "протермінований"]]
        assert cs.is_certificates_export(HEAD)
        rep = cs.import_certificates(db, rows, HEAD)
        assert rep["rows"] == 2
        c = db.get(GiftCertificate, 9001)
        assert c.patient_id == pid and c.state() == "expiring" and db.get(GiftCertificate, 9002).state() == "used"
        assert db.get(GiftCertificate, 9002).client_name is None
        generate_auto(db)
        t = db.query(Task).filter(Task.kind == "certificate", Task.patient_id == pid).one()
        assert "6607" in t.title and t.priority == "normal"
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Подарункові сертифікати" in cl.get(f"/patients/{pid}").text
        page = cl.get("/reports?r=certificates&period=month").text
        assert "Скоро згорить" in page and "Кучеренко Яна" in page
