from types import SimpleNamespace as N

from app.services.catalog import product_match


def P(name, sku=None, brand=None, barcode=None):
    return N(name=name, sku=sku, brand=brand, barcode=barcode, short_name=None)


def test_product_match_prefix_and_sku():
    p = P("ZO Гідрейтін Клінсер гель для вмивання 200мл", "965000", "ZO Skin Health", "4820000000017")
    assert product_match(p, "гід") is not None and product_match(p, "клін гель") is not None
    assert product_match(p, "гі") is None                       # текст — від 3 символів
    assert product_match(p, "96") == 1 and product_match(p, "965000") == 0 and product_match(p, "50") == 2
    assert product_match(p, "крем") is None
    assert product_match(p, "рейт") is not None                 # частина слова від 4 літер
