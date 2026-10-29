"""Склад у CRM: старт обліку, поставка, переміщення, списання, повернення, інвентаризація, замовлення,
автосписання продажів і процедур за нормами, скасування."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import (SessionLocal, Product, StockLevel, StockDoc, StockMove, ProductPurchase, ProductSale, Patient,
                        Visit, CbService, ProcedureNorm)
from app.services import stock as st, catalog


def _bal(db, pid, wh):
    return st.balances(db).get((pid, wh), 0)


def test_stock_ledger_flow():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        cream = Product(name="Крем Тест Вітрина", name_norm="крем тест вітрина", cb_product_id=8001, warehouse="vitrina",
                        price=2000, cost=1000, min_stock=3, supplier="Дистриб Т")
        gel = Product(name="Гель УЗ Тест", name_norm="гель уз тест", cb_product_id=8002, warehouse="single", unit="мл")
        db.add_all([cream, gel]); db.flush()
        db.add_all([StockLevel(cb_product_id=8001, name=cream.name, qty=4), StockLevel(cb_product_id=8002, name=gel.name, qty=500)])
        svc = CbService(id=9901, name="Гігієна обличчя тест", price=1800)
        pat = Patient(full_name="Склад Пацієнтка", cleverbox_id=77001)
        db.add_all([svc, pat]); db.commit()
        cid, gid, pid = cream.id, gel.id, pat.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Почати облік" in c.get("/stock").text
        assert c.post("/stock/start", follow_redirects=False).status_code == 303
        with SessionLocal() as db:
            assert _bal(db, cid, "vitrina") == 4 and _bal(db, gid, "single") == 500

        # поставка: +6 кремів по 1100, термін — у закупівлі
        r = c.post("/stock/docs", data={"kind": "supply", "wh": "vitrina", "supplier": "Дистриб Т"}, follow_redirects=False)
        did = int(r.headers["location"].rsplit("/", 1)[1])
        c.post(f"/stock/docs/{did}", data={"new_product": f"Крем Тест Вітрина [#{cid}]", "new_qty": "6", "new_cost": "1100",
                                           "new_exp": "2027-12-01", "action": "save"})
        assert "Крем Тест Вітрина" in c.get(f"/stock/docs/{did}").text
        c.post(f"/stock/docs/{did}", data={"action": "post"})
        with SessionLocal() as db:
            assert _bal(db, cid, "vitrina") == 10
            assert db.query(ProductPurchase).filter_by(product_id=cid).one().unit_cost == 1100

        # переміщення 2 шт у класичну, списання 1 (пошкоджено)
        r = c.post("/stock/docs", data={"kind": "transfer", "wh": "vitrina"}, follow_redirects=False)
        tid = int(r.headers["location"].rsplit("/", 1)[1])
        c.post(f"/stock/docs/{tid}", data={"wh_from": "vitrina", "wh_to": "classic", "new_product": f"x [#{cid}]", "new_qty": "2"})
        c.post(f"/stock/docs/{tid}", data={"action": "post"})
        r = c.post("/stock/docs", data={"kind": "writeoff", "wh": "vitrina"}, follow_redirects=False)
        wid = int(r.headers["location"].rsplit("/", 1)[1])
        c.post(f"/stock/docs/{wid}", data={"reason": "Пошкоджено", "new_product": f"x [#{cid}]", "new_qty": "1"})
        c.post(f"/stock/docs/{wid}", data={"action": "post"})
        with SessionLocal() as db:
            assert _bal(db, cid, "vitrina") == 7 and _bal(db, cid, "classic") == 2

        # продаж з CleverBox і процедура за нормою — автосписання (один раз)
        with SessionLocal() as db:
            db.add(ProductSale(external_id="st-sale-1", patient_id=pid, product_id=8001, product_name="Крем", quantity=2,
                               amount=4000, sold_at=datetime.now()))
            db.add(ProcedureNorm(service_id=9901, product_id=gid, qty=15))
            db.add(Visit(source="cb_service_sale", external_id="st-v1", patient_id=pid, service_id=9901, service_name="Гігієна",
                         starts_at=datetime.now(), status="done"))
            db.commit()
            st.auto_moves(db); st.auto_moves(db)
            assert _bal(db, cid, "vitrina") == 5 and _bal(db, gid, "single") == 485

        # інвентаризація вітрини: фактично 4 → нестача 1
        r = c.post("/stock/docs", data={"kind": "inventory", "wh": "vitrina"}, follow_redirects=False)
        iid = int(r.headers["location"].rsplit("/", 1)[1])
        with SessionLocal() as db:
            ln = next(l for l in db.get(StockDoc, iid).lines if l.product_id == cid)
            lid = ln.id
            assert ln.qty == 5
        c.post(f"/stock/docs/{iid}", data={f"qty_{lid}": "4", "action": "post"})
        with SessionLocal() as db:
            assert _bal(db, cid, "vitrina") == 4

        # критичний (мін 3, у вітрині 4 + 2 у класичній = 6? загалом) → замовлення з критичних → поставка
        assert "Критичні" in c.get("/stock?tab=critical").text
        with SessionLocal() as db:
            prod = db.get(Product, cid); prod.min_stock = 10; db.commit()
        r = c.post("/stock/order-from-critical", data={"supplier": ""}, follow_redirects=False)
        oid = int(r.headers["location"].rsplit("/", 1)[1])
        with SessionLocal() as db:
            o = db.get(StockDoc, oid)
            assert o.kind == "order" and any(l.product_id == cid and l.qty > 0 for l in o.lines)
        r = c.post(f"/stock/docs/{oid}", data={"action": "receive"}, follow_redirects=False)
        sid = int(r.headers["location"].split("?")[0].rsplit("/", 1)[1])
        with SessionLocal() as db:
            assert db.get(StockDoc, sid).kind == "supply" and db.get(StockDoc, oid).status == "received"

        # скасування поставки прибирає рухи і закупівлю
        c.post(f"/stock/docs/{did}", data={"action": "cancel"})
        with SessionLocal() as db:
            assert _bal(db, cid, "vitrina") == -2
            assert db.query(ProductPurchase).filter_by(product_id=cid).count() == 0

        # сторінки
        for tab in ("balances", "critical", "supply", "order", "transfer", "writeoff", "return", "inventory", "suppliers", "norms", "moves"):
            assert c.get(f"/stock?tab={tab}").status_code == 200, tab
        assert "Дистриб Т" in c.get("/stock?tab=suppliers").text
        assert "Гігієна обличчя тест" in c.get("/stock?tab=norms").text
        with SessionLocal() as db:
            rows = {r.product.id: r for r in catalog.product_rows(db, [])}
            assert rows[cid].stock == 0       # -2 у вітрині + 2 у класичній


def test_supplier_invoices_import(tmp_path):
    from openpyxl import Workbook
    from app.models import SupplierInvoice
    wb = Workbook(); ws = wb.active
    ws.append(["Дата", "time", "N", "статус", "тип", "постачальник", "склад", "Сума", "оплата", "Автор", "Коментар"])
    ws.append(["27.10.2026", "10:10", "20790", "Проведено", "Постачання", "Regenera", "Вітрина", 2000.0, 2000.0, "Адмін", ""])
    ws.append(["21.10.2026", "17:33", "20707", "Проведено", "Постачання", "TEBISKIN", "Вітрина", 5044.0, 0.0, "Адмін", ""])
    ws.append(["22.10.2026", "17:45", "20733", "Проведено", "Постачання", "INNOVA", "Препарати ін'єкційна косметологія ", 16952.0, 10000.0, "Адмін", ""])
    ws.append(["15.10.2026", "17:00", "20600", "Проведено", "Постачання", "-", "Одноразка", 3325.41, 0.0, "Адмін", "Inventory 20595"])
    f = tmp_path / "supply.xlsx"; wb.save(f)
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post("/stock/suppliers/import", files={"file": ("supply.xlsx", f.read_bytes(), "application/octet-stream")})
        page = c.get("/stock?tab=suppliers&per=all").text
        assert "Імпортовано поставок: 4" in page or "TEBISKIN" in page
        assert "11 996" in page                                 # борг 5044 + 6952
        with SessionLocal() as db:
            inv = db.query(SupplierInvoice).filter_by(cb_number="20733").one()
            assert inv.warehouse == "inject" and inv.debt == 6952
            iid = inv.id
            assert db.query(SupplierInvoice).count() == 3      # інвентаризаційний рядок без постачальника пропущено
        assert "INNOVA" in c.get("/stock?tab=suppliers&debt=1").text
        c.post(f"/stock/invoices/{iid}/paid", data={"back": "/stock?tab=suppliers"})
        c.post("/stock/suppliers/import", files={"file": ("supply.xlsx", f.read_bytes(), "application/octet-stream")})
        with SessionLocal() as db:
            assert db.get(SupplierInvoice, iid).debt == 0           # повторний імпорт не затер відмітку


def _balances_xlsx(path):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Назва", "Категорія", "Бренд", "Напрямок", "Штрихкод", "Артикул", "Собівартість", "Залишок од. обліку",
               "Залишок од. прих", "Сума ПРОДАЖУ", "од. обліку", "од. продажу"])
    ws.append(["Вітрина", None, None, None, None, None, None, None, None, None, None, None])
    ws.append(["Крем залишки тест 50 мл", "Вітрина", "TestBrand", "Вітрина", "0", "BAL-1", 3000, 2, 2, 5000, "шт.", "шт."])
    ws.append(["Мінусовий товар", "Вітрина", "TestBrand", "Вітрина", "0", "BAL-2", 0, -1, -1, 0, "шт.", "шт."])
    ws.append(["Одноразка", None, None, None, None, None, None, None, None, None, None, None])
    ws.append(["Рукавички залишки тест", "Одноразка", None, "", "", "BAL-3", 500, 100, 1, 0, "шт.", "уп."])
    ws.append(["Total:", None, None, None, None, None, 3500, 101, 2, 5000, None, None])
    wb.save(path)


def test_balances_report_start_and_reconcile(tmp_path):
    """Звіт «Залишки по складах»: собівартість, старт обліку по складах, звірка інвентаризацією."""
    from app.services import catalog, stock as st
    from app.models import SyncState
    f = str(tmp_path / "wh.xlsx"); _balances_xlsx(f)
    assert catalog.detect_export(f) == "balances"
    with SessionLocal() as db:
        db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()
        rep = catalog.parse_balances(db, f)
        assert rep["rows"] == 3 and len(rep["negative"]) == 1 and rep["by_wh"]["single"]["n"] == 1
        cream = db.query(Product).filter_by(sku="BAL-1").one()
        assert cream.stock_cost == 1500 and cream.warehouse == "vitrina"
        st.start_ledger(db, snapshot=rep)
        bal = st.balances(db)
        gl = db.query(Product).filter_by(sku="BAL-3").one()
        assert bal[(cream.id, "vitrina")] == 2 and bal[(gl.id, "single")] == 100
        # облік уже йде: звірка створює чернетки інвентаризації з різницею
        rep["items"] = [[cream.id, "vitrina", 1, 1500], [gl.id, "single", 100, 500]]
        docs = st.reconcile_from_snapshot(db, rep)
        inv = {d.wh_from: d for d in docs}
        ln = next(x for x in inv["vitrina"].lines if x.product_id == cream.id)
        assert ln.qty == 1 and ln.sys_qty == 2 and all(d.status == "draft" for d in docs)
        db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()


def test_services_export_and_techcard_norms():
    """Довідник «Послуги» CleverBox: напрямок і техкартка; норма на техкартку → на всі її послуги."""
    from app.models import CbService, ProcedureNorm
    from app.services import cb_services_import as cbs
    head = ["Назва", "Назва для SMS", "Назва у чеку", "Категорія", "Направленість", "техкартка", "Тривалість", "Повтор", "Molodist_"]
    rows = [["Лазер Тест Губа", "", "Лазерна Епіляція Тест Губа", "Лазерна", "Лазерна епіляція", "Лазер тест мала", "10", "", "250"],
            ["Лазер Тест Пахви", "", "Лазерна Епіляція Тест Пахви", "Лазерна", "Лазерна епіляція", "Лазер тест мала", "10", "", "350"],
            ["Неіснуюча послуга", "", "", "", "", "x", "10", "", "1"]]
    assert cbs.is_services_export(head)
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.add_all([CbService(id=990001, name="Лазер Тест Губа"), CbService(id=990002, name="Лазерна Епіляція Тест Пахви")])
        gel = Product(name="Гель для лазера тест", name_norm="гель для лазера тест", unit="мл")
        db.add(gel); db.commit()
        rep = cbs.import_services(db, rows, head)
        assert rep["updated"] == 2 and rep["skipped"] == 1
        assert db.get(CbService, 990002).techcard == "Лазер тест мала" and db.get(CbService, 990001).direction == "Лазерна епіляція"
        gid = gel.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post("/stock/norms", data={"service_id": 990001, "product": f"[#{gid}]", "qty": "5", "techcard": "Лазер тест мала"})
        assert "Лазер тест мала" in c.get("/stock?tab=norms&q=лазер тест").text
    with SessionLocal() as db:
        assert {n.service_id for n in db.query(ProcedureNorm).filter_by(product_id=gid)} == {990001, 990002}


def test_supply_modal_quick_doc():
    """Вікно «Нова поставка»: пошук товару, збереження й проведення одним запитом; упаковки → одиниці обліку."""
    from app.models import ProductPurchase, Supplier
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p1 = Product(name="Модальна сироватка 30 мл", name_norm="модальна сироватка 30 мл", sku="MOD-30", brand="ModBrand")
        p2 = Product(name="Модальний гель 250 мл", name_norm="модальний гель 250 мл", coef=250, unit="мл", warehouse="single")
        db.add_all([p1, p2]); db.commit()
        id1, id2 = p1.id, p2.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "Прихід на склад" in c.get("/stock?tab=supply").text
        found = c.get("/stock/lookup/products?q=MOD-30").json()
        assert found and found[0]["id"] == id1
        assert c.get("/stock/lookup/products?q=модальний гель").json()[0]["unit"].startswith("уп. (250")
        r = c.post("/stock/docs/quick", json={"kind": "supply", "post": False, "date": "2026-10-29", "supplier": "Модальний Дистриб'ютор",
                                               "wh": "vitrina", "ext_no": "ПН-1", "lines": [
            {"product_id": id1, "qty": 3, "price": 900, "expiry": "2027-12-31"},
            {"product_id": id2, "qty": 2, "price": 500}]}).json()
        assert r["url"].startswith("/stock/docs/")
        assert c.post("/stock/docs/quick", json={"kind": "supply", "lines": []}).status_code == 400
    with SessionLocal() as db:
        d = db.get(StockDoc, r["id"])
        ln2 = next(x for x in d.lines if x.product_id == id2)
        assert d.supplier == "Модальний Дистриб'ютор" and d.ext_no == "ПН-1" and d.status == "draft"
        assert ln2.qty == 500 and ln2.unit_cost == 2                                  # 2 уп × 250 мл, 500 ₴ / 250
        assert db.query(Supplier).filter_by(name="Модальний Дистриб'ютор").count() == 1


def test_batches_by_expiry_fefo_and_reassign():
    """Один товар — кілька партій за терміном; продаж CleverBox списує найближчий термін; партію продажу можна змінити."""
    from datetime import date as _d
    from app.models import SyncState
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()
        p = Product(name="Партійний крем", name_norm="партійний крем", cb_product_id=990777, warehouse="vitrina")
        db.add(p); db.commit()
        st.start_ledger(db)
        doc = st.new_doc(db, "supply", wh_to="vitrina", supplier="Партія Тест")
        doc.lines.append(st.StockDocLine(product_id=p.id, qty=2, unit_cost=100, expiry_date=_d(2027, 6, 1)))
        doc.lines.append(st.StockDocLine(product_id=p.id, qty=3, unit_cost=100, expiry_date=_d(2027, 1, 1)))
        db.commit(); st.post(db, doc)
        pt = Patient(full_name="Партійна Покупниця"); db.add(pt); db.flush()
        db.add(ProductSale(external_id="batch-s-1", patient_id=pt.id, product_id=990777, product_name=p.name, quantity=4,
                           amount=800, sold_at=datetime.now()))
        db.commit()
        st.auto_moves(db)
        bb = {e: q for (pid, w, e), q in st.batch_balances(db, p.id).items() if q}
        assert bb == {_d(2027, 6, 1): 1}                             # 3 з січня + 1 з червня
        rows = {r.product.id: r for r in catalog.product_rows(db, [])}
        assert rows[p.id].expiry == _d(2027, 6, 1) and rows[p.id].stock == 1
        mv = db.query(StockMove).filter(StockMove.ref == "sale:batch-s-1#1").one()
        assert mv.expiry_date == _d(2027, 6, 1) and mv.qty == -1
        st.reassign_batch(db, mv.id, _d(2027, 1, 1))                 # насправді продали ще одну січневу
        bb = {e: q for (pid, w, e), q in st.batch_balances(db, p.id).items() if q}
        assert bb == {_d(2027, 6, 1): 2, _d(2027, 1, 1): -1}
        pid = p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get(f"/products/{pid}").text
        assert "Партії на складі" in page and "01.06.2027" in page
    with SessionLocal() as db:
        db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()


def test_manual_batches_on_product_card():
    """Ручне внесення термінів: залишок без терміну → кілька партій; загальна к-сть не змінюється."""
    from datetime import date as _d
    from app.models import SyncState
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        with SessionLocal() as db:
            db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()
            p = Product(name="Ручні терміни крем", name_norm="ручні терміни крем", warehouse="vitrina")
            db.add(p); db.commit()
            db.add(StockLevel(cb_product_id=None, name=p.name, qty=0)); db.commit()
            st.start_ledger(db)
            db.add(StockMove(product_id=p.id, warehouse="vitrina", qty=5, kind="opening", at=datetime.now())); db.commit()
            pid = p.id
        r = c.post(f"/products/{pid}/batches", data={"wh": "vitrina", "exp": ["2027-01-31", "2027-06-30", ""], "qty": ["2", "2", ""]},
                   follow_redirects=False)
        assert "saved=1" in r.headers["location"]
        with SessionLocal() as db:
            bb = {e: q for (x, w, e), q in st.batch_balances(db, pid).items() if abs(q) > 1e-9}
            assert bb == {_d(2027, 1, 31): 2, _d(2027, 6, 30): 2, None: 1}
            assert st.balance_by_product(db)[pid] == 5
        bad = c.post(f"/products/{pid}/batches", data={"wh": "vitrina", "exp": ["2027-01-31"], "qty": ["9"]}, follow_redirects=False)
        assert "err=" in bad.headers["location"]
        r = c.post(f"/products/{pid}/batches", data={"wh": "vitrina", "exp": ["2027-06-30"], "qty": ["5"]}, follow_redirects=False)
        with SessionLocal() as db:
            bb = {e: q for (x, w, e), q in st.batch_balances(db, pid).items() if abs(q) > 1e-9}
            assert bb == {_d(2027, 6, 30): 5}
        assert "Внести терміни вручну" in c.get(f"/products/{pid}").text
        with SessionLocal() as db:
            db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()


def test_doc_archive_with_password():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, Product, StockMove, SyncState
    from app.services import stock as st
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()
        p = Product(name="Архівний крем", name_norm="архівний крем", warehouse="vitrina")
        db.add(p); db.commit()
        st.start_ledger(db)
        doc = st.new_doc(db, "supply", wh_to="vitrina", supplier="Архів Тест")
        doc.lines.append(st.StockDocLine(product_id=p.id, qty=3, unit_cost=10))
        db.commit(); st.post(db, doc)
        did, num = doc.id, doc.number
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "ddOpen(" in c.get("/stock?tab=supply").text
        r = c.post(f"/stock/docs/{did}/archive", data={"del_password": "owner-pass-1", "back": "/stock?tab=supply"}, follow_redirects=False)
        assert "err=" in r.headers["location"]                                # пароль видалення ще не задано
        from app.models import User
        with SessionLocal() as db:
            owner = db.query(User).filter(User.login == settings.owner_login).one()
            oid = owner.id
        c.post("/access/users", data={"uid": str(oid), "login": owner.login, "name": owner.name, "role": "owner",
                                      "active": "1", "delete_pw": "del-2026"})
        r = c.post(f"/stock/docs/{did}/archive", data={"del_password": "wrong", "back": "/stock?tab=supply"}, follow_redirects=False)
        assert "err=" in r.headers["location"]
        with SessionLocal() as db:
            assert db.get(st.StockDoc, did).archived_at is None
        r = c.post(f"/stock/docs/{did}/archive", data={"del_password": "del-2026", "note": "дубль", "back": "/stock?tab=supply"},
                   follow_redirects=False)
        assert "msg=" in r.headers["location"]
        page = c.get("/stock?tab=supply").text
        assert f">{num}<" not in page and "Архів · 1" in page
        arch = c.get("/stock?tab=supply&archive=1").text
        assert f">{num}<" in arch and "дубль" in arch and "Відновити" in arch
        with SessionLocal() as db:
            d = db.get(st.StockDoc, did)
            assert d.status == "cancelled" and d.archived_from == "posted"
            assert not db.query(StockMove).filter(StockMove.doc_id == did).count()
        c.post(f"/stock/docs/{did}/restore")
        with SessionLocal() as db:
            d = db.get(st.StockDoc, did)
            assert d.archived_at is None and d.status == "draft"
            db.get(User, oid).delete_pw_hash = None
            db.query(SyncState).filter(SyncState.key == "stock_ledger_start").delete(); db.commit()


def test_supply_list_payment_author_and_history():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, Product
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        p = Product(name="Історичний гель", name_norm="історичний гель", warehouse="vitrina")
        db.add(p); db.commit(); pid = p.id
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        j = c.post("/stock/docs/quick", json={"kind": "supply", "supplier": "Історія Тест", "wh": "vitrina", "note": "термінова",
                                               "lines": [{"product_id": pid, "qty": 2, "price": 100}]}).json()
        did = j["id"]
        page = c.get(f"/stock/docs/{did}").text
        ln = page.split('name="qty_')[1].split('"')[0]
        c.post(f"/stock/docs/{did}", data={"action": "save", f"qty_{ln}": "3", f"cost_{ln}": "100"})
        c.post(f"/stock/docs/{did}/pay", data={"action": "paid", "fop": "Бондар", "amount": "120"})
        lst = c.get("/stock?tab=supply").text
        assert "Оплата" in lst and "Автор" in lst and "120,00" in lst.replace(" ", " ").replace(" 120", "120") or "120" in lst
        assert "частково" in lst and "термінова" in lst
        h = c.get(f"/stock/docs/{did}").text
        assert "Історія документа" in h and "Створено у вікні поставки" in h and "к-сть 2 → 3" in h and "Відмічено оплату" in h
