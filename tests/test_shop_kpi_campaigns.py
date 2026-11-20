from app.services.shop_kpi import is_shop_campaign


def test_shop_campaign_filter():
    assert is_shop_campaign("MOLODIST Store — PMax товари")
    assert not is_shop_campaign("MOLODIST — PMax відвідування (карта)")
    assert not is_shop_campaign("MOLODIST | Пошук | Загальні → головна")
