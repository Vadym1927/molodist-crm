from types import SimpleNamespace as NS

from app.services import shop_front as sf


def test_ga_payload():
    o = NS(number="M1", total=3588.0, delivery_price=0, items=[{"product_id": 52009, "name": "Набір", "price": 3588, "qty": 1}],
           raw={"ga": {"cid": "123456.1700000000", "sid": "1763730000"}})
    b = sf.ga_payload(o)
    assert b["client_id"] == "123456.1700000000"
    ev = b["events"][0]
    assert ev["name"] == "purchase" and ev["params"]["transaction_id"] == "M1" and ev["params"]["value"] == 3588.0
    assert ev["params"]["session_id"] == "1763730000" and ev["params"]["items"][0]["item_id"] == "52009"
    assert sf.ga_payload(NS(number="M2", total=1, delivery_price=0, items=[], raw={})) is None
