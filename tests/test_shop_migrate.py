from pathlib import Path

from app.models import SessionLocal, Product, ProductFile


class FakeClient:
    root = "https://molodist.store/"
    how = "body"

    def products(self):
        yield {"id": 9101, "sku": "MIG-1", "title": "Крем міграційний 50 мл", "link": "/shop/krem/9101/", "slug": "krem",
               "short_description": "<p>Опис <b>крему</b></p><script>alert(1)</script><a href=x onclick=y>x</a>",
               "seo_title": "Крем SEO", "brand": {"title": "Ag Skin"}, "price": 1950, "price_old": 2100,
               "images": ["/Media/shop-1/a.webp", {"original": "https://molodist.store/Media/shop-1/b.webp"}],
               "gallery_common": [], "characteristics": [{"name": "Обʼєм", "value": "50 мл"}], "display_in_showcase": True}
        yield {"id": 9102, "sku": "NOPE", "title": "Немає в CRM"}


class R:
    def __init__(self, content, ctype="image/webp", code=200):
        self.content, self.status_code, self.headers = content, code, {"content-type": ctype}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return R(b"IMG" + url.encode())


def test_migrate_photos_texts_and_no_dupes(monkeypatch, tmp_path):
    from app.config import settings
    from app.services import shop_migrate as sm
    from app.integrations import shop_express as sx
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    monkeypatch.setattr(sx, "sync", lambda db, client=None: {})
    monkeypatch.setattr(sx, "creds", lambda db: {"login": "x", "site": "https://molodist.store/", "password": "y"})
    with SessionLocal() as db:
        p = Product(name="Крем міграційний 50 мл", name_norm="крем міграційний 50 мл", sku="MIG-1", shop_id="9101",
                    photo_url="https://molodist.store/Media/shop-1/a.webp")
        db.add(p); db.commit(); pid = p.id
        http = FakeHttp()
        rep = sm.migrate(db, client=FakeClient(), http=http)
        assert rep["photos_new"] == 2 and rep["no_match"] == 1 and rep["products"] == 1
        p = db.get(Product, pid)
        assert p.photo_url.startswith("/products/files/") and p.site_on is True
        si = p.site_info
        assert "<script" not in si["short_description"] and "onclick" not in si["short_description"] and "<b>крему</b>" in si["short_description"]
        assert si["seo_title"] == "Крем SEO" and si["price_old"] == 2100 and len(si["images"]) == 2
        fs = db.query(ProductFile).filter(ProductFile.product_id == pid).order_by(ProductFile.sort).all()
        assert [f.site for f in fs] == [True, True] and Path(fs[0].path).read_bytes().startswith(b"IMG")
        # правка в CRM + повторний перенос: фото не дублюються, правка лишається
        p.site_info = {**si, "short_description": "<p>Наш опис</p>", "edited_at": "2026-11-16T13:00"}
        db.commit()
        rep2 = sm.migrate(db, client=FakeClient(), http=http)
        assert rep2["photos_new"] == 0 and len(http.calls) == 2
        assert db.get(Product, pid).site_info["short_description"] == "<p>Наш опис</p>"
        assert list(tmp_path.joinpath("shop_express").glob("export-*.json"))
        assert sm.coverage(db)["local_photo"] >= 1


def test_product_card_site_block():
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    settings.admin_password = "owner-pass-sm"
    with TestClient(app) as c:
        with SessionLocal() as db:
            p = Product(name="Сайт-блок тест", name_norm="сайт-блок тест", shop_id="9300", site_on=True,
                        site_info={"short_description": "<p>Опис</p>", "link": "/shop/x/", "brand": {"title": "Ag"}, "price": 10,
                                   "characteristics": [{"name": "a", "value": "b"}], "groups": [1], "migrated_at": "2026-11-16T13:00"})
            db.add(p); db.commit(); pid = p.id
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-sm"})
        r = c.get(f"/products/{pid}")
        assert r.status_code == 200 and "На сайті" in r.text and "<p>Опис</p>" in r.text
        assert c.post(f"/products/{pid}/site", data={"short_description": "<p>Новий</p><script>x</script>", "seo_title": "T"},
                      follow_redirects=False).status_code == 303
        with SessionLocal() as db:
            p = db.get(Product, pid)
            assert p.site_on is False and p.site_info["short_description"] == "<p>Новий</p>" and p.site_info["edited_at"]
        assert c.get("/settings/keys?s=shop").status_code == 200
