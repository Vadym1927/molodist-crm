"""Архів Tilda: розпакування (без ../), адреси з htaccess, віддача сторінок і файлів, міст форм, пріоритет сторінок CRM."""
import io
import zipfile

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal
from app.services import tilda_site as ts


def _zip() -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("project1/htaccess", "DirectoryIndex page1.html\nErrorDocument 404 /page9.html\n"
                   "RewriteRule ^price$ page2.html [NC]\nRewriteRule ^price/$ page2.html [NC]\n"
                   "RewriteRule ^laserepilation$ page3.html [NC]\n")
        z.writestr("project1/page1.html", "<html><head></head><body>HOME mailto:info.molodostua@gmail.com</body></html>")
        z.writestr("project1/page2.html", "<html><body>PRICE<form class='t-form'></form></body></html>")
        z.writestr("project1/page3.html", "<html><body>TILDA LASER</body></html>")
        z.writestr("project1/page9.html", "<html><body>NOT FOUND</body></html>")
        z.writestr("project1/css/a.css", "body{}")
        z.writestr("project1/../evil.txt", "x")
    return b.getvalue()


def test_tilda_import_and_serve(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    with SessionLocal() as db:
        c = ts.import_zip(db, _zip())
        assert c["pages"] == 2 and c["map"]["price"] == "page2.html" and c["index"] == "page1.html"
        assert not (tmp_path / "evil.txt").exists()
        html = ts.render(db, ts.page_file(db, "price"))
        assert "tilda-bridge.js" in html and "PRICE" in html
        assert "info@molodist.com.ua" in ts.render(db, ts.page_file(db, ""))
    with TestClient(app) as cl:
        assert "PRICE" in cl.get("/_site/price").text
        assert cl.get("/_site/css/a.css").text == "body{}"
        assert cl.get("/_site/css/../htaccess").status_code == 404
        r = cl.get("/_site/nope-page")
        assert r.status_code == 404 and "Такої сторінки немає" in r.text      # своя 404, не з архіву Tilda
        assert "TILDA LASER" not in cl.get("/_site/laserepilation").text     # сторінка CRM має пріоритет
        assert "HOME" in cl.get("/_site/").text


def test_google_feed_desc_without_medical_claims():
    from app.services.shop_feeds import clean_desc
    d = ("Glow Antioxidant Mask — ефективний засіб для боротьби з розацеа і куперозом. Маска зволожує і освітлює шкіру. "
         "Склад відповідає рекомендованим протоколам лікування розацеа від FDA. Підходить для щоденного догляду.")
    out = clean_desc(d)
    assert "FDA" not in out and "розацеа" not in out and "зволожує" in out and "щоденного" in out


def test_staging_host_noindex():
    from fastapi.testclient import TestClient
    with TestClient(app) as cl:
        from app.services import site_pages as sp
        sp._cache["hosts"], sp._cache["at"] = {"new.example.ua"}, 9e18
        r = cl.get("/", headers={"host": "new.example.ua"})
        assert "noindex" in r.headers.get("x-robots-tag", "")
        sp._cache["at"] = 0


def test_tilda_cdn_localized(tmp_path, monkeypatch):
    from app.services import tilda_site as ts
    import httpx
    monkeypatch.setattr(ts, "root", lambda: tmp_path)
    html = '<script src="https://neo.tildacdn.com/js/tilda-fallback-1.0.min.js"></script><img src="https://static.tildacdn.one/lib/flags/flags7.png">'
    out = ts.localize(html)
    assert "https://" not in out and "/_site/_tcdn/neo.tildacdn.com/js/tilda-fallback-1.0.min.js" in out
    calls = []
    cl = httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(str(r.url)) or httpx.Response(200, text="var a='https://static.tildacdn.one/lib/flags/flags7.png';")))
    f = ts.cdn("static.tildacdn.one/js/tilda-forms-dict-1.0.min.js", cl)
    assert f and "_tcdn/static.tildacdn.one/lib/flags" in f.read_text() and len(calls) == 1
    assert ts.cdn("static.tildacdn.one/js/tilda-forms-dict-1.0.min.js", cl) == f and len(calls) == 1      # з кешу
    assert ts.cdn("evil.com/js/x.js", cl) is None and ts.cdn("static.tildacdn.one/js/../../x.js", cl) is None


def test_feed_clean_google_triggers():
    from app.services.shop_feeds import clean_desc, clean_title
    d = ("Крем зволожує шкіру. Допомагає при акне. Містить олію насіння канабісу. "
         "Aqua, Glycerin, Cannabis Sativa Seed Oil, Squalane, Tocopherol, Panthenol, Betaine, Allantoin, Sodium Benzoate, Citric Acid.")
    out = clean_desc(d)
    assert "зволожує" in out and "акне" not in out and "канабіс" not in out and "Cannabis" not in out and "Glycerin" not in out
    assert clean_title("Гель AG SKIN 3 Step Acid System For Acne 150 мл") == "Гель AG SKIN 3 Step Acid System 150 мл"


def test_tilda_doctor_block_inserted():
    from app.services import tilda_site as ts
    h = ts.add_blocks('<div id="allrecords"><div id="rec680643180" class="r t-rec">ВІДГУКИ</div></div>')
    assert h.index("Софія Сирова") < h.index("ВІДГУКИ") and "клінік" not in h.lower()
    assert ts.add_blocks("<div>інша сторінка</div>") == "<div>інша сторінка</div>"
