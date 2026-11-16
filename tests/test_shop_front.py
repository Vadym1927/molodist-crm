import json
from datetime import datetime, timedelta
from sqlalchemy import select

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


def test_mono_parts_flow(monkeypatch):
    import json as _j
    from app.services import mono_parts as mp, shop_front as sf
    monkeypatch.setattr("app.services.tg_notify.send_text", lambda *a, **k: True)
    calls = []

    class H:
        def post(self, url, content=None, headers=None, **k):
            calls.append((url, _j.loads(content), headers))
            body = {"order_id": "PCH-1"} if url.endswith("/create") else {"state": "IN_PROCESS", "order_sub_state": "WAITING_FOR_STORE_CONFIRM"} if url.endswith("/state") else {}
            return type("R", (), {"status_code": 200, "content": b"x", "json": lambda s: body})()
    with SessionLocal() as db:
        mp.save(db, "store-1", "key-1", "test", "3, 4, 99", True, 500)
        p = mp.public(db)
        assert p["ready"] and p["parts"] == [3, 4] and "key-1" not in db.get(SyncState, mp.KEY).value
        o = SiteOrder(source="crm", ext_id="M1", number="M1", status="Очікує оплати", total=3900, phone="+380671112233", created_at=__import__("datetime").datetime.now(),
                      items=[{"name": "Крем", "qty": 2, "price": 1950, "total": 3900}])
        db.add(o); db.commit()
        assert mp.create(db, o, 4, "https://crm/cb", H()) == "PCH-1"
        url, body, hd = calls[0]
        assert url.startswith("https://u2-demo-ext") and body["available_programs"][0]["available_parts_count"] == [4] and hd["store-id"] == "store-1"
        assert hd["signature"] == mp.sign(_j.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(), "key-1")
        raw = _j.dumps({"order_id": "PCH-1", "state": "IN_PROCESS", "order_sub_state": "WAITING_FOR_STORE_CONFIRM"}).encode()
        assert not mp.callback(db, raw, "bad") and mp.callback(db, raw, mp.sign(raw, "key-1"))
        db.refresh(o)
        assert o.paid and o.status == "Оплачено"
        assert mp.confirm(db, o, H()) and not mp.confirm(db, o, H()) and calls[-1][0].endswith("/api/order/confirm")
        mp.save(db, "", "", "test", "3 4", False, 500)
        assert not mp.public(db)["ready"] and mp.public(db)["has_keys"]


def test_shop_admin_sections_bulk_import(monkeypatch):
    from app.services import shop_front as sf
    monkeypatch.setattr("app.services.tg_notify.send_text", lambda *a, **k: True)
    sec = sf.sections('<div style="x"><div>Опис</div><div><p><span style="color:red">Гель очищує.</span></p></div><div><p></p></div>'
                      '<h3>АКТИВНІ ІНГРЕДІЄНТИ</h3><p>— Кислота</p><p><strong>Спосіб застосування</strong></p><p>Нанести.</p></div>')
    assert [x["title"] for x in sec] == ["Опис", "Активні інгредієнти", "Спосіб застосування"] and "style" not in sec[0]["html"] and "Гель очищує" in sec[0]["html"]
    settings.admin_password = "owner-pass-sa"
    with TestClient(app) as c:
        with SessionLocal() as db:
            a = Product(name="Bulk A", name_norm="bulk a", price=2000, site_on=True, site_info={"slug": "bulk-a", "title": "Bulk A", "presence": 1})
            b = Product(name="Bulk B", name_norm="bulk b", price=1000, site_on=False, site_info={"slug": "bulk-b", "title": "Bulk B", "presence": 1})
            db.add_all([a, b]); db.commit()
            ia, ib = a.id, b.id
            k = sf.preview_key(db)
        bad = c.post("/api/public/shop-import", json={"k": "x", "cats": []})
        assert bad.status_code == 403
        r = c.post("/api/public/shop-import", json={"k": k, "cats": [{"path": "/shop/nabory/", "h1": "Набори", "title": "Набори T", "in_menu": True, "slugs": ["bulk-a", "nope"]},
                                                                 {"path": "/shop/bulk-a/", "slugs": []}],
                                                    "pages": [{"path": "/blog/x/", "h1": "Стаття", "html": "<p>t</p><script>1</script>"}]})
        assert r.json()["cats"] == 1 and r.json()["products_in_cats"] == 1
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-sa"})
        assert "Bulk A" in c.get("/settings/shop/products").text and c.get("/settings/shop/cats").status_code == 200
        c.post("/settings/shop/products", data={"id": [ia, ib], "action": "sale", "pct": "20"}, follow_redirects=False)
        c.post("/settings/shop/products", data={"id": [ib], "action": "show"}, follow_redirects=False)
        c.post("/settings/shop/products", data={"id": [ib], "action": "cat_add", "bcat": "/shop/nabory/"}, follow_redirects=False)
        c.post("/settings/shop/cats", data={"path": "/shop/nabory/", "name": "Набори догляду", "title": "T2", "in_menu": "1", "sort": "5"}, follow_redirects=False)
        with SessionLocal() as db:
            d = sf.catalog(db, fresh=True)
            assert d["by_slug"]["bulk-a"]["price"] == 1600 and d["by_slug"]["bulk-a"]["old"] == 2000 and d["by_slug"]["bulk-a"]["pct"] == 20
            cat = d["by_path"]["/shop/nabory/"]
            assert cat["name"] == "Набори догляду" and set(cat["slugs"]) == {"bulk-a", "bulk-b"} and d["menu"][0]["path"] == "/shop/nabory/"
        c.post(f"/products/{ia}/site", data={"site_on": "1", "short_description": "<p>x</p>", "sale_pct": "", "cats_form": "1"}, follow_redirects=False)
        with SessionLocal() as db:
            d = sf.catalog(db, fresh=True)
            assert d["by_slug"]["bulk-a"]["price"] == 2000 and "bulk-a" not in d["by_path"]["/shop/nabory/"]["slugs"]
        assert "Набори догляду" in c.get(f"/products/{ib}").text


def test_shop_parts_label_and_legal():
    from app.services import shop_front as sf, mono_parts as mp
    settings.admin_password = "owner-pass-pl"
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    with TestClient(app) as c:
        with SessionLocal() as db:
            mp.save(db, "store-x", "key-x", "test", "3 4", True, 3000)
            db.add(Product(name="Label A", name_norm="label a", price=3500, site_on=True, site_info={"slug": "label-a", "title": "Label A", "presence": 1}))
            db.commit()
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-pl"})
        r = c.post("/settings/shop", data={"on": "1"}, files={"parts_upload": ("l.png", png, "image/png"), "paw_upload": ("p.png", png, "image/png")}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            name = sf.cfg(db)["parts_file"]
            sf.catalog(db, fresh=True)
        assert name.startswith("parts-label-") and c.get(f"/_shop/brand/{name}").content == png
        assert c.get("/_shop/brand/..%2Fx.png").status_code == 404
        h = c.get("/_shop/shop/label-a/").text
        assert "АТ «УНІВЕРСАЛ БАНК» ліцензія НБУ No92 від 20.01.1994, номер у держреєстрі банків No 226." in h
        assert "Реальна річна процентна ставка: 0,000001%" in h and f"/brand/{name}" in h and "chast.monobank.ua" in h
        with SessionLocal() as db:
            paw = sf.cfg(db)["parts_paw_file"]
        assert paw.startswith("parts-paw-") and f'class="pch-paw" src="/_shop/brand/{paw}"' in c.get("/_shop/shop/").text
        bad = c.post("/settings/shop", data={"on": "1"}, files={"parts_upload": ("l.exe", b"MZ", "application/octet-stream")}, follow_redirects=False)
        assert "err" in bad.headers["location"]


def test_shop_order_flow_notifications(monkeypatch):
    from app.services import shop_front as sf, notifications as nt
    from app.models import SiteOrder, User
    sent = []
    monkeypatch.setattr(sf, "_tg_send", lambda chat, text, reply_to=None: (sent.append((chat, text, reply_to)) or True, 700 + len(sent)))
    monkeypatch.setattr(sf, "order_chats", lambda db: ["-100leads", "111"])
    settings.admin_password = "owner-pass-of"
    with TestClient(app) as c:
        with SessionLocal() as db:
            o = SiteOrder(source="crm", ext_id="T1", number="T1", created_at=datetime.now(), status="Очікує оплати", paid=False, total=5470, name="Тест Покупець",
                          phone="380500000001", payment="Онлайн карткою (monobank)", delivery="Нова пошта", items=[{"name": "Serum", "qty": 1, "price": 5470, "total": 5470}])
            db.add(o); db.commit()
            sf.notify(db, o); db.commit()
            oid = o.id
            assert len(sent) == 2 and "не підтверджена" in sent[0][1] and o.raw["tg"] == [["-100leads", 701], ["111", 702]]

            class PL:
                info = {"shop_order": oid}
            sf.pay_event(db, PL(), "expired"); db.commit()
            assert sent[2][2] == 701 and "Не оплачено" in sent[2][1] and "прострочено" in sent[2][1] and len(sent) == 4
            sf.pay_event(db, PL(), "expired")
            assert len(sent) == 4
            u = db.scalars(select(User)).first()
            kinds = {x["kind"] for x in nt.shop(db, datetime.utcnow() - timedelta(days=1))}
            assert {"shop", "shopfail"} <= kinds
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-of"})
        r = c.post(f"/shop-orders/{oid}/status", data={"status": "Відправлено", "ttn": "20450000000000", "paid": "1", "resp": "1"}, follow_redirects=False)
        assert r.status_code == 303
        page = c.get(f"/shop-orders/{oid}").text
        assert "Статус: Очікує оплати → Відправлено" in page and "20450000000000" in page and "Історія" in page
        lst = c.get("/shop-orders").text
        assert "Опрацьовується" in lst and "Повернення" in lst and "Відповідальний" in lst
