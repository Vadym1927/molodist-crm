import base64
import json

import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, FinOp, Patient, PayLink
from app.services import paylinks as pls


def _mono():
    st = {"status": "created"}

    def h(req: httpx.Request):
        assert req.headers.get("X-Token") == "MTOK"
        if req.url.path.endswith("/invoice/create"):
            b = json.loads(req.content)
            assert b["amount"] == 150050 and b["ccy"] == 980 and "/api/public/pay/mono/" in b["webHookUrl"]
            return httpx.Response(200, json={"invoiceId": "inv1", "pageUrl": "https://pay.mbnk.biz/inv1"})
        if req.url.path.endswith("/invoice/status"):
            return httpx.Response(200, json={"invoiceId": "inv1", "status": st["status"], "finalAmount": 150050})
        return httpx.Response(404, json={})
    return httpx.Client(transport=httpx.MockTransport(h)), st


def test_mono_link_and_webhook_books_income():
    with SessionLocal() as db:
        p = Patient(full_name="Оплатна Пацієнтка", phone="+380671119988")
        db.add(p); db.commit()
        pls.save(db, "mono", {"token": "MTOK"}, 24, "", "Онлайн-оплати")
        assert pls.public(db)["ready"] and "MTOK" not in json.dumps(pls.public(db))
        http, st = _mono()
        pl = pls.create(db, 1500.5, "Передоплата", "https://crm.molodist.com.ua", p.id, http=http)
        assert pl.url == "https://pay.mbnk.biz/inv1" and pl.status == "created"
        assert "1500.50" in pls.message(db, pl) and pl.url in pls.message(db, pl)
        pls.webhook(db, "mono", json.dumps({"invoiceId": "inv1", "status": "success"}).encode(), {}, http)   # тілу не віримо
        assert pl.status == "created"
        st["status"] = "success"
        pls.webhook(db, "mono", json.dumps({"invoiceId": "inv1"}).encode(), {}, http)
        assert pl.status == "paid" and pl.paid_amount == 1500.5
        ops = db.query(FinOp).filter(FinOp.category == "Онлайн-оплата").all()
        assert len(ops) == 1 and ops[0].amount == 1500.5 and ops[0].counterparty == "Оплатна Пацієнтка"
        pls.check_status(db, pl, http)                       # повторно — без дубля
        assert db.query(FinOp).filter(FinOp.category == "Онлайн-оплата").count() == 1


def test_liqpay_link_and_signed_callback():
    with SessionLocal() as db:
        pls.save(db, "liqpay", {"public_key": "pub1", "private_key": "priv1"}, 24, "", "")
        pl = pls.create(db, 200, "Консультація", "https://crm.molodist.com.ua")
        assert pl.url.startswith("https://www.liqpay.ua/api/3/checkout?data=")
        data = base64.b64encode(json.dumps({"order_id": pl.ext_id, "status": "success", "amount": 200}).encode()).decode()
        bad = pls.webhook(db, "liqpay", b"", {"data": data, "signature": "x"})
        assert not bad["ok"] and pl.status == "created"
        pls.webhook(db, "liqpay", b"", {"data": data, "signature": pls._liqpay_sig("priv1", data)})
        assert pl.status == "paid"


def test_pages_and_hub():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        hub = c.get("/settings").text
        assert 'href="/settings/payments"' in hub and "Бот пацієнтів і чат" not in hub and "/settings/keys?s=mono#mono" in hub
        assert "Ключі й інтеграції" not in hub
        t = c.get("/settings/payments").text
        assert "monobank еквайринг" in t and "LiqPay" in t and "WayForPay" in t
        c.post("/settings/payments/account", data={"provider": "mono", "name": "monobank ФОП Колган", "token": "T1", "cashbox": "Онлайн-оплати", "active": "1"})
        c.post("/settings/payments/account", data={"provider": "mono", "name": "monobank ФОП Бондар", "token": "T2", "cashbox": "Онлайн-оплати", "active": "1"})
        t = c.get("/settings/payments").text
        assert "monobank ФОП Колган" in t and "monobank ФОП Бондар" in t and "T1" not in t
        mono = c.get("/settings/keys?s=mono").text
        assert "Банк monobank" in mono and "Claude API" not in mono
        with SessionLocal() as db:
            p = Patient(full_name="Картка Оплати", phone="+380671110000"); db.add(p); db.commit(); pid = p.id
        pg = c.get(f"/patients/{pid}").text
        assert "Посилання на оплату" in pg and "monobank ФОП Бондар" in pg        # вибір ФОП у формі
        hook = None
        with SessionLocal() as db:
            hook = pls.public(db)["hook"]
        assert c.post("/api/public/pay/mono/bad", json={}).status_code == 404
        assert c.post(f"/api/public/pay/mono/{hook}", json={"invoiceId": "zzz"}).status_code == 200


def test_two_accounts_parallel():
    with SessionLocal() as db:
        a1 = pls.save_account(db, None, "mono", "monobank ФОП Колган", {"token": "MTOK"}, "Онлайн-оплати", True)
        a2 = pls.save_account(db, None, "mono", "monobank ФОП Бондар", {"token": "OTHER"}, "Готівка", True)
        http, st = _mono()
        pl = pls.create(db, 1500.5, "Передоплата", "https://crm.molodist.com.ua", http=http, account_id=a1["id"])
        assert pl.account_id == a1["id"]
        st["status"] = "success"
        pls.check_status(db, pl, http)                      # токен саме цього ФОП (MTOK), інакше мок впаде
        assert pl.status == "paid"
        assert pls.account(pls._raw(db), a2["id"])["cashbox"] == "Готівка"
