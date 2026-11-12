"""Сайт клініки на VPS: сторінки напрямків, ціни з прайсу CRM, домен сайту, заявки → звернення."""
from datetime import date

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, CbService, Appeal
from app.services import site_pages as sp


def _svc(db, sid, name, price, active=True):
    s = db.get(CbService, sid) or CbService(id=sid, name=name)
    s.name, s.price, s.active = name, price, active
    db.merge(s)


def test_prices_live_from_crm_and_promo():
    with SessionLocal() as db:
        _svc(db, 4, "Лазер Light (Пахви+Глибоке Бікіні)", 1400)
        _svc(db, 114, "Лазер Full", 3200)
        _svc(db, 86, "Лазер Пахви", 550, active=False)
        db.commit()
        p = sp.PAGES["laserepilation"]
        g = sp.prices(db, p, today=date(2026, 11, 15))
        cx = next(x for x in g if x["title"] == "Комплекси")
        light = next(r for r in cx["rows"] if r["id"] == 4)
        assert light["old"] == 1400 and light["price"] == 700                  # −50% на перший візит
        assert next(r for r in cx["rows"] if r["id"] == 114).get("old") is None
        assert not any(r["id"] == 86 for x in g for r in x["rows"])          # прихована послуга — не на сайті
        assert next(r for r in sp.prices(db, p, today=date(2026, 12, 1))[0]["rows"] if r["id"] == 4)["price"] == 1400   # акція скінчилась
        _svc(db, 4, "Лазер Light", 1500); db.commit()
        assert next(r for x in sp.prices(db, p, today=date(2027, 1, 1)) for r in x["rows"] if r["id"] == 4)["price"] == 1500
        assert 86 in sp.missing(db, "laserepilation")


def test_site_host_routing_and_lead():
    with SessionLocal() as db:
        _svc(db, 208, "Видалення невусу 1 шт", 1500); db.commit()
        c = sp.cfg(db); c["hosts"] = ["new.molodist.test"]; sp.save_cfg(db, c)
    with TestClient(app) as cl:
        r = cl.get("/_site/vydalennia-rodymok")                               # перегляд у CRM — noindex
        assert r.status_code == 200 and "1 500 ₴" in r.text and r.headers.get("x-robots-tag")
        h = {"host": "new.molodist.test"}
        r = cl.get("/vydalennia-rodymok", headers=h)
        assert r.status_code == 200 and "noindex" not in r.text and "application/ld+json" in r.text and 'href="/laserepilation"' in r.text
        assert cl.get("/", headers=h).status_code == 200
        assert cl.get("/login", headers=h).status_code == 404                # CRM на домені сайту недоступна
        assert cl.get("/patients", headers=h).status_code == 404
        assert "sitemap.xml" in cl.get("/robots.txt", headers=h).text and "/beautyplan" in cl.get("/sitemap.xml", headers=h).text
        assert cl.get("/static/site/site.css", headers=h).status_code == 200
        assert cl.post("/api/lead", headers=h, json={"phone": "+380501112233", "website": "spam", "t": 9}).json()["ok"]
        r = cl.post("/api/lead", headers=h, json={"name": "Тест Сайт", "phone": "+380501112299", "interest": "Видалення новоутворень",
                                                    "formname": "vydalennia-rodymok:hero", "gclid": "Cj0TEST", "utm_source": "google", "t": 12})
        assert r.json()["ok"]
        assert cl.post("/api/lead", headers=h, json={"phone": "12", "t": 9}).status_code == 400
    with SessionLocal() as db:
        a = db.query(Appeal).filter(Appeal.name == "Тест Сайт").one()
        assert "gclid=Cj0TEST" in (a.utm or "") and a.interest == "Видалення новоутворень"
        assert not db.query(Appeal).filter(Appeal.phone == "+380501112233").count()           # бот (приховане поле) — без звернення
        c = sp.cfg(db); c["hosts"] = ["new.molodist.com.ua"]; sp.save_cfg(db, c)


def test_settings_page():
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = cl.get("/settings/site")
        assert r.status_code == 200 and "Сайт клініки" in r.text and "/_site/laserepilation" in r.text
        assert cl.post("/settings/site", data={"hosts": "new.molodist.com.ua, crm.molodist.com.ua", "phone_text": "+38 (093) 375 64 37"},
                       follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert sp.cfg(db)["hosts"] == ["new.molodist.com.ua"]               # домен CRM сайтом зробити не можна


def test_media_upload_and_page_images():
    import io
    from PIL import Image
    settings.admin_password = "owner-pass-1"
    buf = io.BytesIO(); Image.new("RGB", (40, 50), (90, 70, 130)).save(buf, "WEBP"); data = buf.getvalue()
    with TestClient(app) as cl:
        assert cl.post("/settings/site/media", files=[("files", ("laser-hero.webp", data, "image/webp"))]).status_code in (401, 403) \
            or "login" in cl.post("/settings/site/media", files=[("files", ("laser-hero.webp", data, "image/webp"))]).url.path
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        r = cl.post("/settings/site/media", files=[("files", ("laser-hero.webp", data, "image/webp")), ("files", ("x.svg", b"<svg/>", "image/svg+xml")),
                                                   ("files", ("doc-syrova.webp", data, "image/webp"))], headers={"accept": "application/json"})
        assert r.json()["ok"] == ["laser-hero.webp", "doc-syrova.webp"] and r.json()["errors"]
        assert cl.get("/_site/media/laser-hero.webp").content == data
        assert cl.get("/_site/media/..%2F..%2Fetc%2Fpasswd").status_code == 404
        page = cl.get("/_site/laserepilation").text
        assert 'class="hero-ph" src="/_site/media/laser-hero.webp?v=' in page
        assert "/_site/media/doc-syrova.webp" in cl.get("/_site/vydalennia-rodymok").text
        assert "laser-hero.webp" in cl.get("/settings/site").text


def test_upload_key_external():
    import io
    from PIL import Image
    settings.admin_password = "owner-pass-1"
    buf = io.BytesIO(); Image.new("RGB", (30, 30)).save(buf, "WEBP"); data = buf.getvalue()
    with TestClient(app) as cl:
        assert cl.post("/api/public/site/media", files=[("files", ("clinic-1.webp", data, "image/webp"))]).status_code == 403
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        key = cl.post("/settings/site/upload-key").json()["key"]
    with TestClient(app) as cl2:
        r = cl2.post("/api/public/site/media", files=[("files", ("clinic-1.webp", data, "image/webp"))], headers={"x-upload-key": key})
        assert r.json()["ok"] == ["clinic-1.webp"] and r.headers["access-control-allow-origin"] == "*"
        assert cl2.post("/api/public/site/media", files=[("files", ("clinic-1.webp", data, "image/webp"))], headers={"x-upload-key": "bad"}).status_code == 403


def test_seo_meta_and_og_image():
    import io
    from PIL import Image
    settings.admin_password = "owner-pass-1"
    buf = io.BytesIO(); Image.new("RGB", (900, 1200), (120, 90, 160)).save(buf, "WEBP"); data = buf.getvalue()
    with SessionLocal() as db:
        _svc(db, 387, "Консультація Beauty plan Anti Age", 2500); db.commit()
    with TestClient(app) as cl:
        cl.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        cl.post("/settings/site/media", files=[("files", ("bp-hero.webp", data, "image/webp"))], headers={"accept": "application/json"})
        html = cl.get("/_site/beautyplan").text
        assert "Консультація косметолога Київ — Beauty Plan | MOLODIST" in html and "— 2 500 ₴" in html       # жива ціна в описі
        assert 'property="og:image" content="https://' in html and "/og/beautyplan.jpg" in html and "twitter:card" in html
        assert "BreadcrumbList" in html and "OfferCatalog" in html
        r = cl.get("/_site/og/beautyplan.jpg")
        assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (1200, 630)
        assert cl.get("/_site/og/nope.jpg").status_code == 404


def test_site_lead_alert_text():
    from app.services import site_leads as sl
    t = sl.alert_text({"name": "Наталія", "phone": "+380931112233", "interest": "Інʼєкційна косметологія",
                       "page": "https://new.molodist.com.ua/injection?utm_source=google&gclid=x"}, 42, "https://crm.molodist.com.ua/")
    assert "Наталія" in t and "+380931112233" in t
    assert "https://new.molodist.com.ua/injection" in t and "gclid" not in t
    assert t.endswith("https://crm.molodist.com.ua/appeals/42")


def test_site_lead_notify_chat(monkeypatch):
    from app.models import SessionLocal
    from app.services import site_leads as sl, alerts
    sent = []
    monkeypatch.setattr(alerts, "_api", lambda m, **kw: sent.append(("chat", kw["data"]["chat_id"], kw["data"]["text"])) or True)
    monkeypatch.setattr(alerts, "send", lambda t: sent.append(("owner", None, t)) or 1)
    d = {"name": "Іра <b>", "phone": "+380931112233", "page": "https://x/laser"}
    with SessionLocal() as db:
        sl.set_chat(db, None)
        assert sl.notify(db, d, {"ok": True, "appeal_id": 5}, "https://crm")
        assert sent[-1][0] == "owner"
        sl.set_chat(db, "-100123")
        sl.notify(db, d, {"ok": True, "appeal_id": 5}, "https://crm")
        assert sent[-1][:2] == ("chat", "-100123") and "&lt;b&gt;" in sent[-1][2]
        n = len(sent)
        assert not sl.notify(db, d, {"ok": True, "appeal_id": 5, "duplicate": True}, "https://crm")
        assert len(sent) == n
        sl.set_chat(db, None)
