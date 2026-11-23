from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from app.services import shop_feeds as fd


def _o(**k):
    d = dict(id=1, source="crm", status="Оплачено", paid=True, total=3588.0, number="M261121-4859",
             created_at=datetime(2026, 11, 21, 15, 5), raw={"utm": {"gclid": "Cj0KCQabc-123_x"}, "paid_at": "2026-11-21T13:05:40"})
    d.update(k)
    return NS(**d)


def test_rows_and_csv():
    now = datetime(2026, 11, 23, 12, 0)
    rows = fd.ads_offline_rows([
        _o(),
        _o(number="A", paid=False, status="Очікує оплати"),                 # не оплачено
        _o(number="B", status="Скасовано"),                                 # скасовано
        _o(number="C", raw={"utm": {}}),                                    # без кліку
        _o(number="D", raw={"utm": {"gclid": "0AAAAxyz", "gbraid": "0AAAAxyz"}}),   # gbraid — не gclid
        _o(number="E", created_at=now - timedelta(days=120)),               # старше 90 днів
        _o(number="F", source="shop_express"),
    ], now=now)
    assert [r["order"] for r in rows] == ["M261121-4859"]
    assert rows[0]["time"] == datetime(2026, 11, 21, 15, 5, 40)
    csv = fd.ads_offline_csv(rows, "Покупка molodist.store (CRM)")
    assert csv.splitlines()[0] == "Parameters:TimeZone=Europe/Kiev"
    assert "Cj0KCQabc-123_x,Покупка molodist.store (CRM),2026-11-21 15:05:40,3588.00,UAH,M261121-4859" in csv
