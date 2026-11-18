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
        assert r.status_code == 404 and "NOT FOUND" in r.text
        assert "TILDA LASER" not in cl.get("/_site/laserepilation").text     # сторінка CRM має пріоритет
        assert "HOME" in cl.get("/_site/").text
