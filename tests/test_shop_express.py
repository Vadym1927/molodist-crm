"""molodist.store (Shop-Express): посилання, фото й ціна в картки товарів — без реальних запитів."""
import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product
from app.integrations import shop_express as sx


def _transport(request: httpx.Request):
    if request.url.path == "/api/auth/":
        return httpx.Response(200, json={"status": "OK", "response": {"token": "tok-1"}})
    if request.url.path == "/api/catalog/export/":
        assert request.url.params["token"] == "tok-1"
        return httpx.Response(200, json={"status": "OK", "response": {"products": [
            {"id": 501, "article": "SX-1", "title": {"ua": "Сироватка сайтова 30 мл"}, "url": "/ua/catalog/serum-sx1/",
             "images": [{"url": "/content/images/sx1.jpg"}], "price": "1990.00"},
            {"id": 502, "article": "", "title": "Невідомий на сайті", "url": "/x/"}]}})
    return httpx.Response(404)


def test_shop_express_sync():
    with SessionLocal() as db:
        p = Product(name="Сироватка сайтова 30 мл", name_norm="сироватка сайтова 30 мл", sku="SX-1")
        q = Product(name="Свій товар", name_norm="свій товар", url="https://other.example/x")
        db.add_all([p, q]); db.commit()
        cli = sx.ShopExpress("https://molodist.store/eco/ukr/api/users", "api@molodist", "pw",
                             http=httpx.Client(transport=httpx.MockTransport(_transport)))
        rep = sx.sync(db, client=cli)
        db.refresh(p)
        assert rep["rows"] == 2 and rep["matched"] == 1 and rep["unmatched"] == ["— · Невідомий на сайті"]
        assert p.url == "https://molodist.store/ua/catalog/serum-sx1/" and p.photo_url == "https://molodist.store/content/images/sx1.jpg"
        assert p.shop_price == 1990 and p.shop_id == "501"
        sx.save_creds(db, "https://molodist.store/eco/ukr/api/users", "api@molodist", "secret-Пароль-42")
        raw = db.get(__import__("app.models", fromlist=["x"]).SyncState, "shop_express_enc").value
        assert "secret-Пароль-42" not in raw and "api@molodist" not in raw and sx.creds(db)["login"] == "api@molodist"
        pid = p.id
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        assert "sx1.jpg" in c.get(f"/products/{pid}").text
        assert "molodist.store (Shop-Express)" in c.get("/settings/keys").text



def test_token_in_body_is_found():
    def tr(request: httpx.Request):
        import json as _j
        if request.url.path == "/api/auth/":
            return httpx.Response(200, json={"status": "OK", "response": {"token": "t2"}})
        body = _j.loads(request.content or b"{}")
        if body.get("token") != "t2":
            return httpx.Response(403, json={"status": "UNAUTHORIZED", "response": {"message": "Token is invalid"}})
        return httpx.Response(200, json={"status": "OK", "response": {"products": [{"id": 1, "article": "Z"}]}})
    cli = sx.ShopExpress("https://molodist.store/", "a", "b", http=httpx.Client(transport=httpx.MockTransport(tr)))
    assert [r["article"] for r in cli.products()] == ["Z"] and cli.how == "body"
