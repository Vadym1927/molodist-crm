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
