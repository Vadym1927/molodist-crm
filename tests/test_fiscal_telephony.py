import json

import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Appeal, Call, Patient, Sale, SaleItem, SalePayment
from app.services import fiscal as fis, telephony as tel


def _mock_checkbox(log):
    st = {"shift": None}

    def h(req: httpx.Request):
        p = req.url.path
        log.append((req.method, p, req.headers.get("X-License-Key")))
        if p.endswith("/cashier/signin"):
            return httpx.Response(200, json={"access_token": "T"})
        if p.endswith("/cashier/shift"):
            return httpx.Response(200, json=st["shift"])
        if p.endswith("/shifts") and req.method == "POST":
            st["shift"] = {"id": "sh1", "status": "OPENED"}
            return httpx.Response(200, json=st["shift"])
        if p.endswith("/receipts/sell"):
            body = json.loads(req.content)
            log.append(("body", body))
            return httpx.Response(200, json={"id": "rc-1", "status": "CREATED"})
        if p.endswith("/receipts/rc-1"):
            return httpx.Response(200, json={"id": "rc-1", "status": "DONE", "fiscal_code": "FX123"})
        return httpx.Response(404, json={"message": "nf"})
    return httpx.Client(transport=httpx.MockTransport(h))


def test_fiscalize_sale():
    with SessionLocal() as db:
        p = Patient(full_name="Фіскальна Пацієнтка", phone="+380671112233")
        db.add(p); db.flush()
        s = Sale(patient_id=p.id, subtotal=1000, discount=100, total=900, status="paid")
        s.items.append(SaleItem(kind="service", ref_id=5, name="Пілінг", qty=1, price=1000, discount=100, total=900))
        s.payments.append(SalePayment(method="Готівка", amount=700))
        s.payments.append(SalePayment(method="Бонуси", amount=200))
        db.add(s); db.commit()
        rid = fis.save_rro(db, None, {"name": "Каса Колган", "fop": "Колган", "login": "kas", "password": "pw", "license_key": "LK", "test": True})
        assert fis.cfg(db)["default_rro"] == rid and "LK" not in json.dumps(fis.rros(db))
        log = []
        info = fis.fiscalize(db, s, http=_mock_checkbox(log), wait=0)
        assert info["fiscal_code"] == "FX123" and s.fiscal["url"].endswith("rc-1")
        body = next(x[1] for x in log if x[0] == "body")
        assert body["payments"] == [{"type": "CASH", "value": 70000, "label": "Готівка"}]
        assert body["goods"][0]["good"]["price"] == 100000 and body["goods"][0]["quantity"] == 1000
        assert body["goods"][0]["discounts"][0]["value"] == 10000 and body["discounts"][0]["value"] == 20000
        assert any(x[1].endswith("/shifts") and x[2] == "LK" for x in log if len(x) == 3)
        try:
            fis.fiscalize(db, s, http=_mock_checkbox([]), wait=0)
            assert False
        except fis.FiscalError:
            pass
        # переказ на рахунок — не фіскалізується
        s2 = Sale(total=500, subtotal=500, status="paid")
        s2.items.append(SaleItem(kind="other", name="X", qty=1, price=500, total=500))
        s2.payments.append(SalePayment(method="ФОП Колган — рахунок", amount=500))
        db.add(s2); db.commit()
        try:
            fis.build(db, s2)
            assert False
        except fis.FiscalError as e:
            assert "не фіскалізується" in str(e)


def test_checkbox_and_telephony_pages_and_hook():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/settings/checkbox").text
        assert "Фіскальний чек" in t and "Відправлення чеків" in t and "Термінали" in t
        r = c.post("/settings/checkbox/receipt", data={"auto": "1", "mode_0": "cash", "mode_1": "card"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            cf = fis.cfg(db)
            assert cf["auto"] and cf["method_modes"]["Готівка"] == "cash"
            cf["auto"] = False
            fis.save_cfg(db, cf)
        hub = c.get("/settings").text
        assert 'href="/settings/telephony"' in hub and 'href="/settings/checkbox"' in hub
        c.post("/settings/telephony", data={"provider": "ringostat", "project_id": "P1", "auth_key": "SECRETKEY", "source": "Дзвінок",
                                            "create_appeal": "1"})
        page = c.get("/settings/telephony").text
        assert "SECRETKEY" not in page and "P1" in page
        with SessionLocal() as db:
            hook = tel.public(db)["hook"]
            db.add(Patient(full_name="Дзвінкова Пацієнтка", phone="+380 67 555 44 33")); db.commit()
        assert c.post("/api/public/telephony/ringostat/wrong", json={}).status_code == 404
        r = c.post(f"/api/public/telephony/ringostat/{hook}", json={"uniqueid": "u1", "caller": "380675554433", "call_type": "in",
                                                                    "disposition": "NO ANSWER", "duration": 0})
        assert r.status_code == 200 and r.json()["appeal"]
        r2 = c.post(f"/api/public/telephony/binotel/{hook}", data={"callDetails[generalCallID]": "b1", "callDetails[externalNumber]": "0675554433",
                                                                     "callDetails[callType]": "0", "callDetails[disposition]": "ANSWER",
                                                                     "callDetails[billsec]": "65"})
        assert r2.json()["appeal"] == r.json()["appeal"]        # той самий номер за добу → те саме звернення
        with SessionLocal() as db:
            calls = db.query(Call).all()
            assert len(calls) == 2 and all(x.patient_id for x in calls) and calls[1].duration == 65 and calls[1].status == "answered"
            a = db.get(Appeal, r.json()["appeal"])
            assert a.source == "Дзвінок" and a.patient_id
        assert "0675554433" in c.get("/settings/telephony").text
