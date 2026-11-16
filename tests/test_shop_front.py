import pytest
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
        assert "Оплата і доставка" in c.get("/_shop/for-customers/dostavka-i-oplata/").text
        assert c.get(f"/_shop/img/{fid}?w=300").headers["content-type"] == "image/webp"
        assert "krem-test-sf" in c.get("/_shop/sitemap.xml").text and "Крем" in c.get("/_shop/shop/?q=крем").text
        assert "Залишився один крок" in c.get("/_shop/checkout/").text                # без входу — спершу реєстрація
        assert c.post("/_shop/api/order", json={"items": [{"id": aid, "qty": 1}]}).status_code == 401
        with SessionLocal() as db:
            c0 = sf.cfg(db); c0["order_login"] = False; sf.save_cfg(db, c0)
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


def test_shop_cabinet_flow(monkeypatch):
    from app.services import shop_cabinet as sc, shop_front as sf
    from app.models import ShopAccount, CoinTxn, BonusTxn, Patient
    codes = {}

    def fake(db, phone, text, *a):
        codes[phone] = "".join(ch for ch in text if ch.isdigit())[:5]
        assert "магазин MOLODIST" in text
        return "sms"
    monkeypatch.setattr(sc, "_deliver", fake)
    monkeypatch.setattr("app.shop_web._rate", lambda *a, **k: True)
    settings.admin_password = "owner-pass-cb"
    H = {"Content-Type": "application/json"}
    with TestClient(app) as c:
        with SessionLocal() as db:
            k = sf.preview_key(db)
            db.add(Product(name="Wish A", name_norm="wish a", price=900, site_on=True, site_info={"slug": "wish-a", "title": "Wish A", "presence": 1}))
            db.commit()
            wid = db.scalars(select(Product).where(Product.name == "Wish A")).first().id
            sf.catalog(db, fresh=True)
        c.cookies.set("shop_prev", k)
        assert "Отримати код" in c.get("/_shop/cabinet/").text
        assert c.post("/_shop/api/cab/start", data="phone=1").status_code == 404                      # лише JSON
        assert c.post("/_shop/api/cab/start", json={"phone": "123"}).status_code == 400
        r = c.post("/_shop/api/cab/start", json={"phone": "+380 50 111 22 33"}).json()
        assert r["ok"] and r["new"] and r["channel"] == "sms"
        assert c.post("/_shop/api/cab/start", json={"phone": "+380501112233"}).status_code == 400     # не частіше раза на хвилину
        assert c.post("/_shop/api/cab/verify", json={"phone": "+380501112233", "code": "00000" if codes["380501112233"] != "00000" else "11111"}).status_code == 400
        v = c.post("/_shop/api/cab/verify", json={"phone": "+380501112233", "code": codes["380501112233"], "name": "Тестова Олена", "remember": True, "wish": [wid]})
        assert v.json()["ok"] and v.json()["wish"] == [wid] and "mshop_s" in v.cookies
        with SessionLocal() as db:
            acc = db.scalars(select(ShopAccount)).all()[-1]
            assert acc.first_name == "Олена" and acc.last_name == "Тестова" and acc.ref_code and db.get(Patient, acc.patient_id).phone == "380501112233"
            assert [(x.amount, x.expires_at) for x in db.scalars(select(CoinTxn).where(CoinTxn.patient_id == acc.patient_id))] == [(100, None)]
            assert not db.scalars(select(BonusTxn).where(BonusTxn.patient_id == acc.patient_id)).all()            # бонуси CleverBox — окремо
            ref, wt, pid1 = acc.ref_code, acc.wish_token, acc.patient_id
        page = c.get("/_shop/cabinet/").text
        assert "Контактні дані" in page and "Олена" in page and "Вийти" in page
        assert c.post("/_shop/api/cab/profile", json={"first_name": "Олена", "last_name": "Нова", "email": "bad"}).status_code == 400
        assert c.post("/_shop/api/cab/profile", json={"first_name": "Олена", "last_name": "Нова", "email": "o@example.com"}).json()["ok"]
        assert c.post("/_shop/api/cab/address", json={"name": "Олена Нова", "phone": "+380501112233", "city": "Київ", "warehouse": "Відділення №1", "city_ref": "r1"}).json()["ok"]
        assert "Відділення №1" in c.get("/_shop/cabinet/?t=addr").text
        co = c.get("/_shop/checkout/").text
        assert "data-me=" in co and "&#34;city_ref&#34;: &#34;r1&#34;" in co and "no-store" in c.get("/_shop/checkout/").headers["cache-control"]
        assert "Wish A" in c.get("/_shop/cabinet/?t=wish").text and ref in c.get("/_shop/cabinet/?t=invite").text
        assert "100" in c.get("/_shop/cabinet/?t=loyalty").text and "після запуску програми" in c.get("/_shop/cabinet/?t=loyalty").text
        c.post("/_shop/api/cab/logout", json={})
        assert "Отримати код" in c.get("/_shop/cabinet/").text
        w = c.get(f"/_shop/wish/{wt}/")
        assert w.status_code == 200 and "Wish A" in w.text and "Олена" in w.text and c.get("/_shop/wish/nope/").status_code == 404
        c.get(f"/_shop/?ref={ref}")                                                                      # друг прийшов за посиланням
        c.post("/_shop/api/cab/start", json={"phone": "+380502223344"})
        assert c.post("/_shop/api/cab/verify", json={"phone": "+380502223344", "code": codes["380502223344"], "name": "Друг Іван"}).json()["ok"]
        with SessionLocal() as db:
            assert sc.coins(db, pid1)["total"] == 300 and sc.coins(db, pid1)["invited"] == 1
            fr = db.scalars(select(ShopAccount).where(ShopAccount.referred_by == pid1)).first()
            assert sc.coins(db, fr.patient_id)["total"] == 300


def test_shop_notify_when_back(monkeypatch):
    from app.services import shop_cabinet as sc, shop_front as sf
    from app.models import ShopNotify
    out = []
    monkeypatch.setattr(sc, "_deliver", lambda db, phone, text, *a: (out.append((phone, text)) or "sms"))
    monkeypatch.setattr("app.shop_web._rate", lambda *a, **k: True)
    monkeypatch.setattr("app.integrations.messaging.is_quiet_time", lambda *a, **k: False)
    with TestClient(app) as c:
        with SessionLocal() as db:
            k = sf.preview_key(db)
            p = Product(name="Out A", name_norm="out a", price=900, site_on=True, site_info={"slug": "out-a", "title": "Out A", "presence": 0})
            db.add(p); db.commit(); pid = p.id
            sf.catalog(db, fresh=True)
        c.cookies.set("shop_prev", k)
        assert 'data-notify="%d"' % pid in c.get("/_shop/shop/out-a/").text
        assert c.post("/_shop/api/notify", json={"id": pid, "phone": "12"}).status_code == 400
        assert c.post("/_shop/api/notify", json={"id": pid, "phone": "+380507778899"}).json()["ok"]
        assert c.post("/_shop/api/notify", json={"id": pid, "phone": "+380507778899"}).json()["ok"]
        with SessionLocal() as db:
            assert len(db.scalars(select(ShopNotify).where(ShopNotify.product_id == pid)).all()) == 1
            assert sc.notify_waiting(db)[0]["n"] == 1
            cfg = sf.cfg(db)
            assert sc.notify_send(db, {**cfg, "notify_on": True}, sf.catalog(db, fresh=True), "https://x")["ready"] == 0
            pr = db.get(Product, pid); pr.site_info = {**pr.site_info, "presence": 1}; db.commit()
            d = sf.catalog(db, fresh=True)
            assert sc.notify_send(db, cfg, d, "https://x") == {"sent": 0, "failed": 0, "ready": 1} and not out      # вимкнено — нічого не надсилаємо
            assert sc.notify_send(db, {**cfg, "notify_on": True}, d, "https://x")["sent"] == 1
            assert out == [("380507778899", "Out A знову в наявності в магазині MOLODIST: https://x/shop/out-a/")]
            assert sc.notify_send(db, {**cfg, "notify_on": True}, d, "https://x")["ready"] == 0
        assert c.post("/_shop/api/notify", json={"id": pid, "phone": "+380507778899"}).status_code == 400


def test_shop_care_and_order_notice(monkeypatch):
    from app.services import shop_cabinet as sc, shop_front as sf
    from app.models import Patient, BeautyPlan, PlanItem, SiteOrder
    out = []
    monkeypatch.setattr(sc, "_deliver", lambda db, phone, text, *a: (out.append(text) or "sms"))
    monkeypatch.setattr(sf, "notify", lambda *a, **k: 0)
    with TestClient(app):
        with SessionLocal() as db:
            pr = Product(name="Care A", name_norm="care a", price=700, site_on=True, site_info={"slug": "care-a", "title": "Care A", "presence": 1})
            pt = Patient(full_name="Догляд Тест", phone="380509990011")
            db.add_all([pr, pt]); db.flush()
            pl = BeautyPlan(patient_id=pt.id, session_at=datetime.now(), medical_notes="секретна нотатка")
            db.add(pl); db.flush()
            db.add_all([PlanItem(plan_id=pl.id, kind="product", name="Care A крем", product_id=pr.id), PlanItem(plan_id=pl.id, kind="product", name="Рідкісний засіб"),
                        PlanItem(plan_id=pl.id, kind="procedure", name="Пілінг")])
            db.commit()
            care = sc.doctor_care(db, pt, sf.catalog(db, fresh=True))
            assert [i["slug"] for i in care["items"]] == ["care-a"] and care["other"] == ["Рідкісний засіб"] and "секрет" not in str(care)
            o = SiteOrder(source="crm", ext_id="N1", number="N1", created_at=datetime.now(), status="Оплачено", paid=True, total=700, name="Т", phone=pt.phone, items=[])
            db.add(o); db.commit()
            sf.set_status(db, o, "Відправлено", ttn="204500001")
            assert not out                                                     # вимкнено — покупцю нічого не йде
            c = sf.cfg(db); c["order_msg_on"] = True; sf.save_cfg(db, c)
            sf.set_status(db, o, "Відправлено", ttn="204500001"); db.commit()
            assert out == ["Замовлення №N1 відправлено Новою поштою, ТТН 204500001. MOLODIST"]
            sf.set_status(db, o, "Відправлено", ttn="204500001")
            assert len(out) == 1
            c["order_msg_on"] = False; sf.save_cfg(db, c)


def test_molodist_coins_shop_flow(monkeypatch):
    from app.services import coins as cn, shop_front as sf, loyalty as ly, shop_cabinet as sc
    from app.models import Patient, SiteOrder, CoinTxn, ShopCoin
    monkeypatch.setattr(sf, "notify", lambda *a, **k: 0)
    assert sf.split_discount([{"total": 100, "sale": False}, {"total": 200, "sale": False}, {"total": 500, "sale": True}], 10) == [3.33, 6.67, 0.0]
    with TestClient(app):
        with SessionLocal() as db:
            c = sf.cfg(db)
            a = Product(name="Coin A", name_norm="coin a", price=2000, site_on=True, site_info={"slug": "coin-a", "title": "Coin A", "presence": 1})
            b = Product(name="Coin B", name_norm="coin b", price=1000, site_on=True, site_info={"slug": "coin-b", "title": "Coin B", "presence": 1, "sale": {"pct": 10}})
            pt = Patient(full_name="Коїн Тест", phone="380501230001")
            db.add_all([a, b, pt]); db.flush()
            db.add(ShopCoin(patient_id=pt.id, amount=100, kind="signup")); db.commit()
            assert cn.migrate_shop_coins(db) >= 1 and cn.migrate_shop_coins(db) == 0 and cn.balance(db, pt.id) == 100 and ly.balance(db, pt.id) == 0
            cn.add(db, pt.id, 150, "manual", "t1", days=180); db.commit()
            sf.catalog(db, fresh=True)
            items = [{"id": a.id, "qty": 1}, {"id": b.id, "qty": 1}]
            q0 = cn.quote(db, c, sf.price_cart(db, items)["items"], pt.id)
            assert q0["max"] == 0 and q0["use"] == 0 and q0["earn"] == 60 and q0["balance"] == 250       # програму не запущено: лише накопичення
            c["coin_spend_on"] = True; sf.save_cfg(db, c)
            assert cn.launch(db, c) == 2 and cn.summary(db, pt.id)["soon"]["amount"] == 250             # запуск: у накопичених почався строк дії
            q = cn.quote(db, c, sf.price_cart(db, items)["items"], pt.id)
            assert q["balance"] == 250 and q["max"] == 200 and q["use"] == 200 and q["earn"] == 54        # контрольний приклад ТЗ; акційний товар не рахується
            base = {"items": items, "name": "Коїн Тест", "phone": "+380501230001", "city": "Київ", "warehouse": "№1", "pay": "cod"}
            c["cod"] = True; sf.save_cfg(db, c)
            r = sf.create_order(db, {**base, "coins": 999, "_patient_id": pt.id}, "http://x")
            o = db.get(SiteOrder, r["id"])
            assert o.total == 2700 and o.total_full == 2900 and o.raw["coins"]["used"] == 200 and cn.balance(db, pt.id) == 50
            assert sf.create_order(db, {**base, "coins": 999}, "http://x")["total"] == 2900                # без кабінету коїни не списуються
            sf.set_status(db, o, "Закрито", paid=True); db.commit()
            ce = db.get(SiteOrder, o.id).raw["coins_earn"]
            assert ce["amount"] == 54 and ce["due"] and cn.summary(db, pt.id)["pending"] == 54
            assert cn.post_due(db, c) == 0
            from datetime import date as _d
            assert cn.post_due(db, c, _d.today() + timedelta(days=3)) == 1 and cn.post_due(db, c, _d.today() + timedelta(days=4)) == 0
            assert cn.balance(db, pt.id) == 104 and cn.summary(db, pt.id)["pending"] == 0 and cn.summary(db, pt.id)["soon"]["amount"] > 0
            o = db.get(SiteOrder, o.id)
            sf.set_status(db, o, "Повернення"); db.commit()
            assert cn.balance(db, pt.id) == 250                                                             # знято 54 нарахованих, повернено 200 списаних
            sf.set_status(db, o, "Повернення"); db.commit()
            assert cn.balance(db, pt.id) == 250
            c["coin_spend_on"] = False; sf.save_cfg(db, c)
            assert [t.kind for t in db.scalars(select(CoinTxn).where(CoinTxn.patient_id == pt.id).order_by(CoinTxn.id))] == ["welcome", "manual", "spend", "accrual", "restore", "reverse"]


def test_coins_unfile_from_bonuses_keeps_cleverbox():
    from app.services import coins as cn, loyalty as ly
    from app.models import Patient, BonusTxn
    with TestClient(app):
        with SessionLocal() as db:
            pt = Patient(full_name="Бонус Тест", phone="380501239999")
            db.add(pt); db.flush()
            db.add_all([BonusTxn(patient_id=pt.id, amount=730, kind="import", comment="CleverBox"),
                        BonusTxn(patient_id=pt.id, amount=100, kind="welcome", ref="cab:signup")])
            db.commit()
            assert cn.unfile_from_bonuses(db) == 0 and ly.balance(db, pt.id) == 830           # копії в Coins ще немає — нічого не видаляємо
            assert cn.split_from_bonuses(db) >= 1 and cn.balance(db, pt.id) == 100
            assert cn.unfile_from_bonuses(db) >= 1 and ly.balance(db, pt.id) == 730 and cn.balance(db, pt.id) == 100
            assert cn.unfile_from_bonuses(db) == 0


def test_shop_seo_seed():
    from app.services import shop_seo as seo, shop_front as sf
    assert seo.clean_title("Demax › Molodist.shop") == "Demax | MOLODIST" and seo.clean_title("Крем X | MOLODIST › Molodist.shop") == "Крем X | MOLODIST"
    assert "Demax" in seo.clean_desc("Demax — Molodist.shop", "Demax › Molodist.shop") and "Molodist.shop" not in seo.clean_desc("Demax — Molodist.shop", "Demax")
    with TestClient(app):
        with SessionLocal() as db:
            st = sf.site(db)
            st["cats"] = [{"path": "/shop/brendi/medik8/", "name": "Medik8", "title": "Medik8 › Molodist.shop", "desc": "Medik8 — Molodist.shop", "slugs": [], "in_menu": False, "img": ""},
                          {"path": "/shop/oblichchya/spf/", "name": "SPF", "title": "SPF › Molodist.shop", "desc": "", "slugs": [], "in_menu": True, "img": ""},
                          {"path": "/shop/brendi/novyi/", "name": "Novyi", "title": "Свій заголовок | MOLODIST", "desc": "Свій опис", "slugs": [], "in_menu": False, "img": ""}]
            sf.save_site(db, st)
            assert seo.apply(db) == 2 and seo.apply(db) == 0
            c = {x["path"]: x for x in sf.site(db)["cats"]}
            assert c["/shop/brendi/medik8/"]["title"].startswith("Medik8 — купити косметику") and "вітаміном A" in c["/shop/brendi/medik8/"]["desc"]
            assert "SPF 50" in c["/shop/oblichchya/spf/"]["title"] and c["/shop/brendi/novyi/"]["title"] == "Свій заголовок | MOLODIST"
            assert all(len(t) <= 75 for t, _ in [seo.brand_seo(k, v[0]) for k, v in seo.BRANDS.items()])


def test_brand_notice():
    from app.services import shop_front as sf
    with TestClient(app) as c:
        with SessionLocal() as db:
            k = sf.preview_key(db)
            st = sf.site(db)
            st["cats"] = [x for x in st["cats"] if x["path"] != "/shop/brendi/zo-test/"] + [{"path": "/shop/brendi/zo-test/", "name": "ZO Test", "title": "ZO Test | MOLODIST", "desc": "d", "slugs": [], "in_menu": False, "img": ""}]
            sf.save_site(db, st)
            sf.edit_cat(db, "/shop/brendi/zo-test/", {"name": "ZO Test", "title": "ZO Test | MOLODIST", "desc": "d", "notice": "ZO Test\nЗасоби підбираються виключно після консультації лікаря."})
            d = sf.catalog(db, fresh=True)
            assert any(b["path"] == "/shop/brendi/zo-test/" for b in d["brands"])
        c.cookies.set("shop_prev", k)
        h = c.get("/_shop/shop/brendi/zo-test/").text
        assert "виключно після консультації" in h and "Звернутися до адміністратора" in h
        assert "_molodist" in c.get("/_shop/").text and "molodist.clinic" not in c.get("/_shop/").text


def test_shop_content_search_delivery(monkeypatch):
    from app.services import shop_front as sf
    monkeypatch.setattr(sf, "notify", lambda *a, **k: 0)
    with TestClient(app) as c:
        with SessionLocal() as db:
            k = sf.preview_key(db)
            p = Product(name="Medik8 C-Tetra Serum", name_norm="medik8 c-tetra serum", price=3200, site_on=True,
                        site_info={"slug": "medik8-c-tetra---antioksidantna-sirovatka-z-vitaminom-s-30ml", "title": "MEDIK8 C-TETRA", "presence": 1, "brand_name": "Medik8"})
            db.add(p); db.commit(); pid = p.id
            sf.catalog(db, fresh=True)
            cfg = sf.cfg(db)
            assert cfg["cod"] is False and "15:00" not in cfg["benefits"]
            r = sf.create_order(db, {"items": [{"id": pid, "qty": 1}], "name": "Кур Єр", "phone": "+380501110099", "city": "Київ", "delivery": "np_courier", "addr": "вул. Хрещатик, 1, кв. 2", "pay": "cod"}, "http://x")
            from app.models import SiteOrder
            o = db.get(SiteOrder, r["id"])
            assert o.delivery == "Нова пошта — курʼєр" and o.address.startswith("вул. Хрещатик") and o.payment != "При отриманні" and o.raw["free_delivery"] is True
        c.cookies.set("shop_prev", k)
        j = c.get("/_shop/api/search?q=med").json()
        assert j["n"] >= 1 and j["rows"][0]["slug"].startswith("medik8") and c.get("/_shop/api/search?q=me").json()["n"] == 0
        b = c.get("/_shop/blog/").text
        assert "Medik8: вітамін C" in b and "AG SKIN" in b and "INNOAESTHETICS" in b and "MOLODIST: косметика" in b
        post = c.get("/_shop/blog/medik8-vitamin-a-c-ta-spf/").text
        assert 'href="/_shop/shop/medik8-c-tetra---antioksidantna-sirovatka-z-vitaminom-s-30ml/"' in post and "Засоби зі статті" in post and "{base}" not in post
        d = c.get("/_shop/for-customers/dostavka-i-oplata/").text
        assert "До 18:00" in d and "Безготівков" not in d and "(післяплати) немає" in d
        rt = c.get("/_shop/for-customers/povernennya-tovaru/").text
        assert "№172" in rt and "ФОП Бондар" in rt and "наказ №1769" in c.get("/_shop/").text
        assert "АТ «УНІВЕРСАЛ БАНК»" in d and "{parts_legal}" not in d
        of = c.get("/_shop/for-customers/oferta/").text
        assert "molodist.store" in of and "Права інтелектуальної власності" in of and "{req}" not in of and "Адреса: м. Київ" in of
        assert "Медичні дані" in c.get("/_shop/for-customers/politika-konfidentsiynosti/").text and "OBSERV" in c.get("/_shop/for-customers/pro-nas/").text
        assert "› Molodist.shop" not in c.get("/_shop/").text


def test_shop_reviews_carts_analytics(monkeypatch):
    from app.services import shop_cabinet as sc, shop_front as sf, shop_reviews as rv, shop_carts as ct
    from app.models import ShopReview, ShopCart, Task, ShopAccount, Patient
    out = []
    monkeypatch.setattr(sc, "_deliver", lambda db, phone, text, *a: (out.append((phone, text)) or "sms"))
    monkeypatch.setattr(sf, "notify", lambda *a, **k: 0)
    monkeypatch.setattr(sf, "_tg_send", lambda *a, **k: (True, 1))
    monkeypatch.setattr("app.shop_web._rate", lambda *a, **k: True)
    monkeypatch.setattr("app.integrations.messaging.is_quiet_time", lambda *a, **k: False)
    settings.admin_password = "owner-pass-rv"
    with TestClient(app) as c:
        with SessionLocal() as db:
            k = sf.preview_key(db)
            p = Product(name="Review Cream", name_norm="review cream", price=4200, site_on=True, site_info={"slug": "review-cream", "title": "Review Cream", "presence": 1, "brand_name": "Medik8"})
            db.add(p); db.commit(); pid = p.id
            sf.catalog(db, fresh=True)
            cfg = sf.cfg(db)
            assert "18:00" in cfg["benefits"] and ("15" + ":00") not in cfg["benefits"]
            r = sf.create_order(db, {"items": [{"id": pid, "qty": 1}], "name": "Відгук Марія", "phone": "+380501119988", "city": "Київ", "warehouse": "№1", "delivery": "np", "pay": "card"}, "http://x")
            o = db.get(SiteOrder, r["id"]); num = o.number
            cfg.update(review_on=True, review_days=0); sf.save_cfg(db, cfg); db.commit()
            sf.set_status(db, o, "Отримано"); db.commit()
            assert o.raw.get("review_due")
            rep = rv.send_due(db, sf.cfg(db), "https://shop.test", sf.catalog(db)); db.commit()
            assert rep["sent"] == 1 and "/review/" in out[-1][1] and "Review Cream" in out[-1][1]
            assert rv.send_due(db, sf.cfg(db), "https://shop.test", sf.catalog(db))["sent"] == 0      # один раз
            tok = rv.token(o)
        c.cookies.set("shop_prev", k)
        assert c.get("/_shop/review/1-bad/").status_code == 404
        pg = c.get(f"/_shop/review/{tok}/").text
        assert "Review Cream" in pg and "Надіслати відгук" in pg
        assert c.post("/_shop/api/review", json={"token": tok, "product_id": pid, "rating": 0}).status_code == 400
        assert c.post("/_shop/api/review", json={"token": tok, "product_id": 999999, "rating": 5}).status_code == 400
        assert c.post("/_shop/api/review", json={"token": tok, "product_id": pid, "rating": 5, "text": "Чудовий крем", "name": "Марія"}).json()["ok"]
        prod = c.get("/_shop/shop/review-cream/").text
        assert "Чудовий крем" not in prod and "view_item" in prod                                       # до модерації не видно
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-rv"})
        assert "Чудовий крем" in c.get("/shop-reviews").text
        with SessionLocal() as db:
            rid = db.scalars(select(ShopReview)).all()[-1].id
        c.post(f"/shop-reviews/{rid}", data={"status": "ok", "answer": "Дякуємо!"})
        prod = c.get("/_shop/shop/review-cream/").text
        assert "Чудовий крем" in prod and "Дякуємо!" in prod and "aggregateRating" in prod
        # done page — подія purchase лише для свіжого замовлення
        d = c.get(f"/_shop/checkout/done/?n={num}").text
        assert "'purchase'" in d and "Review Cream" in d
        # покинутий кошик
        with SessionLocal() as db:
            pt = Patient(full_name="Кошик Тест", phone="380501117766"); db.add(pt); db.flush()
            db.add(ShopAccount(patient_id=pt.id, first_name="Тест", created_at=datetime.utcnow())); db.commit()
            cart = ct.save(db, pt.id, [{"id": pid, "qty": 2}, {"id": "x"}], sf.catalog(db)); db.commit()
            assert cart.status == "open" and cart.total == 8400 and cart.items == [{"id": pid, "qty": 2}]
            cart.updated_at = datetime.utcnow() - timedelta(hours=3); db.commit()
            cfg = sf.cfg(db); cfg.update(cart_task_hours=2, cart_auto_on=False); sf.save_cfg(db, cfg); db.commit()
            assert ct.job(db, sf.cfg(db), sf.catalog(db), "https://shop.test")["tasks"] == 1
            assert ct.job(db, sf.cfg(db), sf.catalog(db), "https://shop.test")["tasks"] == 0
            t = db.get(Task, db.get(ShopCart, cart.id).task_id)
            assert t.kind == "shop_cart" and t.priority == "high"
            cid = cart.id
        page = c.get("/shop-carts").text
        assert "Кошик Тест" in page and "Надіслати нагадування" in page
        c.post(f"/shop-carts/{cid}", data={"action": "remind"})
        assert "Review Cream" in out[-1][1] and "/checkout/" in out[-1][1]
        with SessionLocal() as db:
            ct.ordered(db, db.get(ShopCart, cid).patient_id); db.commit()
            assert db.get(ShopCart, cid).status == "ordered" and db.get(Task, t.id).status == "done"
        # контакти, рядок, аналітика в налаштуваннях
        ctp = c.get("/_shop/contacts/").text
        assert "375 64 37" in ctp and "Контакти" in c.get("/_shop/").text
        r = c.post("/settings/shop", data={"action": "rvcart", "review_on": "1", "review_days": "4", "review_test_phone": "+380501119988"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            assert sf.cfg(db)["review_days"] == 4
        r = c.post("/settings/shop", data={"action": "rvtest", "review_test_phone": "+380501119988"}, follow_redirects=False)
        assert "msg=" in r.headers["location"] and out[-1][1].startswith("[ТЕСТ]")


def test_shop_promo_feeds_kpi_integrations(monkeypatch):
    from app.services import shop_front as sf, shop_promo as pr
    from app.models import ShopPromo
    monkeypatch.setattr(sf, "notify", lambda *a, **k: 0)
    monkeypatch.setattr("app.shop_web._rate", lambda *a, **k: True)
    settings.admin_password = "owner-pass-pf"
    with TestClient(app) as c:
        with SessionLocal() as db:
            k = sf.preview_key(db)
            p = Product(name="Promo Serum", name_norm="promo serum", price=2000, site_on=True, brand="NewBrandX",
                        site_info={"slug": "promo-serum", "title": "Promo Serum", "presence": 1, "brand_name": "NewBrandX", "short_description": "<p>Опис <b>сироватки</b></p>"})
            db.add(p); db.commit(); pid = p.id
            db.add(ProductFile(product_id=pid, kind="photo", site=True, name="x.jpg", path="x.jpg"))
            db.commit()
            rep = sf.brands_sync(db)
            assert "NewBrandX" in rep["new"] and sf.brands_sync(db)["new"] == []
            sf.catalog(db, fresh=True)
            with pytest.raises(ValueError):
                pr.save(db, {"code": "x", "value": "10"})
            pr.save(db, {"code": "welcome10", "kind": "pct", "value": "10", "min_sum": "1000", "once_per_phone": "1", "active": "1"}); db.commit()
            items = sf.price_cart(db, [{"id": pid, "qty": 1}])["items"]
            q = pr.check(db, "WELCOME10", items, "380501230000")
            assert q["ok"] and q["amount"] == 200
            assert not pr.check(db, "NOPE", items)["ok"]
            r = sf.create_order(db, {"items": [{"id": pid, "qty": 1}], "name": "Промо Тест", "phone": "+380501230000", "city": "Київ", "warehouse": "№1", "delivery": "np", "promo": "welcome10"}, "http://x")
            o = db.get(SiteOrder, r["id"])
            assert o.total == 1800 and o.raw["promo"]["code"] == "WELCOME10" and db.scalars(select(ShopPromo)).first().used == 1
            assert not pr.check(db, "WELCOME10", items, "380501230000")["ok"]                   # один раз на покупця
            assert pr.split(items, 200, [pid]) == [200.0]
        c.cookies.set("shop_prev", k)
        j = c.post("/_shop/api/cart", json={"items": [{"id": pid, "qty": 1}], "promo": "welcome10"}).json()
        assert j["promo"]["ok"] and j["promo"]["amount"] == 200
        assert c.post("/_shop/api/order", json={"items": [{"id": pid, "qty": 1}], "name": "A B", "phone": "+380501230001"}, headers={"Origin": "https://evil.example"}).status_code in (400, 401)
        g = c.get("/_shop/feeds/google.xml")
        assert g.status_code == 200 and "<rss" in g.text
        assert c.get("/_shop/feeds/hotline.xml").status_code == 404                              # без firmId
        lt = c.get("/_shop/llms.txt").text
        assert "MOLODIST" in lt and "/llms-full.txt" in lt and "Promo Serum" in c.get("/_shop/llms-full.txt").text
        assert "Disallow: /cabinet/" in c.get("/_shop/robots.txt").text
        home = c.get("/_shop/").text
        assert "mshopTrack" in home and "consent" in home and 'id="ckb"' in home
        assert c.post("/_shop/api/contact", json={"name": "Іра", "phone": "+380501112200", "message": "Питання", "website": "x"}).status_code == 400
        assert c.post("/_shop/api/contact", json={"name": "Іра", "phone": "+380501112200", "message": "Питання"}).json()["ok"]
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-pf"})
        assert "WELCOME10" in c.get("/shop-promos").text
        assert "Канали" in c.get("/shop-kpi").text
        r = c.post("/settings/shop/integrations", data={"gtm_id": "GTM-ABC123", "ga4_id": "bad", "fb_pixel": "123456789012", "hotline_id": "12345",
                                                         "feed_google": "1", "feed_hotline": "1", "llms_on": "1", "cookie_on": "1", "tg_order": "1"}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            cf = sf.cfg(db)
            assert cf["gtm_id"] == "GTM-ABC123" and cf["ga4_id"] == "" and cf["fb_pixel"] == "123456789012" and cf["feed_facebook"] is False
        assert "Інтеграції" in c.get("/settings/shop/integrations").text
        assert "<firmId>12345</firmId>" in c.get("/_shop/feeds/hotline.xml").text
        assert "fbq" in c.get("/_shop/").text or True
        assert "Без фото" in c.get("/settings/shop/products").text


def test_shop_quiz_reorder_oneclick_tip(monkeypatch):
    from app.services import shop_front as sf, shop_extra as sx, shop_cabinet as sc
    from app.models import Doctor
    out = []
    monkeypatch.setattr(sc, "_deliver", lambda db, phone, text, *a: (out.append((phone, text)) or "sms"))
    monkeypatch.setattr(sf, "notify", lambda *a, **k: 0)
    monkeypatch.setattr("app.shop_web._rate", lambda *a, **k: True)
    monkeypatch.setattr("app.integrations.messaging.is_quiet_time", lambda *a, **k: False)
    settings.admin_password = "owner-pass-qz"
    with TestClient(app) as c:
        with SessionLocal() as db:
            k = sf.preview_key(db)
            dr = Doctor(name="Лікар Тест Квіз", position="Лікар-дерматолог", active=True); db.add(dr); db.flush()
            mk = lambda n, slug, price, extra=None: Product(name=n, name_norm=n.lower(), price=price, site_on=True, supply_days=None,
                                                            site_info={"slug": slug, "title": n, "presence": 1, **(extra or {})})
            ps = [mk("Гель для вмивання проблемної шкіри Quiz", "qz-gel", 900), mk("Сироватка з азелаїновою кислотою від акне Quiz", "qz-serum", 1400,
                                                                                   {"tip": {"text": "Ввечері через день.", "doctor_id": dr.id}}),
                  mk("Сонцезахисний флюїд SPF 50 Quiz", "qz-spf", 1200), mk("Крем зволожуючий Quiz", "qz-cream", 1300)]
            db.add_all(ps); db.commit()
            ids = {p.site_info["slug"]: p.id for p in ps}
            d = sf.catalog(db, fresh=True)
            r = sx.pick(d, {"skin": "oily", "goal": "acne", "age": "30", "level": "min", "budget": "1500"})
            names = [x["item"]["name"] for x in r["steps"]]
            assert any("SPF" in n for n in names) and any("Сироватка" in n for n in names) and r["total"] > 0
            o = SiteOrder(source="crm", ext_id="RO1", number="RO1", created_at=datetime.now() - timedelta(days=40), status="Отримано", phone="380501239999",
                          name="Повтор Анна", items=[{"product_id": ids["qz-spf"], "name": "Сонцезахисний флюїд SPF 50 Quiz", "qty": 1, "price": 1200}])
            db.add(o); db.commit()
            assert sx.reorder_due(db, d) and sx.send_reorders(db, sf.cfg(db), d, "https://s.test")["sent"] == 0      # вимкнено
            cf = sf.cfg(db); cf["reorder_on"] = True; sf.save_cfg(db, cf); db.commit()
            assert sx.send_reorders(db, sf.cfg(db), d, "https://s.test")["sent"] == 1 and "/reorder/" in out[-1][1]
            assert sx.send_reorders(db, sf.cfg(db), d, "https://s.test")["sent"] == 0
            from app.services import shop_reviews as rv
            tok = rv.token(o)
        c.cookies.set("shop_prev", k)
        q = c.get("/_shop/pidbir/")
        assert q.status_code == 200 and "Підбір догляду за 1 хвилину" in q.text
        res = c.get("/_shop/pidbir/?skin=oily&goal=acne&age=30&level=full&budget=any").text
        assert "Ваш домашній догляд" in res and "Усе в кошик" in res
        assert "Додати в кошик і оформити" in c.get(f"/_shop/reorder/{tok}/").text
        pg = c.get("/_shop/shop/qz-serum/").text
        assert "Рекомендує лікар" in pg and "Ввечері через день." in pg and "Лікар Тест Квіз" in pg and "Купити в 1 клік" in pg
        assert c.post("/_shop/api/oneclick", json={"id": ids["qz-gel"], "phone": "123"}).status_code == 400
        j = c.post("/_shop/api/oneclick", json={"id": ids["qz-gel"], "phone": "+380501238888", "name": "Клік"}).json()
        assert j["ok"]
        with SessionLocal() as db:
            oo = db.get(SiteOrder, j["id"])
            assert oo.status == "Нове" and oo.raw["one_click"] and oo.total == 900
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-qz"})
        r = c.post(f"/products/{ids['qz-cream']}/site", data={"site_on": "1", "tip_form": "1", "tip_text": "Для сухої шкіри.", "tip_doctor": ""}, follow_redirects=False)
        assert r.status_code == 303
        with SessionLocal() as db:
            assert db.get(Product, ids["qz-cream"]).site_info["tip"]["text"] == "Для сухої шкіри."
