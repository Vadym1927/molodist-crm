from datetime import date

from app.models import SessionLocal, Patient, Product, SiteOrder


class C:
    root = "https://molodist.store/"
    how = "body"


def fake_export(client, path, key, body=None, page=200):
    if key == "orders":
        yield {"order_id": 77001, "payed": 1, "order_number": "59961", "user": 14410, "comment": None, "total_sum": 3900.0, "total_default": 4100.0,
               "stat_created": "03.04.2026 10:31:04", "delivery_name": "Олена Магазинна", "delivery_email": "o@x.ua", "delivery_phone": "+380671234500",
               "delivery_city": "Київ", "delivery_address": ", Відділення №5", "delivery_price": 0,
               "delivery_type": {"id": 89, "value": {"uk": "Нова Пошта"}}, "product": [{"id": 88001, "parent_id": 1, "quantity": 2, "sku": "SO-1", "price": 1950, "total_price": 3900}],
               "stat_status": {"id": 1034, "value": {"uk": "Оброблені"}}, "payment_type": {"id": 1, "value": {"uk": "Онлайн"}}, "order_additional": []}
        yield {"order_id": 77002, "payed": 0, "order_number": "59962", "total_sum": 500, "stat_created": "05.04.2026 12:00:00",
               "delivery_name": "Олена Магазинна", "delivery_phone": "0671234500", "product": [],
               "stat_status": {"id": 1036, "value": {"uk": "Скасовані"}}}
    else:
        yield {"id": 1, "title": {"uk": "X"}}


def test_shop_orders_sync_and_report(monkeypatch, tmp_path):
    from app.config import settings
    from app.integrations import shop_express as sx
    from app.services import shop_orders as so, reports_shop as rs
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    monkeypatch.setattr(sx, "export", fake_export)
    with SessionLocal() as db:
        p = Product(name="Крем замовлення", name_norm="крем замовлення", shop_id="88001", sku="SO-1")
        db.add(p); db.commit()
        rep = so.sync(db, client=C(), full=True)
        assert rep["new"] == 2 and rep["patients_new"] == 1 and rep["items_matched"] == 1 and rep["categories"] == 1
        o = db.query(SiteOrder).filter(SiteOrder.ext_id == "77001").one()
        assert o.paid and o.status == "Оброблені" and o.delivery == "Нова Пошта" and o.items[0]["product_id"] == p.id
        pat = db.get(Patient, o.patient_id)
        assert pat.phone.endswith("671234500") and db.query(SiteOrder).filter(SiteOrder.patient_id == pat.id).count() == 2
        rep2 = so.sync(db, client=C(), full=False)
        assert rep2["new"] == 0 and rep2["updated"] == 2
        r = rs.shop(db, date(2026, 4, 1), date(2026, 4, 30))
        assert r.tiles[0][1] == 3900 and r.tiles[3][1] == 1 and r.rows[0]["n"] == 1
        assert rs.shop(db, date(2026, 4, 1), date(2026, 4, 30), "products").rows[0]["qty"] == 2
        assert list(tmp_path.joinpath("shop_express").glob("orders-*.json"))


def test_shop_orders_pages():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    settings.admin_password = "owner-pass-so"
    with TestClient(app) as c:
        with SessionLocal() as db:
            pat = Patient(full_name="Покупчиня Сторінка", phone="+380671234599")
            db.add(pat); db.flush()
            o = SiteOrder(source="shop_express", ext_id="99001", number="1", status="Оплачені", total=100, patient_id=pat.id,
                          items=[{"name": "Крем", "qty": 1, "price": 100, "total": 100}], extra=[{"name": "Пакування", "price": 20, "on": True}])
            db.add(o); db.commit(); oid, pid = o.id, pat.id
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-so"})
        assert "Замовлення сайту" in c.get("/shop-orders").text
        assert "Пакування" in c.get(f"/shop-orders/{oid}").text
        assert "Замовлення з сайту" in c.get(f"/patients/{pid}").text
        assert c.get("/reports?r=shop").status_code == 200
