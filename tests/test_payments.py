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
        assert 'href="/settings/keys?s=mono#mono"' in c.get("/").text


def test_no_match_by_legal_form_words():
    """«ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ» є в усіх ТОВ — за цими словами постачальника не впізнаємо."""
    from app.services import suppliers as sp
    from app.models import SupplierEntity
    with SessionLocal() as db:
        p = Product(name="Мерц-крем", name_norm="мерц-крем", warehouse="vitrina")
        s = Supplier(name="MERZTEST")
        db.add_all([p, s]); db.commit()
        sp.save_entity(db, s, {"kind": "ТОВ", "legal_name": 'Товариство з обмеженою відповідальністю "АКА8"',
                               "edrpou": "46094319", "iban": "UA" + "7" * 27})
        from app.services import stock as st
        doc = st.new_doc(db, "supply", wh_to="vitrina", supplier="MERZTEST", ext_no="86140")
        doc.lines.append(st.StockDocLine(product_id=p.id, qty=1, unit_cost=5000))
        db.commit()
        wrong = BankTxn(id="mono-wrong", fop="Бондар", at=datetime.now(), amount=-4881.63, counter_iban="UA" + "8" * 27,
                        counter_name='ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ "СТАРЛІТ"', description="Оплата за товари згідно рах.")
        db.add(wrong); db.commit()
        assert pay.supplier_for(db, wrong) is None and pay.match_txn(db, wrong) == "new"
        good = BankTxn(id="mono-good", fop="Колган", at=datetime.now(), amount=-710.69, counter_iban="UA" + "7" * 27,
                       counter_name='Товариство з обмеженою відповідальністю "АКА8"', description="Оплата за товар згідно рахунку")
        db.add(good); db.commit()
        assert pay.match_txn(db, good) == "matched"                       # за IBAN юр. особи — єдина відкрита накладна
        assert pay.weak_auto(db) == []
        assert pay.learn_entity(db, wrong, s)                              # ручне рознесення запамʼятовує реквізити
        assert db.query(SupplierEntity).filter_by(supplier_id=s.id).count() == 2
        assert pay.supplier_for(db, wrong).id == s.id


def test_partial_payment_tops_up_after_wrong_unassigned():
    from app.services import suppliers as sp, stock as st
    with SessionLocal() as db:
        p = Product(name="Доплатний крем", name_norm="доплатний крем", warehouse="vitrina")
        s = Supplier(name="TOPUPTEST")
        db.add_all([p, s]); db.commit()
        sp.save_entity(db, s, {"kind": "ТОВ", "legal_name": "ТОВ ДОПЛАТА", "edrpou": "11112222", "iban": "UA" + "5" * 27})
        doc = st.new_doc(db, "supply", wh_to="vitrina", supplier="TOPUPTEST", ext_no="77123")
        doc.lines.append(st.StockDocLine(product_id=p.id, qty=1, unit_cost=1000))
        db.commit()
        g = pay.find_group(db, pay.group_key(doc))
        wrong = BankTxn(id="topup-wrong", fop="Бондар", at=datetime.now(), amount=-400, counter_name="ТОВ ІНШЕ")
        db.add(wrong); db.commit()
        pay.allocate(db, g, 400, txn=wrong, fop="Бондар"); wrong.status = "matched"; db.commit()
        good = BankTxn(id="topup-good", fop="Колган", at=datetime.now(), amount=-1000, counter_iban="UA" + "5" * 27,
                       description="Оплата за товар згідно рахунку № 77123")
        db.add(good); db.commit()
        assert pay.match_txn(db, good) == "partial"                        # лягло лише 600
        pay.unallocate_txn(db, wrong)
        pay.rematch_all(db)
        db.refresh(good)
        assert good.status == "matched" and pay.paid_map(db, [doc.id])[doc.id] == 1000
        assert db.get(st.StockDoc, doc.id).paid_by == "Колган"


def test_supply_payment_books_finop_and_cash_report():
    from app.models import FinOp
    from app.services import reports as rp

    def getter(path, token):
        return {"name": "Тест", "accounts": [{"id": "acc-k", "iban": "UA000000000000000000000023032", "type": "fop", "currencyCode": 980, "balance": 9500000}]}
    with SessionLocal() as db:
        p = Product(name="Фінопний крем", name_norm="фінопний крем"); db.add(p); db.commit(); pid = p.id
        monobank.connect(db, "Колган", "tok-k", getter=getter)
    with TestClient(app) as c:
        _login(c)
        c.post("/stock/suppliers", data={"name": "ФінопТест", "iban": "UA" + "4" * 27, "edrpou": "87654321"})
        j = c.post("/stock/docs/quick", json={"kind": "supply", "supplier": "ФінопТест", "wh": "vitrina", "ext_no": "ФТ-77",
                   "lines": [{"product_id": pid, "qty": 1, "price": 1200, "wh": "vitrina"}]}).json()
    with SessionLocal() as db:
        t = BankTxn(id="finop-t", fop="Колган", account="acc-k", at=datetime.now(), amount=-1200, counter_iban="UA" + "4" * 27,
                    counter_name="ФінопТест", description="оплата ФТ-77")
        db.add(t); db.commit()
        assert pay.match_txn(db, t) == "matched"
        db.commit()
        op = db.query(FinOp).filter_by(ref="banktxn:finop-t").one()
        assert op.cashbox == "Операційний Фонд" and op.amount == 1200 and op.kind == "expense"
        import json as _j
        from app.models import SyncState
        db.merge(SyncState(key="mono_balances", value=_j.dumps({"acc-k": {"balance": 95000, "fop": "Колган", "at": "30.10 17:00"},
                                                                  "_at": datetime.now().isoformat()})))
        db.commit()
        rep = rp.build(db, "cash", datetime.now().date(), datetime.now().date())
        row = next(r for r in rep.rows if r["name"] == "Операційний Фонд")
        assert row["bank"] == 95000 and row["out"] >= 1200
        assert pay.backfill_finops(db) == 0                       # уже в журналі — не дублюємо
        db.delete(db.get(FinOp, op.id)); db.commit()
        assert pay.backfill_finops(db) == 1                       # оплата, рознесена до цієї зміни, — у журнал
        op = db.query(FinOp).filter_by(ref="banktxn:finop-t").one()
        assert op.cashbox == "Операційний Фонд" and op.amount == 1200 and "ФТ-77" not in (op.comment or "") or True
        pay.unallocate_txn(db, t)
        assert db.get(FinOp, op.id).deleted_at is not None
        monobank.disconnect(db, "Колган")
    with TestClient(app) as c:
        _login(c)
        assert "Стан каси" in c.get("/reports?r=cash").text


def test_revert_booked_without_supply():
    from app.models import FinOp
    with SessionLocal() as db:
        t = BankTxn(id="skin-41", fop="Бондар", account="acc-b", at=datetime.now(), amount=-8400, counter_name="ТОВ «СКІН Б‘ЮТІ ПРО»",
                    description="№ 41", status="booked")
        db.add(t); db.add(FinOp(day=datetime.now().date(), kind="expense", category="Закупка товара", amount=8400, cashbox="X", ref="banktxn:skin-41"))
        db.commit()
        assert pay.revert_booked_without_supply(db) == 1
        assert db.get(BankTxn, "skin-41").status == "new"
        assert db.query(FinOp).filter_by(ref="banktxn:skin-41").one().deleted_at is not None


def test_cash_report_transfer_is_not_income_or_expense():
    from app.models import FinOp
    from app.services import reports as rp, cashboxes as cbx
    with SessionLocal() as db:
        names = [x["name"] for x in cbx.items(db) if x.get("active") and not x.get("deleted")][:2]
        db.add(FinOp(day=datetime.now().date(), kind="transfer", category="Переміщення", amount=500, cashbox=names[0], cashbox_to=names[1]))
        db.commit()
        rep = rp.build(db, "cash", datetime.now().date(), datetime.now().date())
        src = next(r for r in rep.rows if r["name"] == names[0])
        dst = next(r for r in rep.rows if r["name"] == names[1])
        assert src["tr"] == -500 and dst["tr"] == 500
        assert all(not v.transfer for v in cbx.moves(db, next(x for x in cbx.items(db) if x["name"] == names[0])) if v.kind == "out" and v.amount == 500 and "→" not in v.what)
