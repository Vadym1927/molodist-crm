"""Оплати постачальникам: накладна на 2 склади, рознесення monobank за IBAN і № накладної, ручна оплата, файли."""
from datetime import datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, StockDoc, Supplier, BankTxn, PaymentAlloc, StockDocFile
from app.services import payments as pay
from app.integrations import monobank


def _login(c):
    settings.admin_password = "owner-pass-1"
    c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})


def test_split_supply_and_monobank_allocation():
    with SessionLocal() as db:
        a = Product(name="Оплатний крем", name_norm="оплатний крем")
        b = Product(name="Оплатний філер", name_norm="оплатний філер")
        db.add_all([a, b]); db.commit()
        ia, ib = a.id, b.id
    with TestClient(app) as c:
        _login(c)
        r = c.post("/stock/supliers" if False else "/stock/suppliers", data={"name": "ПлатіжТест", "legal_name": "ТОВ «ПлатіжТест»",
                   "edrpou": "12345678", "iban": "UA21 3223 1300 0002 6007 2335 6600 1", "bank": "Тест банк"}, follow_redirects=False)
        assert r.status_code == 303
        j = c.post("/stock/docs/quick", json={"kind": "supply", "supplier": "ПлатіжТест", "wh": "vitrina", "ext_no": "РФ-4512",
                   "lines": [{"product_id": ia, "qty": 2, "price": 1000, "wh": "vitrina"},
                             {"product_id": ib, "qty": 1, "price": 3000, "wh": "inject"}]}).json()
        assert len(j["ids"]) == 2 and "розділено на 2 склади" in j["warning"]
        up = c.post(f"/stock/docs/{j['ids'][0]}/files", files=[("files", ("nakl.jpg", b"\xff\xd8fakejpeg", "image/jpeg"))]).json()
        assert up["files"] == 1
    with SessionLocal() as db:
        d1, d2 = db.get(StockDoc, j["ids"][0]), db.get(StockDoc, j["ids"][1])
        assert d1.pay_group and d1.pay_group == d2.pay_group and {d1.wh_to, d2.wh_to} == {"vitrina", "inject"}
        assert db.query(StockDocFile).filter(StockDocFile.doc_id.in_(j["ids"])).count() == 2      # файл на обидва склади
        sup = db.query(Supplier).filter_by(name="ПлатіжТест").one()
        assert sup.iban == "UA213223130000026007233566001" and sup.edrpou == "12345678"
        g = pay.find_group(db, d1.pay_group)
        assert g.total == 5000 and g.state == "unpaid"
        # платіж monobank 5000 ₴ за IBAN + № у призначенні → рознесення 2000 / 3000 і по товарах
        t = BankTxn(id="mono-t-1", fop="Бондар", at=datetime.now(), amount=-5000, counter_iban="UA213223130000026007233566001",
                    counter_name="ТОВ ПЛАТІЖТЕСТ", description="Оплата за товар згідно рах. РФ-4512")
        db.add(t); db.commit()
        assert pay.match_txn(db, t) == "matched"
        paid = pay.paid_map(db, j["ids"])
        assert paid[d1.id] == 2000 and paid[d2.id] == 3000
        assert db.get(StockDoc, d1.id).paid_by == "Бондар"
        assert pay.find_group(db, d1.pay_group).state == "paid"
        assert set(pay.modes_map(db, [d1.id])[d1.id]) == {"auto"}                 # рознесено автоматично
        pay.unallocate_txn(db, t)
        assert pay.find_group(db, d1.pay_group).state == "unpaid"
    with TestClient(app) as c:
        _login(c)
        c.post(f"/stock/docs/{j['ids'][1]}/pay", data={"action": "paid", "fop": "Колган", "amount": "1000"})
        page = c.get(f"/stock/docs/{j['ids'][0]}").text
        assert "частково" in page and "Файли й фото" in page and "UA213223130000026007233566001" in page
        assert "Платежі monobank" in c.get("/stock?tab=suppliers&s=ПлатіжТест").text
        assert "✋ вручну" in page and "✋ вручну" in c.get("/stock?tab=supply").text
        c.post("/stock/mono/txn/mono-t-1", data={"action": "assign", "group": d1.pay_group})
        assert "🏦 monobank · вручну" in c.get("/stock?tab=supply").text
        assert "рознесено вручну" in c.get(f"/stock/docs/{j['ids'][0]}").text
        c.post("/stock/mono/txn/mono-t-1", data={"action": "unassign"})
    with SessionLocal() as db:
        paid = pay.paid_map(db, j["ids"])
        assert round(paid[j["ids"][0]] + paid[j["ids"][1]], 2) == 1000 and paid[j["ids"][0]] == 400   # пропорційно 2000:3000


def test_monobank_connect_and_sync_fake():
    calls = []

    def getter(path, token):
        calls.append(path)
        assert token == "tok-1"
        if path == "/personal/client-info":
            return {"name": "Тест", "accounts": [{"id": "acc-fop", "iban": "UA00", "type": "fop", "currencyCode": 980},
                                                 {"id": "acc-black", "type": "black", "currencyCode": 980}]}
        return [{"id": "st-1", "time": 1790000000, "amount": -123400, "counterName": "ФОП Хтось", "description": "оплата"},
                {"id": "st-2", "time": 1790000100, "amount": 50000, "description": "надходження"}]

    with SessionLocal() as db:
        assert monobank.connect(db, "Колган", "tok-1", getter=getter) is None
        pub = monobank.public(db)[0]
        assert pub["watch"] == ["acc-fop"] and "token" not in pub
        rep = monobank.sync(db, days=2, getter=getter, sleep=lambda s: None)
        assert rep["Колган"] == "1 нових списань"
        t = db.get(BankTxn, "st-1")
        assert t.amount == -1234 and t.fop == "Колган" and db.get(BankTxn, "st-2") is None
        monobank.disconnect(db, "Колган")
        assert monobank.public(db) == []


def test_keys_page():
    with TestClient(app) as c:
        _login(c)
        page = c.get("/settings/keys").text
        assert "Ключі й інтеграції" in page and "ФОП Колган" in page and "Вставте токен monobank" in page
        assert 'href="/settings/keys"' in c.get("/").text
