import json

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, ProductFile, SiteOrder, SyncState, PayLink

HOME = '<html><head><title>Косметика | MOLODIST</title><meta name="description" content="Опис головної"></head><body><a href="/shop/oblichchya/">x</a><a href="/shop/krem-test-sf/">p</a></body></html>'
CAT = ('<html><head><title>Купити косметику для обличчя</title><meta name="description" content="SEO опис категорії"></head><body><main><h1>Догляд за обличчям</h1>'
       '<a href="/shop/krem-test-sf/900001/">a</a><a href="/shop/oblichchya/spf/">sub</a><a href="/shop/oblichchya/page-2/">2</a></main></body></html>')
PAGE = '<html><head><title>Доставка</title></head><body><header>h</header><div><h1>Доставка і оплата</h1><p>' + 'Текст умов. ' * 40 + '</p><script>x</script></div></body></html>'


class R:
    def __init__(self, text, code=200):
        self.text, self.status_code = text, code


class Http:
    def get(self, url, **k):
        p = url.replace("https://molodist.store", "")
        if p == "/":
            return R(HOME)
        if p == "/shop/oblichchya/":
            return R(CAT)
        if p == "/shop/oblichchya/page-2/":
            return R('<main><a href="/shop/serum-test-sf/">b</a></main>')
        if p == "/shop/oblichchya/spf/":
            return R('<title>SPF</title><main><h1>SPF</h1><a href="/shop/serum-test-sf/">b</a></main>')
        if p == "/contentsitemap.xml":
            return R("<loc>https://molodist.store/for-customers/dostavka-i-oplata/</loc><loc>https://molodist.store/shop/</loc>")
        if p == "/for-customers/dostavka-i-oplata/":
            return R(PAGE)
        return R("", 404)


def test_shop_front_crawl_catalog_order(monkeypatch, tmp_path):
    from app.services import shop_front as sf
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    monkeypatch.setattr("app.services.tg_notify.send_text", lambda *a, **k: True)
    settings.admin_password = "owner-pass-sf"
    with TestClient(app) as c:
        with SessionLocal() as db:
            a = Product(name="Крем тест SF", name_norm="крем тест sf", price=1950, sku="SF-1", site_on=True, brand="Ag Skin",
                        site_info={"slug": "krem-test-sf", "title": "Крем тест SF 50 мл", "price_old": 2300, "presence": 1, "quantity": 5,
                                   "short_description": "<p>Опис крему</p>", "seo_title": "Крем SEO title"})
            b = Product(name="Сироватка тест SF", name_norm="сироватка тест sf", price=900, site_on=True,
                        site_info={"slug": "serum-test-sf", "title": "Сироватка тест SF", "presence": 0})
            db.add_all([a, b]); db.flush()
            img = tmp_path / "a.png"
            from PIL import Image
            Image.new("RGB", (50, 50), "white").save(img)
            f = ProductFile(product_id=a.id, path=str(img), name="a.png", kind="photo", site=True, sort=0)
            db.add(f); db.commit()
            aid, fid = a.id, f.id
            rep = sf.crawl(db, http=Http())
            assert rep["cats"] == 2 and rep["pages"] == 1
            d = sf.catalog(db, fresh=True)
            cat = d["by_path"]["/shop/oblichchya/"]
            assert cat["slugs"] == ["krem-test-sf", "serum-test-sf"] and cat["title"] == "Купити косметику для обличчя"
            assert d["by_slug"]["krem-test-sf"]["pct"] == 15 and not d["by_slug"]["serum-test-sf"]["stock"]
            assert "<script" not in d["pages"]["/for-customers/dostavka-i-oplata/"]["html"]
        assert c.get("/_shop/").status_code == 404                                    # не опубліковано й не співробітник
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-sf"})
        h = c.get("/_shop/")
        assert h.status_code == 200 and "Крем тест SF" in h.text and "noindex" in h.text
        r = c.get("/_shop/shop/oblichchya/")
        assert "SEO опис категорії" in r.text and "<h1>Догляд за обличчям</h1>" in r.text and "Немає в наявності" in r.text
        p = c.get("/_shop/shop/krem-test-sf/")
        assert p.status_code == 200 and "Крем SEO title" in p.text and "Опис крему" in p.text and '"@type": "Product"' in p.text
        assert c.get("/_shop/shop/krem-test-sf/900001/", follow_redirects=False).status_code == 301
        assert c.get("/_shop/shop/nema-takoho/").status_code == 404
        assert "Текст умов" in c.get("/_shop/for-customers/dostavka-i-oplata/").text
        assert c.get(f"/_shop/img/{fid}?w=300").headers["content-type"] == "image/webp"
        assert "krem-test-sf" in c.get("/_shop/sitemap.xml").text and "Крем" in c.get("/_shop/shop/?q=крем").text
        assert c.get("/_shop/checkout/").status_code == 200 and c.get("/settings/shop").status_code == 200
        cart = c.post("/_shop/api/cart", json={"items": [{"id": aid, "qty": 2}, {"id": 999999, "qty": 1}]}).json()
        assert cart["total"] == 3900 and cart["free_delivery"] and len(cart["items"]) == 1
        bad = c.post("/_shop/api/order", json={"items": [{"id": aid, "qty": 1}], "name": "Оля Тест", "phone": "0671239901", "delivery": "np"})
        assert bad.status_code == 400
        o = c.post("/_shop/api/order", json={"items": [{"id": aid, "qty": 2}], "name": "Оля Тест", "phone": "0671239901", "delivery": "np",
                                             "city": "Київ", "warehouse": "Відділення №5", "pay": "cod"}).json()
        assert o["ok"] and o["total"] == 3900 and "done_url" in o and not o.get("pay_url")
        with SessionLocal() as db:
            so = db.query(SiteOrder).filter(SiteOrder.number == o["number"]).one()
            assert so.source == "crm" and so.patient_id and so.status == "Нове" and so.items[0]["qty"] == 2
            pl = PayLink(provider="mono", amount=3900, purpose="x", status="created", info={"shop_order": so.id})
            db.add(pl); db.commit()
            from app.services import paylinks as pls
            pls._mark(db, pl, "paid", 3900)
            assert db.get(SiteOrder, so.id).paid and db.get(SiteOrder, so.id).status == "Оплачено"
            oid = so.id
        assert c.post(f"/shop-orders/{oid}/status", data={"status": "Відправлено", "ttn": "20450000000000", "paid": "1"}, follow_redirects=False).status_code == 303
        assert "20450000000000" in c.get(f"/shop-orders/{oid}").text
