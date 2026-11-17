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


def test_sale_paid_by_link():
    from app.models import Sale, SaleItem, SalePayment, FinOp
    from app.services import sales as sl, cashboxes as cbx
    with SessionLocal() as db:
        cbx.apply_seed(db)
        a = pls.save_account(db, None, "mono", "monobank ФОП Колган", {"token": "MTOK"}, "Термінал ФОП Колган", True)
        m = sl.paylink_method("monobank ФОП Колган")
        assert m in sl.methods(db) and "Готівка ФОП Колган" in sl.methods(db) and sl.method_kind(m) == "link"
        p = Patient(full_name="Лінк Пацієнтка", phone="+380671234500"); db.add(p); db.commit()
        s = sl.create(db, {"patient_id": p.id, "items": [{"kind": "other", "name": "X", "qty": 1, "price": 1500.5}],
                           "payments": [{"method": m, "amount": 1500.5}]})
        assert s.status == "awaiting"
        http, st = _mono()
        pl = pls.create(db, 1500.5, f"Чек Ч-{s.id}", "https://crm.molodist.com.ua", p.id, http=http, account_id=a["id"])
        pl.sale_id = s.id; db.commit()
        box = next(x for x in cbx.items(db) if x["name"] == "Термінал ФОП Колган")
        before = cbx.balance(db, box)
        st["status"] = "success"
        pls.check_status(db, pl, http)
        db.refresh(s)
        assert s.status == "paid" and db.query(FinOp).filter(FinOp.category == "Онлайн-оплата").count() == 0
        assert round(cbx.balance(db, box) - before, 2) == 1500.5          # гроші — у касі цього ФОП
        try:
            sl.create(db, {"items": [{"kind": "other", "name": "Y", "qty": 1, "price": 10}], "payments": [{"method": m, "amount": 10}]})
            assert False
        except sl.SaleError:
            pass


def test_sale_link_flow_basket_and_status():
    from app.models import Sale
    from app.services import sales as sl
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        pls.save_account(db, None, "mono", "monobank ФОП Бондар", {"token": "MTOK"}, "Онлайн-оплати", True)
        p = Patient(full_name="Лінк Потік", phone="+380671234511"); db.add(p); db.commit()
        s = sl.create(db, {"patient_id": p.id, "items": [{"kind": "other", "name": "X", "qty": 1, "price": 1500.5}],
                           "payments": [{"method": sl.paylink_method("monobank ФОП Бондар"), "amount": 1500.5}]})
        sid, pid = s.id, p.id
    seen = {}

    def h(req):
        if req.url.path.endswith("/invoice/create"):
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"invoiceId": "inv9", "pageUrl": "https://pay.mbnk.biz/inv9"})
        return httpx.Response(200, json={"status": "success", "finalAmount": 150050})
    import app.services.paylinks as P
    orig = P.httpx.Client
    P.httpx.Client = lambda *a, **k: orig(transport=httpx.MockTransport(h))
    try:
        with TestClient(app) as c:
            c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
            r = c.post(f"/sales/{sid}/paylink", follow_redirects=False)
            assert r.status_code == 303
            b = seen["body"]["merchantPaymInfo"]["basketOrder"]
            assert sum(x["sum"] * x["qty"] for x in b) == 150050
            assert "pay.mbnk.biz/inv9" in c.get(f"/sales/{sid}").text
            j = c.get(f"/sales/{sid}/pay-status").json()
            assert j["sale"] == "paid" and j["links"][0]["status"] == "paid"
    finally:
        P.httpx.Client = orig


def test_mono_fiscal_check_attached_to_sale():
    from app.models import Sale
    from app.services import sales as sl
    with SessionLocal() as db:
        a = pls.save_account(db, None, "mono", "monobank ФОП Колган", {"token": "MTOK"}, "Онлайн-оплати", True)
        p = Patient(full_name="Фіск Моно", phone="+380671234522"); db.add(p); db.commit()
        s = sl.create(db, {"patient_id": p.id, "items": [{"kind": "other", "name": "X", "qty": 1, "price": 10}],
                           "payments": [{"method": sl.paylink_method("monobank ФОП Колган"), "amount": 10}]})

        def h(req):
            if req.url.path.endswith("/invoice/create"):
                return httpx.Response(200, json={"invoiceId": "invF", "pageUrl": "https://pay.mbnk.biz/invF"})
            if req.url.path.endswith("/fiscal-checks"):
                return httpx.Response(200, json={"checks": [{"id": "FC-1", "type": "sale", "status": "done", "taxUrl": "https://cabinet.tax.gov.ua/x",
                                                             "fiscalizationSource": "checkbox", "file": "JVBERi0="}]})
            return httpx.Response(200, json={"status": "success", "finalAmount": 1000})
        http = httpx.Client(transport=httpx.MockTransport(h))
        pl = pls.create(db, 10, "Чек", "https://crm.molodist.com.ua", p.id, http=http, account_id=a["id"])
        pl.sale_id = s.id; db.commit()
        pls.check_status(db, pl, http)
        out = pls.mono_fiscal(db, pl, http)
        db.refresh(s)
        assert out[0]["status"] == "done" and s.status == "paid"
        assert s.fiscal["fiscal_code"] == "FC-1" and s.fiscal["source"] == "monobank" and "Checkbox" in s.fiscal["rro_name"]



def test_paylink_send_channel(monkeypatch):
    import app.integrations.messaging as M
    sent = []
    monkeypatch.setattr(M, "send_to_patient", lambda p, t, prefer=None, **k: (sent.append((prefer, t)), M.SendResult(True, prefer[0]))[1])
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Patient(full_name="Надіслати Лінк", phone="+380671234599"); db.add(p); db.commit()
        pl = PayLink(provider="mono", amount=100, purpose="Тест", url="https://pay.mbnk.biz/x1", status="created", patient_id=p.id)
        db.add(pl); db.commit(); lid = pl.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = c.post(f"/paylinks/{lid}/send", data={"ch": "viber", "back": "/paylinks"}, follow_redirects=False)
        assert r.status_code == 303 and "Viber" in r.headers["location"] or "msg=" in r.headers["location"]
    assert sent and sent[0][0] == ["viber"] and "pay.mbnk.biz/x1" in sent[0][1]
    with SessionLocal() as db:
        assert db.get(PayLink, lid).info["sent"]["channel"] == "viber"


def test_awaiting_sale_cancel_and_archive():
    from app.models import Sale
    from app.services import sales as sl
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        pls.save_account(db, None, "mono", "monobank ФОП Архів", {"token": "MTOK"}, "Онлайн-оплати", True)
        p = Patient(full_name="Архів Чек", phone="+380671234577"); db.add(p); db.commit()
        s = sl.create(db, {"patient_id": p.id, "items": [{"kind": "other", "name": "Скасовуваний товар", "qty": 1, "price": 10}],
                           "payments": [{"method": sl.paylink_method("monobank ФОП Архів"), "amount": 10}]})
        pl = PayLink(provider="mono", amount=10, purpose="x", url="https://pay.mbnk.biz/zz", status="created", patient_id=p.id, sale_id=s.id)
        db.add(pl); db.commit(); sid = s.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert f"/sales/{sid}/cancel" in c.get("/sales").text
        r = c.post(f"/sales/{sid}/cancel", data={"back": "/sales"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            assert db.get(Sale, sid).status == "cancelled"
            assert all(x.status == "cancelled" for x in db.query(PayLink).filter(PayLink.sale_id == sid))
        c.post(f"/sales/{sid}/archive", data={"on": "1"})
        assert f"Ч-{sid}" not in c.get("/sales").text
        assert f"Ч-{sid}" in c.get("/sales/archive").text
        c.post(f"/sales/{sid}/archive", data={"on": "0"})
        with SessionLocal() as db:
            assert db.get(Sale, sid).archived_at is None


def test_sale_paid_by_link_is_fiscalized(monkeypatch):
    from app.models import Sale
    from app.services import sales as sl, cashboxes as cbx, fiscal as fis
    calls = []
    monkeypatch.setattr(fis, "fiscalize", lambda db, s, rid=None, *a, **k: calls.append((s.id, rid)) or {"fiscal_code": "F1"})
    monkeypatch.setattr(fis, "send_receipt", lambda db, s: None)
    with SessionLocal() as db:
        cbx.apply_seed(db)
        a = pls.save_account(db, None, "mono", "monobank ФОП Бондар", {"token": "MTOK"}, "Термінал ФОП Бондар", True)
        m = sl.paylink_method("monobank ФОП Бондар")
        p = Patient(full_name="Фіскал Лінк", phone="+380671234577"); db.add(p); db.commit()
        s = sl.create(db, {"patient_id": p.id, "items": [{"kind": "other", "name": "X", "qty": 1, "price": 1500.5}],
                           "payments": [{"method": m, "amount": 1500.5}]})
        s.fiscal = {"rro_pending": 7}; db.commit()
        http, st = _mono()
        pl = pls.create(db, 1500.5, f"Чек Ч-{s.id}", "https://crm.molodist.com.ua", p.id, http=http, account_id=a["id"])
        pl.sale_id = s.id; db.commit()
        st["status"] = "success"
        pls.check_status(db, pl, http)
        db.refresh(s)
        assert s.status == "paid" and calls == [(s.id, 7)]


def test_sale_page_manual_channels_and_mark_sent(monkeypatch):
    from app import security
    from app.models import PayLink
    from app.services import sales as sl, cashboxes as cbx
    security.login_limiter.fails.clear(); security.ip_limiter.fails.clear()
    monkeypatch.setattr(settings, "admin_password", "owner-pass-1")
    with SessionLocal() as db:
        cbx.apply_seed(db)
        a = pls.save_account(db, None, "mono", "monobank ФОП Тест", {"token": "MTOK"}, "Термінал ФОП Колган", True)
        m = sl.paylink_method("monobank ФОП Тест")
        p = Patient(full_name="Канал Пацієнтка", phone="+380671230099", communication_channel="Viber", instagram="@kanal.p"); db.add(p); db.commit()
        s = sl.create(db, {"patient_id": p.id, "items": [{"kind": "other", "name": "X", "qty": 1, "price": 1500.5}],
                           "payments": [{"method": m, "amount": 1500.5}]})
        http, st = _mono()
        pl = pls.create(db, 1500.5, f"Чек Ч-{s.id}", "https://crm.molodist.com.ua", p.id, http=http, account_id=a["id"])
        pl.sale_id = s.id; db.commit()
        sid, lid = s.id, pl.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        h = c.get(f"/sales/{sid}").text
        assert 'class="sch"' in h and 'data-ch="whatsapp"' in h and 'class="main " href="viber://' in h and "instagram.com/kanal.p" in h
        assert c.post(f"/paylinks/{lid}/mark-sent", data={"ch": "viber"}).json()["ok"]
    with SessionLocal() as db:
        assert db.get(PayLink, lid).info["sent"]["channel"] == "Viber"


def test_paid_once_webhook_and_poll_race(monkeypatch):
    """Вебхук і опитування статусу одночасно: фіскальний чек і сповіщення — рівно один раз."""
    from app.models import Sale, PayLink
    from app.services import sales as sl, cashboxes as cbx, fiscal as fis, alerts, notifications as nt
    calls, sent = [], []

    def fake_fis(db, s, rid=None, *a, **k):
        calls.append(s.id)
        s.fiscal = {**(s.fiscal or {}), "fiscal_code": "F%d" % len(calls)}; db.commit()
        return {"fiscal_code": "F1"}
    monkeypatch.setattr(fis, "fiscalize", fake_fis)
    monkeypatch.setattr(fis, "send_receipt", lambda db, s: None)
    monkeypatch.setattr(alerts, "send", lambda text, *a, **k: sent.append(text) or True)
    with SessionLocal() as db:
        cbx.apply_seed(db)
        a = pls.save_account(db, None, "mono", "monobank ФОП Дубль", {"token": "MTOK"}, "Термінал ФОП Бондар", True)
        m = sl.paylink_method("monobank ФОП Дубль")
        p = Patient(full_name="Дубль Оплата", phone="+380671234588"); db.add(p); db.commit()
        s = sl.create(db, {"patient_id": p.id, "items": [{"kind": "other", "name": "X", "qty": 1, "price": 1500.5}],
                           "payments": [{"method": m, "amount": 1500.5}]})
        s.fiscal = {"rro_pending": 7}; db.commit()
        http, st = _mono()
        pl = pls.create(db, 1500.5, f"Чек Ч-{s.id}", "https://crm.molodist.com.ua", p.id, http=http, account_id=a["id"])
        pl.sale_id = s.id; db.commit()
        lid, sid = pl.id, s.id
    st["status"] = "success"
    with SessionLocal() as d1, SessionLocal() as d2:
        p1, p2 = d1.get(PayLink, lid), d2.get(PayLink, lid)      # обидва запити бачать «created»
        pls.check_status(d1, p1, http)
        pls.check_status(d2, p2, http)
    assert calls == [sid]
    paid_msgs = [x for x in sent if "Оплачено онлайн" in x]
    assert len(paid_msgs) == 1 and "✅" in paid_msgs[0] and "Дубль Оплата" in paid_msgs[0] and f"Ч-{sid}" in paid_msgs[0]
    with SessionLocal() as db:
        from datetime import datetime, timedelta
        it = [x for x in nt.payments(db, datetime.utcnow() - timedelta(hours=30)) if x["id"] == lid]
        assert it and it[0]["kind"] == "paid" and it[0]["url"] == f"/sales/{sid}" and "1 500 ₴" in it[0]["title"]


def test_sale_fiscal_relink(monkeypatch):
    """Дубль у Checkbox: власник привʼязує до чека правильний фіскальний номер."""
    import httpx as _hx
    from app.models import Sale
    from app.services import sales as sl, cashboxes as cbx, fiscal as fis
    with SessionLocal() as db:
        cbx.apply_seed(db)
        p = Patient(full_name="Релінк", phone="+380671234599"); db.add(p); db.commit()
        s = sl.create(db, {"patient_id": p.id, "items": [{"kind": "other", "name": "X", "qty": 1, "price": 100}],
                           "payments": [{"method": "Готівка", "amount": 100}]})
        s.fiscal = {"fiscal_code": "QjTi_XyAaZk", "id": "old-id", "rro": 7, "rro_name": "ФОП"}; db.commit()
        monkeypatch.setattr(fis, "_rro", lambda db, rid: {"id": 7, "name": "ФОП БОНДАР", "license_key": "L", "pin": "1"})

        def h(req):
            if req.url.path.endswith("/signinPinCode"):
                return _hx.Response(200, json={"access_token": "T"})
            assert req.url.params["fiscal_code"] == "DfZ1GzhF54Y"
            return _hx.Response(200, json={"results": [{"id": "new-id", "fiscal_code": "DfZ1GzhF54Y", "fiscal_date": "2026-11-17T17:13:04+02:00"}]})
        f = fis.relink(db, s, "DfZ1GzhF54Y", _hx.Client(transport=_hx.MockTransport(h)))
        assert f["fiscal_code"] == "DfZ1GzhF54Y" and f["id"] == "new-id" and f["url"].endswith("new-id")
        assert f["replaced"][0]["fiscal_code"] == "QjTi_XyAaZk" and f["at"] == "17.11.2026 17:13"
