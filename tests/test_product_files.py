import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, Product, ProductFile
from app.services import product_files


class J:
    def log(self, m):
        pass


def test_product_files(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    with SessionLocal() as db:
        a = Product(name="Кеналог", name_norm="кеналог", cb_product_id=487, warehouse="inject")
        b = Product(name="Кеналог", name_norm="кеналог", cb_product_id=487, warehouse="classic")
        db.add_all([a, b]); db.commit(); ida, idb = a.id, b.id
    monkeypatch.setattr(httpx.Client, "get", lambda self, url: httpx.Response(200, content=b"%PDF-1.4 x" if url.endswith("pdf") else b"\xff\xd8jpg", request=httpx.Request("GET", url)))
    rows = [[487, 13982, "https://cbox.5jk9u.upcloudobjects.com/molodist/product/487_1.pdf", "Сертифікат.pdf", "29.10.2026 13:05", "file"],
            [487, 13983, "https://cbox.5jk9u.upcloudobjects.com/molodist/product/487_2.jpg", "фото.jpg", "29.10.2026 13:06", "img"],
            [99999, 13984, "https://cbox.5jk9u.upcloudobjects.com/molodist/product/x.jpg", "x.jpg", "", "img"]]
    r = product_files.run(J(), rows)
    assert r["saved"] == 2 and r["no_product"] == 1 and r["main_photo"] == 2
    assert product_files.run(J(), rows)["exists"] == 2
    with SessionLocal() as db:
        assert db.query(ProductFile).filter(ProductFile.product_id == idb).count() == 2
        assert db.get(Product, ida).photo_url.startswith("/products/files/")
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        page = c.get(f"/products/{ida}").text
        assert "Сертифікат.pdf" in page and 'id="pfiles"' in page
        with SessionLocal() as db:
            fid = db.query(ProductFile).filter(ProductFile.product_id == ida, ProductFile.kind == "photo").one().id
        assert c.get(f"/products/files/{fid}").status_code == 200
