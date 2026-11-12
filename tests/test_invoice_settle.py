"""Часткове закриття боргу поставки за актом звірки (оплачено раніше / різниця ціни)."""
from datetime import datetime

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, SupplierInvoice


def test_settle_partial():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        db.query(SupplierInvoice).filter_by(cb_number="T-99559").delete()
        db.add(SupplierInvoice(cb_number="T-99559", supplier="TEBISKIN", inv_date=datetime(2025, 8, 27), amount=16149.4, paid=12454.4))
        db.commit()
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        c.post("/stock/invoices/settle", data={"number": "T-99559", "amount": "1363", "kind": "paid", "note": "UVSOOTH оплачено"})
        c.post("/stock/invoices/settle", data={"number": "T-99559", "amount": "76", "kind": "price"})
        c.post("/stock/invoices/settle", data={"number": "T-99559", "amount": "999999", "kind": "paid"})
    with SessionLocal() as db:
        x = db.query(SupplierInvoice).filter_by(cb_number="T-99559").one()
        assert x.crm_paid == 1363 + 2256 and x.adjust == 76 and x.debt == 0
