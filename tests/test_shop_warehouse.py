"""Склад «Онлайн-магазин»: є в довіднику складів і сайт продає з нього."""
from app.services import catalog, shop_front, price_monitor


def test_shop_warehouse_registered():
    assert catalog.WAREHOUSES["shop"] == "Онлайн-магазин"
    assert "shop" in shop_front.SHOP_WHS and "vitrina" in shop_front.SHOP_WHS
    assert "shop" in price_monitor.WAREHOUSES


def test_shop_warehouse_not_mapped_from_cleverbox():
    # CleverBox про цей склад не знає — категорія «магазин» і далі веде на вітрину
    assert catalog.warehouse_of("Магазин") == "vitrina"


def test_custom_warehouse_settings(client_db=None):
    from app.models import SessionLocal, init_db
    from app.services import warehouses as whs, catalog, shop_front
    init_db()
    with SessionLocal() as db:
        assert whs.save(db, None, "Під реалізацію", True) is None
        key = next(x["key"] for x in whs.items(db) if x["name"] == "Під реалізацію")
        assert catalog.WAREHOUSES[key] == "Під реалізацію" and key in shop_front.SHOP_WHS
        assert whs.save(db, None, "під реалізацію", False)           # дубль назви
        assert whs.save(db, "vitrina", "Вітрина клініки", True) is None
        assert catalog.WAREHOUSES["vitrina"] == "Вітрина клініки"
        assert whs.delete(db, "vitrina")                               # вбудований не видаляється
        assert whs.delete(db, key) is None and key not in catalog.WAREHOUSES and key not in shop_front.SHOP_WHS
        assert whs.save(db, "vitrina", "Вітрина", True) is None
        assert catalog.WAREHOUSES["vitrina"] == "Вітрина"


def test_warehouses_settings_page():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        r = c.post("/login", data={"login": settings.admin_login if hasattr(settings, "admin_login") else "vadim", "password": "owner-pass-1"}, follow_redirects=False)
        assert r.status_code == 303
        r = c.get("/settings/warehouses")
        assert r.status_code == 200 and "Онлайн-магазин" in r.text and "Додати склад" in r.text
        r = c.post("/settings/warehouses", data={"name": "Тест-склад", "shop": "1"}, follow_redirects=False)
        assert r.status_code == 303
        r = c.get("/settings/warehouses")
        assert "Тест-склад" in r.text
        r = c.get("/products?tab=care&wh=all")
        assert r.status_code == 200 and "Тест-склад" in r.text
        from app.models import SessionLocal
        from app.services import warehouses as whs
        with SessionLocal() as db:
            key = next(x["key"] for x in whs.items(db) if x["name"] == "Тест-склад")
        r = c.post(f"/settings/warehouses/{key}/delete", follow_redirects=False)
        assert r.status_code == 303
        assert "Тест-склад" not in c.get("/settings/warehouses").text
        assert "/settings/warehouses" in c.get("/settings").text
