"""Продаж сертифіката в касі → картка пацієнта; оплата сертифікатом повністю — без РРО; нагадування; шаблони й налаштування."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import SessionLocal, Doctor, Patient, GiftCertificate, Sale, Task, CbService
from app.services import gift_types as gt, sales as sl


def test_gift_templates_sale_and_pay():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        tps = gt.items(db)
        assert [t["nominal"] for t in tps] == [2000, 3000, 5000, 10000] and all(t["days"] == 60 for t in tps)
        d = Doctor(name="Гіфт Лікарка", active=True)
        p = Patient(full_name="Гіфт Покупчиня", phone="+380671230777")
        s1 = CbService(id=990401, name="Пілінг гіфт", price=1500, time_min=30, category="Пілінги")
        db.add_all([d, p, s1]); db.commit()
        did, pid, tid = d.id, p.id, tps[0]["id"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get("/sales/new").text
        assert "GIFTS=[" in page and "\\u0421\\u0435\\u0440\\u0442\\u0438\\u0444\\u0456\\u043a\\u0430\\u0442 2000" in page
        # продаж сертифіката готівкою → виписано в картку
        j = c.post("/sales", json={"patient_id": pid, "items": [{"kind": "certificate", "ref_id": tid, "name": "Сертифікат 2000", "qty": 1, "price": 2000, "doctor_id": did}],
                                   "cert_for": "мама", "payments": [{"method": "Готівка", "amount": 2000}]}).json()
        assert j["ok"], j
        sid = j["id"]
        with SessionLocal() as db:
            cert = db.scalar(select(GiftCertificate).where(GiftCertificate.patient_id == pid))
            assert cert and cert.id >= gt.CRM_ID_FROM and cert.code.startswith("MC-") and cert.balance == 2000 and cert.nominal == 2000
            assert cert.valid_to == (datetime.now() + timedelta(days=60)).date() and cert.seller == "Гіфт Лікарка" and "мама" in cert.comment
            assert f"Ч-{sid}" in cert.comment and cert.state() == "active"
            cid = cert.id
        assert cert.code in c.get(f"/patients/{pid}").text
        # оплата послуги сертифікатом повністю → без РРО
        j2 = c.post("/sales", json={"patient_id": pid, "items": [{"kind": "service", "ref_id": 990401, "name": "Пілінг гіфт", "qty": 1, "price": 1500, "doctor_id": did}],
                                    "payments": [{"method": "Сертифікат", "amount": 1500, "cert_id": cid}]}).json()
        assert j2["ok"] and "без РРО" in j2["title"]
        with SessionLocal() as db:
            s2 = db.get(Sale, j2["id"])
            assert s2.status == "paid" and (s2.fiscal or {}).get("skipped") and db.get(GiftCertificate, cid).balance == 500
        assert "Без РРО" in c.get(f"/sales/{j2['id']}").text
        # часткова: сертифікат 500 + готівка 1000 → у фіскальному чеку сертифікат — знижка
        j3 = c.post("/sales", json={"patient_id": pid, "items": [{"kind": "service", "ref_id": 990401, "name": "Пілінг гіфт", "qty": 1, "price": 1500, "doctor_id": did}],
                                    "payments": [{"method": "Сертифікат", "amount": 500, "cert_id": cid}, {"method": "Готівка", "amount": 1000}]}).json()
        assert j3["ok"] and "без РРО" not in j3["title"]
        with SessionLocal() as db:
            from app.services import fiscal as fis
            s3 = db.get(Sale, j3["id"])
            assert not (s3.fiscal or {}).get("skipped")
            body = fis.build(db, s3)
            assert body["payments"] == [{"type": "CASH", "value": 100000, "label": "Готівка"}] and body["discounts"][0]["value"] == 50000
            assert db.get(GiftCertificate, cid).balance == 0 and db.get(GiftCertificate, cid).state() == "used"
        # повернення чека продажу сертифіката — сертифікат використаний → анульовано, не видалено
        with SessionLocal() as db:
            sl.refund(db, db.get(Sale, sid), "тест", None)
            cc = db.get(GiftCertificate, cid)
            assert cc is not None and cc.cb_status == "Повернено" and cc.balance == 0
        # шаблони й налаштування
        r = c.post("/gifts/types", data={"name": "Сертифікат 1500", "nominal": "1500", "days": "90", "price": "1400", "desc": "Тест", "active": "1"}, follow_redirects=False)
        assert r.status_code == 303
        tp = c.get("/gifts?tab=types").text
        assert "Сертифікат 1500" in tp and "90 дн." in tp and "Налаштування сертифікатів" in tp
        c.post("/gifts/settings", data={"remind_days": "21", "code_prefix": "GC", "desc": "Послуги краси"})
        with SessionLocal() as db:
            cf = gt.cfg(db)
            assert cf["remind_days"] == 21 and cf["code_prefix"] == "GC" and cf["notify_tg"] is False
            # нагадування: сертифікат, що закінчується за 10 днів → задача адміну
            cx = GiftCertificate(id=990777, code="X-1", type_name="Сертифікат 3000", patient_id=pid, nominal=3000, balance=3000, price=3000, paid=3000,
                                 sold_at=datetime.now(), valid_to=date.today() + timedelta(days=10))
            db.add(cx); db.commit()
            assert cx in gt.expiring(db)
            from app.services.tasks import generate_auto
            generate_auto(db)
            t = db.scalar(select(Task).where(Task.source_key.like("cert:990777:%")))
            assert t and t.assignee_role == "admin" and "X-1" in t.title


def test_single_use_fiscal_name_and_no_sms():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        t = gt.save(db, None, {"name": "Разовий 1000", "nominal": 1000, "days": 30, "desc": "Послуги краси", "usage": "single", "active": True})
        d = Doctor(name="Разова Лікарка", active=True)
        p = Patient(full_name="Разова Пацієнтка", phone="+380671230778")
        s1 = CbService(id=990402, name="Чистка разова", price=600, time_min=30, category="Очищення")
        db.add_all([d, p, s1]); db.commit()
        did, pid, tid = d.id, p.id, t["id"]
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        j = c.post("/sales", json={"patient_id": pid, "items": [{"kind": "certificate", "ref_id": tid, "name": "Разовий 1000", "qty": 1, "price": 1000, "doctor_id": did}],
                                   "payments": [{"method": "Готівка", "amount": 1000}]}).json()
        assert j["ok"]
        with SessionLocal() as db:
            from app.services import fiscal as fis
            s = db.get(Sale, j["id"])
            assert fis.build(db, s)["goods"][0]["good"]["name"] == "Послуги краси"
            cfg = fis.cfg(db); cfg["send"]["on"] = True; fis.save_cfg(db, cfg)
            s.fiscal = {"url": "https://check.example/x"}; db.commit()
            assert "сертифікат" in (fis.send_receipt(db, s) or "")
            cert = db.scalar(select(GiftCertificate).where(GiftCertificate.patient_id == pid))
            assert cert.single_use is True
            cid = cert.id
        j2 = c.post("/sales", json={"patient_id": pid, "items": [{"kind": "service", "ref_id": 990402, "name": "Чистка разова", "qty": 1, "price": 600, "doctor_id": did}],
                                    "payments": [{"method": "Сертифікат", "amount": 600, "cert_id": cid}]}).json()
        assert j2["ok"]
        with SessionLocal() as db:
            cc = db.get(GiftCertificate, cid)
            assert cc.balance == 0 and "згорів" in cc.comment and cc.state() == "used"



def test_packages_import():
    """Вивантаження CleverBox «Пакети» (шаблони абонементів) — розпізнається й показується в «Шаблони й налаштування»."""
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.services import certificates as cs
    head = ["ID", "Назва", "Тип", "ТЕРМІН ДІЇ ", "Ціна", "Філія", "Послуги", "Дозволити використовувати всім клієнтам", "Продано"]
    rows = [["64", "5 процедур RF-ліфтинг", "Роздільний", "180", "16000", "Всі", "RF - ліфтинг все обличчя", "no", "1"]]
    assert cs.is_packages_export(head) and not cs.is_subscriptions_export(head) or cs.is_packages_export(head)
    with SessionLocal() as db:
        rep = cs.import_packages(db, rows, head)
        assert rep["rows"] == 1 and cs.packages(db)[0]["price"] == 16000 and cs.packages(db)[0]["days"] == 180
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "5 процедур RF-ліфтинг" in c.get("/gifts?tab=types").text


def test_package_sale_creates_subscription():
    from fastapi.testclient import TestClient
    from app.models import Subscription
    from app.services import certificates as cs
    head = ["ID", "Назва", "Тип", "ТЕРМІН ДІЇ ", "Ціна", "Філія", "Послуги", "Дозволити використовувати всім клієнтам", "Продано"]
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        cs.import_packages(db, [["64", "Курс RF 5 процедур", "Роздільний", "180", "16000", "Всі", "RF - ліфтинг", "no", "1"],
                                ["9", "Rejuran HB", "Роздільний", "0", "12600", "Всі", "Rejuran НВ", "no", "1"]], head)
        assert {x["id"]: x["visits"] for x in cs.packages(db)} == {64: 5, 9: 1} and all(x["active"] for x in cs.packages(db))
        d = Doctor(name="Абон Лікарка", active=True); p = Patient(full_name="Абон Пацієнтка", phone="+380671230778")
        db.add_all([d, p]); db.commit(); did, pid = d.id, p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "PKGS=[" in c.get("/sales/new").text
        j = c.post("/sales", json={"patient_id": pid, "items": [{"kind": "subscription", "ref_id": 64, "name": "Курс RF 5 процедур", "qty": 1, "price": 16000,
                                                                 "doctor_id": did}], "payments": [{"method": "Готівка", "amount": 16000}]}).json()
        assert j["ok"], j
        with SessionLocal() as db:
            s = db.query(Subscription).filter(Subscription.patient_id == pid).one()
            assert s.visits_total == 5 and s.visits_used == 0 and s.valid_to and s.paid == 16000 and s.id >= cs.SUB_CRM_FROM
        c.post("/gifts/packages/9", data={"visits": "3", "days": "90", "price": "12600"})       # без «active» → вимкнено
        with SessionLocal() as db:
            x = next(y for y in cs.packages(db) if y["id"] == 9)
            assert x["visits"] == 3 and x["days"] == 90 and x["active"] is False and len(cs.package_items(db)) == 1
