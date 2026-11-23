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
            {"id": "3", "media_type": "IMAGE", "media_url": "c", "permalink": "https://www.instagram.com/p/z/", "caption": "RF-ліфтинг"},
            {"id": "4", "media_type": "VIDEO", "media_url": "v2", "permalink": "https://www.instagram.com/reel/w/",
             "caption": "Давно лікуєте акне, але воно постійно повертається?"}]
    monkeypatch.setattr(sia, "_list", lambda db, http, limit=60: data)
    monkeypatch.setattr(sia, "_video", lambda http, url, p: p.write_bytes(b"x" * 20000) or True)
    monkeypatch.setattr(sia, "_img", lambda http, url, p, max_side=1080: p.write_bytes(b"jpg") or True)
    with SessionLocal() as db:
        r = sia.refresh(db, "akne", http=object())
        assert r["reel"]["file"] == "akne-reel-1.mp4" and len(r["cases"]) == 2
        a = sia.assets(db, "akne", "")
        assert a["reel"]["src"] == "/ig/akne-reel-1.mp4" and len(a["ig_cases"]) == 2
        assert r["reel2"]["file"] == "akne-reel-4.mp4" and a["reel2"]["src"] == "/ig/akne-reel-4.mp4"
    with TestClient(app) as cl:
        t = cl.get("/_site/likuvannya-akne").text
        assert 'id="video-rezultat"' in t and "/ig/akne-reel-4.mp4" in t and "результат за 3 місяці" in t


def test_ig_asset_served(tmp_path, monkeypatch):
    from app.config import settings
    from app.services import site_ig_assets as sia
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    (sia.folder() / "akne-ig-1-1.jpg").write_bytes(b"\xff\xd8jpg")
    with TestClient(app) as cl:
        r = cl.get("/_site/ig/akne-ig-1-1.jpg")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_site_events_report():
    from datetime import date
    from app.models import SessionLocal
    from app.services import site_stats as ss
    with TestClient(app) as cl:
        body = {"sid": "abc123", "page": "/_site/likuvannya-akne", "gclid": "x", "utm_campaign": "Акне",
                "ev": [{"k": "view", "t": "t"}, {"k": "sec", "t": "rezultaty", "v": 40}, {"k": "click", "t": "Акне й постакне · lhero"},
                       {"k": "form", "t": "m-lp"}, {"k": "exit", "t": "rezultaty", "v": 35}, {"k": "bad", "t": "x"}]}
        import json
        r = cl.post("/_site/api/ev", content=json.dumps(body), headers={"user-agent": "Mozilla/5.0 (iPhone) Mobile"})
        assert r.json()["n"] == 5
    with SessionLocal() as db:
        rep = ss.report(db, date.today())
        p = next(x for x in rep["pages"] if x["page"] == "/likuvannya-akne")
        assert p["paid"] >= 1 and p["forms"] >= 1 and ("Результати", 1) in [(k, v) for k, v in p["exits"]] or p["exits"]
        assert "Сайт за" in ss.text(rep)


def test_pigment_landing_redirects_and_gone():
    """Сторінка «Пігментація й судини», 301 зі старих сторінок IPL, 410 для сторінок лікарів, які вже не працюють."""
    with TestClient(app) as client:
        r = client.get("/_site/likuvannya-pigmentatsii")
        assert r.status_code == 200
        assert "Лікування пігментації та судин у Києві" in r.text and "IPL-терапія: одна технологія" in r.text
        assert "клінік" not in r.text.lower().split("<main")[1].split("</main>")[0]
        assert "track.js" in r.text
        for old in ("ipl-therapy", "fotoomolodzhennya-ipl", "lazerne-vydalennya-sudyn", "likuvannya-sudin"):
            x = client.get(f"/_site/{old}", follow_redirects=False)
            assert x.status_code == 301 and x.headers["location"].endswith("/likuvannya-pigmentatsii")
        assert client.get("/_site/tatianabondarenko").status_code == 410
        assert client.get("/_site/crop/nope.webp").status_code == 404
        assert "likuvannya-pigmentatsii" in client.get("/_site/sitemap.xml").text


def test_tilda_strip_gone_staff():
    from app.services import tilda_site as ts
    h = ('<ul><li class="t-menu__list_item"><a class="t-menu__link-item" href="/tatianabondarenko">ТЕТЯНА БОНДАРЕНКО</a></li>'
         '<li><a href="/daryakolgan">ДАРʼЯ КОЛГАН</a></li><li><a href="https://molodist.com.ua/dmytrolevenchuk">ДМИТРО ЛЕВЕНЧУК</a></li></ul>'
         '<a href="#x">Анастасія Лозинська</a>')
    out = ts.strip_gone(h)
    assert "БОНДАРЕНКО" not in out and "ЛЕВЕНЧУК" not in out and "Лозинська" not in out and "КОЛГАН" in out


def test_own_404_page():
    with TestClient(app) as cl:
        r = cl.get("/_site/nema-takoi-storinky")
        assert r.status_code == 404 and "Такої сторінки немає" in r.text and "Tilda" not in r.text


def test_laser_landing():
    with TestClient(app) as cl:
        r = cl.get("/_site/laserepilation")
        assert r.status_code == 200 and "Лазерна епіляція в Києві" in r.text and "Що змінюється після курсу" in r.text
        assert "Які зони вас цікавлять" in r.text and "h-modal" in r.text
        assert "клінік" not in r.text.lower().split("<main")[1].split("</main>")[0]
        x = cl.get("/_site/consultation-lazerna-epilyatsiya", follow_redirects=False)
        assert x.status_code == 301 and x.headers["location"].endswith("/laserepilation")
        assert cl.get("/_site/sitemap.xml").text.count("/laserepilation<") == 1


def test_robots_ai_bots_and_llms_txt():
    with TestClient(app) as cl:
        r = cl.get("/_site/robots.txt").text
        assert "GPTBot" in r and "ClaudeBot" in r and "PerplexityBot" in r and "llms.txt" in r and "Disallow: /\n" not in r
        t = cl.get("/_site/llms.txt")
        assert t.status_code == 200 and t.text.startswith("# MOLODIST") and "/likuvannya-akne" in t.text and "/laserepilation" in t.text
        assert "клінік" not in t.text.lower()


def test_lead_session_path():
    from datetime import date
    from app.models import SessionLocal
    from app.services import site_stats as ss
    with SessionLocal() as db:
        ss.ingest(db, {"sid": "leadpath1", "page": "/", "ev": [{"k": "view", "t": "x"}, {"k": "sec", "t": "likari", "v": 40},
                                                             {"k": "click", "t": "Записатися · likari"}, {"k": "lead", "t": "modal"}]}, "Mozilla/5.0 (Windows NT 10.0)")
        r = [x for x in ss.lead_sessions(db, date.today()) if x["sid"] == "leadpath"]
        assert r and [e["kind"] for e in r[0]["rows"]] == ["view", "sec", "click", "lead"] and r[0]["rows"][1]["t"].startswith("Лікарі")


def test_skin_landing():
    with TestClient(app) as cl:
        r = cl.get("/_site/tmyana-znevodnena-shkira")
        assert r.status_code == 200 and "Тьмяна, зневоднена шкіра: відновлення в Києві" in r.text and "Що повертає шкірі свіжість" in r.text
        assert "клінік" not in r.text.lower().split("<main")[1].split("</main>")[0]
        assert "/tmyana-znevodnena-shkira" in cl.get("/_site/proto/a").text


def test_skin_landing_photos():
    """Сторінка «якість шкіри»: методи з фото, блоку «до / після» немає."""
    with TestClient(app) as cl:
        t = cl.get("/_site/tmyana-znevodnena-shkira").text
        assert "sk-glow-1.webp" in t and t.count("l-case") >= 0
        assert t.count('class="l-acard pic') == 5 and "sk-home.webp" in t and "f-cosm.webp" in t


def test_skin_landing_reel_process_pics_modal():
    with TestClient(app) as cl:
        t = cl.get("/_site/tmyana-znevodnena-shkira").text
        assert "skin-reel.mp4" in t and "Ефект сяючої шкіри за 2 місяці" in t
        assert 'class="ic pic"' in t and 'class="l-cimg"' in t and 'class="h-mside"' in t


def test_skin_landing_seo_video_og():
    import json, re
    with TestClient(app) as cl:
        t = cl.get("/_site/tmyana-znevodnena-shkira").text
        assert "sk-modal.webp" in t and "og-skin" not in t
        ld = [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', t, re.S)]
        v = [g for d in ld for g in d.get("@graph", []) if g.get("@type") == "VideoObject"]
        assert v and v[0]["uploadDate"] == "2026-11-16" and "skin-reel.mp4" in v[0]["contentUrl"]
        assert cl.get("/_site/og/tmyana-znevodnena-shkira.jpg").status_code == 200


def test_home_bp_reel_from_instagram(tmp_path, monkeypatch):
    from app.config import settings
    from app.models import SessionLocal
    from app.services import site_ig_assets as sia
    monkeypatch.setattr(settings, "files_dir", str(tmp_path))
    data = [{"id": "7", "media_type": "VIDEO", "media_url": "v", "permalink": "https://www.instagram.com/reel/a/", "caption": "Консультація Beauty Plan Anti Acne"},
            {"id": "8", "media_type": "VIDEO", "media_url": "v", "permalink": "https://www.instagram.com/reel/b/", "caption": "Що таке BEAUTY PLAN у MOLODIST"}]
    monkeypatch.setattr(sia, "_list", lambda db, http, limit=60: data)
    monkeypatch.setattr(sia, "_video", lambda http, url, p: p.write_bytes(b"x" * 20000) or True)
    with SessionLocal() as db:
        r = sia.refresh(db, "bp", http=object())
        assert r["reel"]["file"] == "bp-reel-8.mp4"
        assert sia.assets(db, "bp", "")["reel"] is None          # спершу приховано
        st = sia.state(db); st["bp"]["reel_on"] = True; sia._save(db, st)
        assert sia.assets(db, "bp", "")["reel"]["src"] == "/ig/bp-reel-8.mp4"


def test_wrinkles_landing():
    with TestClient(app) as cl:
        r = cl.get("/_site/zmorshky-ta-vikovi-zminy")
        assert r.status_code == 200 and "Зморшки й вікові зміни: корекція в Києві" in r.text and "Що допомагає" in r.text
        main = r.text.lower().split("<main")[1].split("</main>")[0] if "<main" in r.text else r.text.lower()
        assert "клінік" not in main and "botox" not in main and "ботокс" not in main and "xeomin" not in main and "dysport" not in main
        assert "/zmorshky-ta-vikovi-zminy" in cl.get("/_site/proto/a").text
        assert cl.get("/_site/og/zmorshky-ta-vikovi-zminy.jpg").status_code == 200
