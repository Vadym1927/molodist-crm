from app.main import app
from app.models import SessionLocal, Appeal
from app.services import site_leads as sl, google_ads as ga
from datetime import date, timedelta
from fastapi.testclient import TestClient


def test_site_lead_tilda_and_gtm():
    c = TestClient(app)
    with SessionLocal() as db:
        hook = sl.cfg(db)["hook"]
    assert c.post(f"/api/public/lead/wrong", data={"Name": "x"}).status_code == 404
    assert c.post(f"/api/public/lead/{hook}", data={"test": "test"}).text == "ok"
    r = c.post(f"/api/public/lead/{hook}", data={"Name": "Форма Тест", "Phone": "+38 (050) 000-77-41", "Textarea": "Хочу на консультацію",
                                                "formname": "Запис", "COOKIES": "_ga=GA1.1.1; TILDAUTM=utm_source%3Dgoogle%7C%7C%7Cutm_medium%3Dcpc%7C%7C%7Cutm_campaign%3D%D0%9F%D0%BE%D0%B8%D1%81%D0%BA%20%7C%20%D0%9A%D0%BB%D1%96%D0%BD%D1%96%D0%BA%D0%B0"})
    assert r.text == "ok" and r.headers["access-control-allow-origin"] == "*"
    # той самий телефон зі скрипта GTM (text/plain JSON) — не нове звернення
    r2 = c.post(f"/api/public/lead/{hook}", content='{"name":"Форма Тест","tel":"0500007741","page":"https://molodist.com.ua/?gclid=abc"}', headers={"Content-Type": "text/plain"})
    assert r2.text == "ok"
    with SessionLocal() as db:
        aa = [a for a in db.query(Appeal).all() if a.phone and "".join(ch for ch in a.phone if ch.isdigit()).endswith("0007741")]
        assert len(aa) == 1
        a = aa[0]
        assert a.name == "Форма Тест" and "utm_source=google" in a.utm and "utm_campaign=Поиск | Клініка" in a.utm
        assert "Хочу на консультацію" in (a.comment or "") and a.source == "Сайт · форма"
        ls = ga.leads(db, date.today() - timedelta(days=1), date.today())
        assert any(l["phone"] == a.phone and l["campaign"] == "Поиск | Клініка" for l in ls)
        assert "__HOOK__" not in sl.gtm_tag("https://x/l") and "https://x/l" in sl.gtm_tag("https://x/l")


def test_shop_cabinet_login_is_not_appeal():
    with SessionLocal() as db:
        r = sl.ingest(db, {"phone": "+380501112299", "form": "cab-login", "code": "75874", "page": "https://molodist.store/cabinet/"})
        assert r.get("skipped")
        r = sl.ingest(db, {"phone": "+380501112298", "name": "X", "page": "https://molodist.store/checkout/?a=1"})
        assert r.get("skipped")
        assert not [a for a in db.query(Appeal).all() if a.phone and a.phone.endswith(("1112299", "1112298"))]
