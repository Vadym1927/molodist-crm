"""Сторінка болю «Лікування акне й постакне» на дизайні нової головної."""
from fastapi.testclient import TestClient

from app.main import app
from app.services import site_landing as sl


def test_acne_landing_renders():
    with TestClient(app) as cl:
        r = cl.get("/_site/likuvannya-akne")
        t = r.text
        assert r.status_code == 200
        assert "<h1>Лікування акне й постакне в Києві</h1>" in t
        assert 'data-topic="Акне й постакне"' in t and 'data-mode="lp"' in t
        for sec in ('id="yak-likuiemo"', 'id="konsultatsiia"', 'id="rezultaty"', 'id="zapys"', 'id="zapytannia"', 'id="kontakty"'):
            assert sec in t, sec
        assert "BEAUTY PLAN Anti Acne" in t and '"MedicalWebPage"' in t and '"FAQPage"' in t
        assert 'id="prostir"' not in t and "h-proto" not in t                 # простору немає, смужки прототипу немає
        low = t.lower()
        assert "ботокс" not in low and "салон" not in low and "клієнт" not in low
        assert cl.get("/_site/og/likuvannya-akne.jpg").status_code == 200
        h = cl.get("/_site/proto/a").text                                     # картка «Акне» на головній веде на сторінку
        assert '/_site/likuvannya-akne"' in h


def test_acne_faq_prices_filled():
    lp = sl.ACNE
    assert len(lp["faq"]) >= 8 and all(q.endswith("?") for q, _ in lp["faq"])
    assert len(lp["seo"]["desc"]) <= 160


def test_ig_assets_route_guards():
    with TestClient(app) as cl:
        assert cl.get("/_site/ig/../etc.jpg").status_code == 404
        assert cl.get("/_site/ig/akne-reel-1.mp4").status_code == 404


def test_ig_assets_refresh_picks_reel_and_cases(tmp_path, monkeypatch):
    from app.config import settings
    from app.models import SessionLocal
    from app.services import site_ig_assets as sia
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    data = [{"id": "1", "media_type": "VIDEO", "media_url": "v", "thumbnail_url": "t", "permalink": "https://www.instagram.com/reel/x/",
             "caption": "На появу акне впливають одразу чотири фактори"},
            {"id": "2", "media_type": "CAROUSEL_ALBUM", "permalink": "https://www.instagram.com/p/y/", "caption": "Акне: результат до і після",
             "children": {"data": [{"media_type": "IMAGE", "media_url": "a"}, {"media_type": "IMAGE", "media_url": "b"}]}},
            {"id": "3", "media_type": "IMAGE", "media_url": "c", "permalink": "https://www.instagram.com/p/z/", "caption": "RF-ліфтинг"}]
    monkeypatch.setattr(sia, "_list", lambda db, http, limit=60: data)
    monkeypatch.setattr(sia, "_video", lambda http, url, p: p.write_bytes(b"x" * 20000) or True)
    monkeypatch.setattr(sia, "_img", lambda http, url, p, max_side=1080: p.write_bytes(b"jpg") or True)
    with SessionLocal() as db:
        r = sia.refresh(db, "akne", http=object())
        assert r["reel"]["file"] == "akne-reel-1.mp4" and len(r["cases"]) == 2
        a = sia.assets(db, "akne", "")
        assert a["reel"]["src"] == "/ig/akne-reel-1.mp4" and len(a["ig_cases"]) == 2
