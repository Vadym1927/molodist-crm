"""Рахунки на оплату: розпізнавання (мок Claude) → ФОП за платником → погодження → чернетка в monobank (мок Corporate API) → стан."""
import json
from datetime import date

import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, PayRequest, Supplier, SupplierEntity
from app.integrations import monobank
from app.services import pay_requests as prq

IBAN = "UA" + "3" * 27


def _claude(key, data, mime, prompt):
    assert "рахунок на оплату" in prompt.lower()
    return {"seller": {"name": "ТОВ «Тест Постач»", "edrpou": "12345678", "iban": "UA 33 " + "3" * 25},
            "payer": {"name": "ФОП Колган Дар'я Олександрівна", "edrpou": "3474905024"}, "number": "87224", "date": "2026-11-03",
            "total": "18 854,64", "vat": "0", "purpose": ""}


def _mono(db):
    monobank._save(db, [{"fop": "Бондар", "token": "tB", "accounts": [{"id": "a1", "iban": "UA" + "1" * 27, "type": "fop", "currency": 980}]},
                        {"fop": "Колган", "token": "tK", "accounts": [{"id": "a2", "iban": "UA" + "2" * 27, "type": "fop", "currency": 980}]}])


def test_payreq_flow():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        _mono(db)
        s = Supplier(name="Тест Постач"); db.add(s); db.commit()
        db.add(SupplierEntity(supplier_id=s.id, legal_name="ТОВ «Тест Постач»", edrpou="12345678", iban=IBAN)); db.commit()
        pr = prq.create(db, b"%PDF-1.4 test", "rah.pdf", "application/pdf", "telegram", "-100", 5, "Адмін", claude=_claude)
        assert pr.status == "new" and pr.receiver_iban == IBAN and pr.amount == 18854.64 and pr.fop == "Колган"
        assert pr.sender_iban == "UA" + "2" * 27 and pr.supplier_id == s.id and "№ 87224 від 03.11.2026" in pr.purpose and not prq.problems(pr)
        assert "Колган" in prq.card_text(db, pr)
        seen = {}

        def handler(req: httpx.Request):
            if req.url.path.endswith("/payment/prepare"):
                seen["body"] = json.loads(req.content)
                seen["tok"] = req.headers.get("x-token")
                return httpx.Response(200, json={"id": "P-1", "state": "DRAFT"})
            if req.url.path.endswith("/payment/state"):
                return httpx.Response(200, json={"state": "IN_STATEMENT"})
            return httpx.Response(404)
        cl = httpx.Client(transport=httpx.MockTransport(handler))
        prq.approve(db, pr, None, http=cl)
        assert pr.status == "draft" and pr.corp_id == "P-1" and seen["tok"] == "tK"
        b = seen["body"]
        assert b["amount"] == 1885464 and b["receiver"]["iban"] == IBAN and b["senderIban"] == "UA" + "2" * 27 and b["externalReference"] == f"crm-pay-{pr.id}"
        assert prq.refresh(db, pr, http=cl) == "paid" and pr.status == "paid"
        # помилка доступу
        pr2 = prq.create(db, b"%PDF", "r2.pdf", "application/pdf", claude=_claude)
        bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"errorDescription": "forbidden"})))
        prq.approve(db, pr2, None, http=bad)
        assert pr2.status == "error" and "Corporate API" in pr2.error
        # не розпізнано / без ключа — запис є, статус error
        pr3 = prq.create(db, b"x", "r3.pdf", "application/pdf")
        assert pr3.status == "error" and "Claude" in pr3.error
        pid = pr2.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Рахунки на оплату" in c.get("/payreq").text and "/rahunky" in c.get("/payreq").text
        v = c.get(f"/payreq/{pid}").text
        assert "Реквізити платежу" in v and IBAN in v
        r = c.post(f"/payreq/{pid}", data={"action": "save", "receiver_name": "ТОВ «Тест Постач»", "receiver_iban": IBAN, "receiver_edrpou": "12345678",
                                           "amount": "100.50", "invoice_no": "1", "invoice_date": "2026-11-05", "fop": "Бондар", "purpose": "Оплата за рахунком № 1"},
                   follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            x = db.get(PayRequest, pid)
            assert x.amount == 100.5 and x.fop == "Бондар" and x.sender_iban == "UA" + "1" * 27 and x.status == "new"
        assert c.post("/payreq/settings", data={"chat": "-1001"}, follow_redirects=False).status_code == 303
        with SessionLocal() as db:
            assert prq.chat_id(db) == "-1001"


def test_corp_probe():
    with SessionLocal() as db:
        _mono(db)
        ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"errorDescription": "not found"})))
        res = monobank.corp_probe(db, "Бондар", http=ok)
        assert res["ok"] and res["token"] == "токен виписки"
        no = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
        res2 = monobank.corp_probe(db, "Колган", token="corpK", http=no)
        assert not res2["ok"] and "не підходить" in res2["msg"]
        assert not next(x for x in monobank.saved(db) if x["fop"] == "Колган").get("corp_token")      # невдалий — не зберігаємо
        assert next(x for x in monobank.public(db) if x["fop"] == "Бондар")["corp"]["ok"]
