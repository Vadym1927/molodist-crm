"""Прототип нової головної сайту: варіанти A–D, спільна обвʼязка, живі ціни з CRM, без індексації, без слова «клініка»."""
import re

from fastapi.testclient import TestClient

from app.main import app
from app.models import SessionLocal, CbService
from app.services import site_home as sh


def test_proto_variants_render_brand_rules():
    with SessionLocal() as db:
        s = db.get(CbService, 387) or CbService(id=387, name="BEAUTY PLAN Anti Age")
        s.price, s.active = 1900, True
        db.merge(s); db.commit()
    with TestClient(app) as cl:
        r = cl.get("/_site/proto")
        assert r.status_code == 200 and all(f"/_site/proto/{k}" in r.text for k in sh.VARIANTS)
        assert cl.get("/_site/proto/zzz").status_code == 404
        for k, v in sh.VARIANTS.items():
            r = cl.get(f"/_site/proto/{k}")
            assert r.status_code == 200 and r.headers.get("x-robots-tag") and 'content="noindex' in r.text
            t = r.text
            assert v["h1"] in t and "1 900 ₴" in t                                   # ціна BEAUTY PLAN — жива з прайсу
            for anchor in ("beauty-plan", "likari", "vidhuky", "zapytannia", "kontakty", "zapys"):
                assert f'id="{anchor}"' in t, (k, anchor)
            assert 'id="h-menu"' in t and 'class="h-up"' in t and "data-fab" in t and "maps.google.com/maps" in t
            assert 'data-lead="home-' in t and "Я знаю процедуру" in t
            if k == "a":                                                             # основний варіант: усе веде на форму, онлайн-запис — лише під лікарями
                assert "/zapys" not in t.replace("/zapys?doctor=", "") and 'data-pick="topic"' in t and "Масаж" in t
                assert "ФОП Бондар Вадим Миколайович" in t and "FAQPage" in t and "bp.mp4" in t and "ba-wrinkles-1.webp" in t
                assert 'class="pulse"' in t and "h-rsum" in t and "h-igrid" in t and "h-shop2" in t
                assert "<h1>Естетична медицина й косметологія в Києві</h1>" in t and "Шкіра, якій не потрібні фільтри" in t
                assert "ЄДРПОУ 3457310591" in t and "data-need-name" in t and "ba-acne-1.webp" in t and "h-faq2" in t and "h-flow" in t
            body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", t, flags=re.S)
            body = re.sub(r"<[^>]+>", " ", body).lower()                             # лише видимий текст
            assert "клінік" not in body and "клієнт" not in body and "ботокс" not in body and "салон" not in body, k


def test_proto_texts_brandbook():
    blob = " ".join(str(x) for x in (sh.VARIANTS, sh.FACTS, sh.CONCERNS, sh.DIRECTIONS, sh.PLAN, sh.SPACE, sh.SHOP, sh.INSTA, sh.FAQ, sh.SEO, sh.MENU)).lower()
    for bad in ("клінік", "клієнт", "ботокс", "салон", "майстер"):
        assert bad not in blob
    assert "Molodist" not in " ".join(str(x) for x in (sh.VARIANTS, sh.FACTS, sh.PLAN, sh.FAQ, sh.SEO))


def test_new_home_as_site_home():
    """Нова головна як основна сторінка сайту: індексується на домені сайту, має картку для месенджерів і розмітку."""
    from app.services import tilda_site as ts, site_pages as sp
    with SessionLocal() as db:
        c = ts.cfg(db); old = c.get("home"); c["home"] = "new"; ts.save(db, c)
        host = sorted(sp.cfg(db)["hosts"])[0]
    sp._cache["at"] = 0
    try:
        with TestClient(app) as cl:
            r = cl.get("/", headers={"host": host})
            t = r.text
            assert r.status_code == 200 and "Естетична медицина й косметологія в Києві" in t
            assert 'rel="canonical"' in t and 'name="robots"' not in t and "h-proto" not in t
            assert 'property="og:title" content="MOLODIST — медичний простір естетики та профілактики"' in t
            assert "googletagmanager.com/gtm.js" in t           # аналітика, як на інших сторінках сайту
            assert "og/home.jpg" in t and '"MedicalClinic"' in t and '"FAQPage"' in t
            assert t.count("s-") >= 10 and "data-car" in t
            assert cl.get("/og/home.jpg", headers={"host": host}).status_code == 200
            assert f"<loc>{sp.cfg(SessionLocal())['canonical'].rstrip('/')}/</loc>" in cl.get("/sitemap.xml", headers={"host": host}).text
            p = cl.get("/_site/proto/a").text                       # перегляд у CRM лишається без індексації
            assert 'name="robots" content="noindex' in p and "h-proto" in p and "googletagmanager" not in p
    finally:
        with SessionLocal() as db:
            c = ts.cfg(db); c["home"] = old or "tilda"; ts.save(db, c)
