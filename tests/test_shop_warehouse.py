"""Склад «Онлайн-магазин»: є в довіднику складів і сайт продає з нього."""
from app.services import catalog, shop_front, price_monitor


def test_shop_warehouse_registered():
    assert catalog.WAREHOUSES["shop"] == "Онлайн-магазин"
    assert "shop" in shop_front.SHOP_WHS and "vitrina" in shop_front.SHOP_WHS
    assert "shop" in price_monitor.WAREHOUSES


def test_shop_warehouse_not_mapped_from_cleverbox():
    # CleverBox про цей склад не знає — категорія «магазин» і далі веде на вітрину
    assert catalog.warehouse_of("Магазин") == "vitrina"
