import json

import httpx

from app.models import SessionLocal
from app.services import paylinks as pls


def test_mono_invoice_returns_buyer_to_thanks_page():
    """Після оплати monobank повертає покупця на «Дякуємо» — там спрацьовує purchase для Google Ads."""
    seen = []

    def h(req: httpx.Request):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"invoiceId": "invR", "pageUrl": "https://pay.mbnk.biz/invR"})
    http = httpx.Client(transport=httpx.MockTransport(h))
    with SessionLocal() as db:
        pls.save(db, "mono", {"token": "MTOK"}, 24, "", "Онлайн-оплати")
        pls.create(db, 500, "Замовлення 1", "https://crm.molodist.com.ua", http=http,
                   redirect_url="https://molodist.store/checkout/done/?n=S-1")
        pls.create(db, 500, "Передоплата", "https://crm.molodist.com.ua", http=http)
    assert seen[0]["redirectUrl"] == "https://molodist.store/checkout/done/?n=S-1"
    assert "redirectUrl" not in seen[1]
