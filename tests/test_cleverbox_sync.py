"""Синк з CleverBox на замоканих відповідях у форматі зі Swagger v3."""
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not (Path(__file__).parent / "sample_plan.pdf").exists(),
                                reason="покладіть будь-який PDF бюті-плану Observ у tests/sample_plan.pdf")

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["CLEVERBOX_TOKEN"] = "test"
os.environ["CLEVERBOX_MIN_INTERVAL_SEC"] = "0"

import httpx  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import init_db, SessionLocal  # noqa: E402
from app.parsers.observ import parse_observ_pdf  # noqa: E402
from app.services.plans import import_plan  # noqa: E402
from app.services.kpi import compute_plan  # noqa: E402
from app.integrations import cleverbox as cbm  # noqa: E402

settings.cleverbox_min_interval_sec = 0
settings.cleverbox_token = "test"
FUT = (datetime.now() + timedelta(days=5)).strftime("%d.%m.%Y 11:00")

RESP = {
    "services": {"ok": True, "offset": 0, "limit": 100, "data": [
        {"id": 11, "name": "Гігієна обличчя комбінована", "category_name": "Догляд", "price": 2800},
        {"id": 12, "name": "IPL обличчя", "category_name": "Апарат", "price": 3900}]},
    "staff": {"ok": True, "data": [{"id": 5, "name": "Дарʼя", "lname": "Колган", "email": "", "img": ""}]},
    "clients": {"ok": True, "offset": 0, "limit": 100, "data": [
        {"id": 777, "name": "Даниленко Олена", "phone": "0671112233"}]},
    "services/sales": {"ok": True, "offset": 0, "limit": 100, "records": 2, "data": [
        {"service_id": 11, "service_name": "Гігієна обличчя комбінована", "date": "13.10.2026 12:00", "quantity": 1,
         "user_name": "Дарʼя Колган", "client_id": 777, "sale_id": 1, "check_id": 1},
        {"service_id": 12, "service_name": "IPL обличчя", "date": "15.10.2026 12:00", "quantity": 1,
         "user_name": "Дарʼя Колган", "client_id": 999, "sale_id": 2, "check_id": 2}]},
    "products/sales": {"ok": True, "offset": 0, "limit": 100, "records": 1, "data": [
        {"date": "10.10.2026 18:00:00", "product_id": 3, "product_name": "AG SKIN Azelorosa Rosy Cream Gel 50 мл",
         "quantity": 1, "total_amount": 1700, "discount": 0, "check_id": "55", "client_id": 777}]},
    "clients/visits": {"ok": True, "offset": 0, "limit": 100, "data": [
        {"id": 900, "date_start": FUT, "status": 1, "_status": "Записаний", "total": 3900, "client_id": 777,
         "services": [{"id": 12, "master": "Дарʼя Колган", "master_id": 5}]},
        {"id": 901, "date_start": FUT, "status": 5, "_status": "Скасовано", "client_id": 777,
         "services": [{"id": 12}]}]},
    "products/list": {"ok": True, "offset": 0, "limit": 100, "rows": 2, "data": [
        {"item_id": 3, "name": "Крем-гель AG SKIN Azelorosa Rosy Cream Gel 50 мл", "sale_price": 1700, "cost": 900,
         "category_name": "Догляд", "for_sale": True},
        {"item_id": 4, "name": "Голки 30G", "for_sale": False, "for_shop": False, "for_work": True}]},
    "products/stock": {"ok": True, "offset": 0, "limit": 100, "rows": 1, "data": [
        {"product_id": 3, "product_name": "Крем-гель AG SKIN Azelorosa Rosy Cream Gel 50 мл", "quantity": 2}]},
    "sales/checks": {"offset": 0, "limit": 100, "total": 2, "data": [
        {"check_id": 55, "date": "10.10.2026 18:00:00", "amount": 1700, "discount": 0, "client_id": 777,
         "author_name": "Адмін", "status": "payd", "items": [{"item_type": "product", "item_name": "Azelorosa",
         "payd_amount": 1700, "initiator_name": "Дарʼя Колган", "quantity": 1}],
         "payments": [{"payment_name": "Картка", "amount": 1700}]},
        {"check_id": 56, "date": "13.10.2026 12:30:00", "amount": 2800, "discount": 0, "client_id": 777,
         "status": "payd", "items": [{"item_type": "service", "item_name": "Гігієна", "payd_amount": 2800,
         "initiator_name": "Дарʼя Колган", "quantity": 1}], "payments": [{"payment_name": "Готівка", "amount": 2800}]}]},
    "visits": {"ok": True, "offset": 0, "limit": 100, "total": 1, "data": [
        {"id": 900, "date_start": FUT, "status": 1, "_status": "Записаний", "total": 3900, "client_id": 777,
         "client": {"id": 777, "name": "Даниленко Олена"}, "services": [{"id": 12, "master": "Дарʼя Колган"}]},
        {"id": 950, "date_start": FUT, "status": 1, "_status": "Записаний", "total": 1200, "client_id": 888,
         "client": {"id": 888, "name": "Нова Клієнтка", "phone": "0991112233"}, "services": [{"id": 11}]}]},
}


def handler(req: httpx.Request):
    assert req.headers["token"] == "test"
    method = req.url.path.split("/api/v3/rest/")[1]
    return httpx.Response(200, json=RESP[method])


def test_full_sync(monkeypatch):
    init_db()
    db = SessionLocal()
    plan = import_plan(db, parse_observ_pdf(Path(__file__).parent / "sample_plan.pdf"), "x.pdf")
    db.commit()
    orig = cbm.CleverBox.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.http = httpx.Client(transport=httpx.MockTransport(handler), headers={"token": "test"})
    monkeypatch.setattr(cbm.CleverBox, "__init__", init)

    rep = cbm.full_sync(db)
    assert rep["patients"]["linked"] == 1 and rep["staff"] == 1
    db.refresh(plan.patient)
    assert plan.patient.cleverbox_id == 777 and plan.patient.phone == "0671112233"
    ps = compute_plan(db, plan)
    assert ps.proc_done == 1            # гігієна (IPL іншого клієнта не рахується)
    assert ps.proc_booked == 1          # майбутній IPL; скасований візит відкинуто
    assert ps.prod_bought == 1          # Azelorosa
    assert ps.realized_sum == 2800 + 1700
    assert ps.is_booked
    from app.models import Product, Check, StockLevel, Patient
    assert db.query(Check).count() == 2 and db.query(StockLevel).count() == 1
    prod = db.query(Product).filter(Product.cb_product_id == 3).one()
    azel = next(i for i in plan.items if "Azelorosa" in i.name)
    assert azel.product_id == prod.id and db.query(Product).count() == 5      # 4 засоби з плану + 1 з CleverBox (злиття)
    assert db.query(Patient).filter(Patient.cleverbox_id == 888).count() == 1   # клієнт із календаря


def test_rate_limit_retry(monkeypatch):
    """«no more than 1 request per second» → пауза і повтор, а не помилка синку."""
    import app.integrations.cleverbox as cbm
    from app.config import settings
    monkeypatch.setattr(settings, "cleverbox_min_interval_sec", 0)
    monkeypatch.setattr(cbm.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={"ok": False, "error": "no more than 1 request per second"})
        return httpx.Response(200, json={"ok": True, "data": [{"id": 1}]})
    cb = cbm.CleverBox(token="test")
    cb.http = httpx.Client(transport=httpx.MockTransport(h))
    assert cb.filials() == [{"id": 1}] and calls["n"] == 3
