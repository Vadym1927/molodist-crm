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


def test_payreq_via_agent(tmp_path, monkeypatch):
    """Розпізнавання через агента на VPS (як накладні): черга kind=payreq → результат → картка в Telegram оновлена."""
    from app.security import new_api_key
    from app.models import ApiKey, InvoiceJob
    from app.services import invoice_ai, alerts
    settings.admin_password = "owner-pass-1"
    old_dir, settings.files_dir = settings.files_dir, str(tmp_path)
    sent = []
    monkeypatch.setattr(alerts, "_api", lambda m, **kw: sent.append((m, kw.get("data"))) or True)
    with SessionLocal() as db:
        _mono(db)
        invoice_ai.set_engine(db, "agent")
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="openclaw-pay", prefix=prefix, key_hash=hsh, scopes=["invoices"])); db.commit()
        pr = prq.create(db, b"%PDF-1.4 x", "rah.pdf", "application/pdf", "telegram", "-100", 7, "Адмін")
        assert pr.status == "recognizing" and "розпізнає" in prq.card_text(db, pr)
        pr.tg_card = 55; db.commit()
        pid = pr.id
        j = db.query(InvoiceJob).filter(InvoiceJob.ref_id == pid).one()
        assert j.kind == "payreq"
        jid = j.id
    try:
        with TestClient(app) as c:
            h = {"X-API-Key": raw}
            p = c.get("/api/v1/invoices/pending", headers=h).json()
            job = next(x for x in p["invoices"] if x["id"] == jid)
            assert job["kind"] == "payreq" and "рахунок на оплату" in job["prompt"].lower()
            assert c.get(f"/api/v1/invoices/{jid}/file", headers=h).status_code == 200
            r = c.post(f"/api/v1/invoices/{jid}/result", headers=h, json={"result": _claude("", b"", "", "рахунок на оплату")})
            assert r.status_code == 200 and r.json()["pay_request"] == pid
        with SessionLocal() as db:
            pr = db.get(PayRequest, pid)
            assert pr.status == "new" and pr.amount == 18854.64 and pr.fop == "Колган" and pr.receiver_iban == IBAN
        m, d = sent[-1]
        assert m == "editMessageText" and d["message_id"] == "55" and "pay:ok" in d.get("reply_markup", "")
        # помилка агента
        with SessionLocal() as db:
            pr2 = prq.create(db, b"\xff\xd8 x", "r.jpg", "image/jpeg")
            j2 = db.query(InvoiceJob).filter(InvoiceJob.ref_id == pr2.id).one().id
            p2 = pr2.id
        with TestClient(app) as c:
            c.post(f"/api/v1/invoices/{j2}/result", headers={"X-API-Key": raw}, json={"error": "розмите фото"})
        with SessionLocal() as db:
            pr2 = db.get(PayRequest, p2)
            assert pr2.status == "error" and "розмите" in pr2.error
    finally:
        settings.files_dir = old_dir
        with SessionLocal() as db:
            invoice_ai.set_engine(db, "crm")


def test_receipt_links_to_invoice(tmp_path, monkeypatch):
    """Квитанція в групі → розпізнано агентом → привʼязано до рахунку з тим самим отримувачем і сумою → «✅ Оплачено» у чаті."""
    from app.security import new_api_key
    from app.models import ApiKey, InvoiceJob
    from app.services import invoice_ai, alerts
    settings.admin_password = "owner-pass-1"
    old_dir, settings.files_dir = settings.files_dir, str(tmp_path)
    sent = []
    monkeypatch.setattr(alerts, "_api", lambda m, **kw: sent.append((m, kw.get("data"))) or True)
    with SessionLocal() as db:
        _mono(db)
        inv = prq.create(db, b"%PDF", "rah.pdf", "application/pdf", "telegram", "-100", 9, "Адмін", claude=_claude)
        inv.tg_card = 77; db.commit()
        assert inv.status == "new" and inv.kind is None
        invoice_ai.set_engine(db, "agent")
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="oc-rc", prefix=prefix, key_hash=hsh, scopes=["invoices"])); db.commit()
        assert prq.is_receipt("Квитанція_Вихідний_платіж_4MXP.pdf") and not prq.is_receipt("rahunok 87224.pdf")
        rc = prq.create(db, b"%PDF r", "Квитанція_Вихідний_платіж_4MXP.pdf", "application/pdf", "telegram", "-100", 10, "Вадим")
        rc.tg_card = 78; db.commit()
        assert rc.kind == "receipt" and rc.status == "recognizing"
        j = db.query(InvoiceJob).filter(InvoiceJob.ref_id == rc.id).one()
        assert j.kind == "receipt"
        jid, inv_id, rc_id = j.id, inv.id, rc.id
    try:
        with TestClient(app) as c:
            h = {"X-API-Key": raw}
            job = next(x for x in c.get("/api/v1/invoices/pending", headers=h).json()["invoices"] if x["id"] == jid)
            assert "квитанція" in job["prompt"].lower()
            c.post(f"/api/v1/invoices/{jid}/result", headers=h, json={"result": _claude("", b"", "", "рахунок на оплату")})
        with SessionLocal() as db:
            inv, rc = db.get(PayRequest, inv_id), db.get(PayRequest, rc_id)
            assert rc.status == "receipt" and rc.link_id == inv_id and inv.status == "paid" and inv.link_id == rc_id
            assert "Привʼязано до рахунку #" in prq.card_text(db, rc)
        msgs = [d for m, d in sent if m == "sendMessage"]
        assert msgs and "Оплачено" in msgs[-1]["text"] and msgs[-1]["reply_to_message_id"] == "77"
    finally:
        settings.files_dir = old_dir
        with SessionLocal() as db:
            invoice_ai.set_engine(db, "crm")
