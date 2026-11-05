"""Прайс постачальника по бренду → зіставлення → підстановка закупівлі у валюті; прайс у картці постачальника."""
import io

import openpyxl
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, Supplier, SupplierPrice


def _xlsx():
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Прайс ПрайсБренд на 01.11.2026"]); ws.append([])
    ws.append(["Код товару", "Найменування", "Штрих-код", "Ціна закупівлі, EUR", "РРЦ, EUR"])
    ws.append(["PB-1", "Сироватка судинна 30 мл", None, 24.5, 49])
    ws.append([None, "Крем навколо очей Smart C 15 мл", None, "12,30", "25"])
    ws.append(["X-9", "Невідомий засіб", None, 5, None])
    b = io.BytesIO(); wb.save(b)
    return b.getvalue()


def test_supplier_price_upload_apply(tmp_path):
    settings.admin_password = "owner-pass-1"
    old, settings.files_dir = settings.files_dir, str(tmp_path)
    with SessionLocal() as db:
        s = Supplier(name="Прайс Постач"); db.add(s); db.commit()
        p1 = Product(name="ПБ Сироватка судинна", name_norm="пб сироватка судинна", brand="ПрайсБренд", sku="pb-1", supplier="Прайс Постач", cost_currency="EUR", cost_fx=20)
        p2 = Product(name="Крем навколо очей Smart C 15 мл", name_norm="крем навколо очей smart c 15 мл", brand="ПрайсБренд", supplier="Прайс Постач")
        db.add_all([p1, p2]); db.commit()
        ids, sid = (p1.id, p2.id), s.id
    try:
        with TestClient(app) as c:
            c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
            assert "Прайс постачальника" in c.get("/finance/pricing?brand=ПрайсБренд").text
            r = c.post("/finance/pricing/price", data={"brand": "ПрайсБренд", "currency": "", "price_date": "2026-11-01"},
                       files={"file": ("prais.xlsx", _xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                       follow_redirects=False)
            assert r.status_code == 303 and "/finance/pricing/price/" in r.headers["location"], r.headers["location"]
            spid = int(r.headers["location"].rsplit("/", 1)[1])
            with SessionLocal() as db:
                sp = db.get(SupplierPrice, spid)
                assert sp.currency == "EUR" and sp.n_rows == 3 and sp.n_matched == 2 and sp.supplier_id == sid
                assert [x["how"] for x in sp.rows][:2] == ["артикул", "назва 100%"]
            v = c.get(f"/finance/pricing/price/{spid}").text
            assert "Невідомий засіб" in v and "+22.5%" in v
            r = c.post(f"/finance/pricing/price/{spid}", data={"row": ["0", "1"], "rrp": "1"}, follow_redirects=False)
            assert r.status_code == 303
            with SessionLocal() as db:
                a, b = db.get(Product, ids[0]), db.get(Product, ids[1])
                assert (a.cost_currency, a.cost_fx, a.rrp_fx) == ("EUR", 24.5, 49) and (b.cost_currency, b.cost_fx, b.rrp_fx) == ("EUR", 12.3, 25)
                assert db.get(SupplierPrice, spid).n_applied == 2
            card = c.get(f"/stock/suppliers/{sid}/card").text
            assert "Прайси" in card and "01.11.2026" in card and "підставлено 2" in card
            assert c.get(f"/finance/pricing/price/{spid}/file").status_code == 200
            bad = c.post("/finance/pricing/price", data={"brand": "ПрайсБренд"}, files={"file": ("x.csv", b"a;b\n1;2\n", "text/csv")},
                         follow_redirects=False)
            assert "err=" in bad.headers["location"]
    finally:
        settings.files_dir = old


def test_price_rrp_other_currency():
    from app.services import supplier_prices as spx
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Артикул", "Назва", "Ціна закупівлі, EUR", "РРЦ, грн"])
    ws.append(["A1", "Крем", 10, 1000])
    b = io.BytesIO(); wb.save(b)
    r = spx.parse(b.getvalue(), "p.xlsx")
    assert r["currency"] == "EUR" and r["rrp_currency"] == "UAH" and r["rows"][0]["rrp"] == 1000


def test_pdf_price_via_agent(tmp_path):
    from app.security import new_api_key
    from app.models import ApiKey, InvoiceJob
    from app.services import invoice_ai, supplier_prices as spx
    settings.admin_password = "owner-pass-1"
    old, settings.files_dir = settings.files_dir, str(tmp_path)
    with SessionLocal() as db:
        db.add(Product(name="Агент Тонік 200 мл", name_norm="агент тонік 200 мл", brand="АгентБренд", sku="AT-200")); db.commit()
        invoice_ai.set_engine(db, "agent")
        raw, prefix, hsh = new_api_key()
        db.add(ApiKey(name="oc-price", prefix=prefix, key_hash=hsh, scopes=["invoices"])); db.commit()
    try:
        with TestClient(app) as c:
            c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
            r = c.post("/finance/pricing/price", data={"brand": "АгентБренд"}, files={"file": ("prais.pdf", b"%PDF-1.4 x", "application/pdf")},
                       follow_redirects=False)
            spid = int(r.headers["location"].rsplit("/", 1)[1])
            assert "розпізнає агент" in c.get(f"/finance/pricing/price/{spid}").text
            with SessionLocal() as db:
                jid = db.query(InvoiceJob).filter(InvoiceJob.kind == "price", InvoiceJob.ref_id == spid).one().id
            h = {"X-API-Key": raw}
            job = next(x for x in c.get("/api/v1/invoices/pending", headers=h).json()["invoices"] if x["id"] == jid)
            assert job["kind"] == "price" and "прайс-лист" in job["prompt"]
            res = c.post(f"/api/v1/invoices/{jid}/result", headers=h, json={"result": {"currency": "€", "rows": [
                {"name": "Тонік 200 мл", "sku": "AT-200", "cost": "8,40", "rrp": 18}, {"name": "Розділ", "cost": 0}]}}).json()
            assert res["rows"] == 1 and res["matched"] == 1
            with SessionLocal() as db:
                sp = db.get(SupplierPrice, spid)
                assert sp.status is None and sp.currency == "EUR" and sp.rows[0]["cost"] == 8.4
        # Claude-шлях (двигун CRM) — мок
        with SessionLocal() as db:
            sp2 = spx.create(db, b"\xff\xd8", "p.jpg", "АгентБренд", None, "USD", None, None,
                             claude=lambda d, m, p: {"currency": "EUR", "rows": [{"name": "Тонік", "sku": "at-200", "cost": 9}]})
            assert sp2.currency == "USD" and sp2.n_matched == 1 and sp2.status is None
    finally:
        settings.files_dir = old
        with SessionLocal() as db:
            invoice_ai.set_engine(db, "crm")
