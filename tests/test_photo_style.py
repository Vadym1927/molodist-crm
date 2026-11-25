"""«Фото в стилі MOLODIST»: композиція на кремовому фоні (без моделі — фото вже з прозорим фоном) і маршрут у картці."""
import io

from PIL import Image, ImageDraw

from app.services import photo_style as ps


def _png_transparent():
    im = Image.new("RGBA", (400, 600), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle((150, 100, 250, 500), fill=(20, 20, 20, 255))
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_compose_cream_square():
    out = Image.open(io.BytesIO(ps.styled_bytes(_png_transparent())))
    assert out.size == (ps.SIZE, ps.SIZE) and out.format == "JPEG"
    assert out.getpixel((10, 10)) == ps.BG or max(abs(a - b) for a, b in zip(out.getpixel((10, 10)), ps.BG)) <= 3
    assert sum(out.getpixel((ps.SIZE // 2, ps.SIZE // 2))) < 120          # товар по центру


def test_photo_style_route(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app
    from app.models import SessionLocal, Product, ProductFile
    from app.services import ingest
    settings.admin_password = "owner-pass-1"
    monkeypatch.setattr(ps, "styled_bytes", lambda data, job=None: ps.compose(Image.new("RGB", (300, 300), "white"),
                                                                              Image.new("L", (300, 300), 255)))
    from types import SimpleNamespace
    from app.services import jobs

    def inline(title, fn, *a, **k):                       # у тестах — без потоку (sqlite у памʼяті)
        j = jobs.Job(id="t1", title=title)
        j.result, j.status = fn(j, *a, **k), "done"
        jobs.JOBS[j.id] = j
        return j
    monkeypatch.setattr(jobs, "start", inline)
    with SessionLocal() as db:
        p = Product(name="Тест фото стиль", name_norm="тест фото стиль", warehouse="shop", active=True)
        db.add(p)
        db.flush()
        f = ProductFile(product_id=p.id, path=ingest.store(_png_transparent(), "t.png"), name="t.png", kind="photo", mime="image/png", site=True)
        db.add(f)
        db.commit()
        pid, old = p.id, f.id
    with TestClient(app) as c:
        assert c.post("/login", data={"login": "vadim", "password": "owner-pass-1"}, follow_redirects=False).status_code == 303
        r = c.post(f"/products/{pid}/photo-style", follow_redirects=False)
        assert r.status_code == 303 and "psjob=" in r.headers["location"]
        with SessionLocal() as db:
            p = db.get(Product, pid)
            new = db.query(ProductFile).filter(ProductFile.product_id == pid, ProductFile.id != old).one()
            assert p.photo_url == f"/products/files/{new.id}" and new.site and not db.get(ProductFile, old).site
        t = c.get(f"/products/{pid}?psjob=t1").text
        assert "Фото в стилі MOLODIST" in t and "Готово" in t


def test_trim_frame():
    im = Image.new("RGB", (500, 500), (240, 236, 230))
    ImageDraw.Draw(im).rectangle((40, 40, 460, 460), fill=(200, 60, 60))
    t = ps.trim_frame(im)
    assert t.size[0] < 430 and t.size[1] < 430
    plain = Image.new("RGB", (500, 500), "white")
    ImageDraw.Draw(plain).rectangle((200, 100, 300, 450), fill=(10, 10, 10))
    assert ps.trim_frame(plain).size == (500, 500)            # товар на білому — не обрізаємо (сцени немає)
