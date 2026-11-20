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
