"""Керування рекламою з погодженням: сміттєві запити → пропозиції → погодження власником → черга скрипта → результат."""
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import SessionLocal, AdsChange
from app.services import google_ads as ga, ads_changes as ac


def _login(c):
    settings.admin_password = "owner-pass-1"
    c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})


def test_analyze_decide_queue_applied():
    d = (date.today() - timedelta(days=2)).isoformat()
    camp = "Тест Чистка | Пошук"
    with SessionLocal() as db:
        ga.ingest(db, {"rows": [["campaign", d, "9901", camp, "", "ENABLED", 50, 900, 3000.0, 1, 0],
                                ["term", d, "курси косметолога київ", "курси косметолога київ", camp, "", 3, 40, 150.0, 0, 0],
                                ["term", d, "курс косметолога онлайн", "курс косметолога онлайн", camp, "", 2, 20, 90.0, 0, 0],
                                ["term", d, "мезотерапія волосся відгуки", "мезотерапія волосся відгуки", camp, "", 8, 60, 700.0, 0, 0],
                                ["term", d, "ботокс київ ціна", "ботокс київ ціна", camp, "", 20, 200, 900.0, 2, 0]]})
        new = ac.analyze(db)
        mine = [x for x in new if x.campaign == camp]
        tg = {(x.target, (x.payload or {}).get("match")) for x in mine}
        assert ("мезотерапія волосся відгуки", "EXACT") in tg            # дорогий без конверсій
        assert any(m == "PHRASE" and t.startswith("курс") for t, m in tg)  # навчання — сміття
        assert not any("ботокс" in t for t, _ in tg)                      # з конверсіями — не чіпаємо
        assert all(x.campaign_id == "9901" for x in mine)
        assert not [x for x in ac.analyze(db) if x.campaign == camp]      # повторно не дублює
        ex = next(x for x in mine if x.payload["match"] == "EXACT")
        assert "[мезотерапія волосся відгуки]" in ac.describe(ex)
        kb = ac.tg_kb([ex])
        assert kb["inline_keyboard"][0][0]["callback_data"] == f"ads:ok:{ex.id}"
        assert ac.decide(db, ex, True, None) and not ac.decide(db, ex, False, None)
        q = [x for x in ac.queue(db) if x["id"] == ex.id]
        assert q and q[0]["match"] == "EXACT" and q[0]["campaign_id"] == "9901"
        assert ac.applied(db, [{"id": ex.id, "ok": True, "note": "додано"}]) == 1
        db.refresh(ex)
        assert ex.status == "applied" and not [x for x in ac.queue(db) if x["id"] == ex.id]


def test_endpoints_owner_and_script():
    settings.admin_password = "owner-pass-1"
    with SessionLocal() as db:
        ch = ac.propose(db, "pause_campaign", "Тест Ендпоінт", None, "перевірка", author="claude")
        db.commit()
        cid, hook = ch.id, ga.cfg(db)["hook"]
    with TestClient(app) as c:
        assert c.get("/api/public/ads/google/wrong/queue").status_code == 404
        _login(c)
        r = c.post(f"/marketing/google/changes/{cid}", data={"act": "ok"}, follow_redirects=False)
        assert r.status_code == 303 and "changes" in r.headers["location"], r.headers.get("location")
        q = c.get(f"/api/public/ads/google/{hook}/queue").json()["changes"]
        assert any(x["id"] == cid and x["kind"] == "pause_campaign" for x in q)
        assert c.post(f"/api/public/ads/google/{hook}/applied", json={"results": [{"id": cid, "ok": False, "note": "кампанію не знайдено"}]}).json()["n"] == 1
        page = c.get("/marketing/google")
        assert page.status_code == 200 and 'id="changes"' in page.text
    with SessionLocal() as db:
        x = db.get(AdsChange, cid)
        assert x.status == "failed" and "не знайдено" in x.result
    s = ga.script("https://x/api/public/ads/google/h")
    assert "applyQueue()" in s and "createNegativeKeyword" in s and "/applied" in s


def test_broad_to_phrase_and_rsa_replace():
    with SessionLocal() as db:
        ch = ac.propose(db, "broad_to_phrase", "Тест Широкі", None, "широка → фразова", campaign_id="777")
        db.commit()
        assert "фразова" in ac.describe(ch)
    s = ga.script("https://x/h")
    assert "broad_to_phrase" in s and "pause_text" in s and "match_type = BROAD" in s


def test_loader_and_remove_negative():
    from fastapi.testclient import TestClient as _TC
    with SessionLocal() as db:
        hook = ga.cfg(db)["hook"]
        ch = ac.propose(db, "remove_negative", "Тест Мінус", "днепровский", "район клініки")
        db.commit()
        assert "прибрати мінус" in ac.describe(ch)
        assert ac.propose(db, "remove_negative", "Тест Мінус", "днепровский", "ще раз") is None
        assert ac.propose(db, "remove_negative", "Тест Мінус", "днепровский", "ще раз", force=True) is not None
    with _TC(app) as c:
        body = c.get(f"/api/public/ads/google/{hook}/script").text
        assert "function run()" in body and "function main()" not in body and hook in body
        assert c.get("/api/public/ads/google/nope/script").status_code == 404
    assert "eval(" in ga.loader("https://x/h") and "remove_negative" in ga.SCRIPT


def test_new_campaign_kind_and_export():
    from datetime import date as _d
    with SessionLocal() as db:
        ch = ac.propose(db, "new_campaign", "Лазер | Тест", None, "нова сторінка", campaign_id=None,
                        payload={"budget": 300, "copy_from_id": "20537117017", "copy_from": "Поиск | Клініка", "cpc_max": 40, "negatives": ["курси"],
                                 "groups": [{"name": "Лазер", "final_url": "https://molodist.com.ua/laserepilation", "keywords": [["лазерна епіляція київ", "PHRASE"]],
                                             "headlines": ["Лазерна епіляція"] * 3, "descriptions": ["Опис"] * 2}]})
        db.commit()
        assert "на паузі" in ac.describe(ch) and "ключів 1" in ac.describe(ch)
        ga.ingest(db, {"rows": [["keyword", _d.today().isoformat(), "1~2", "косметолог київ", "Поиск | Клініка", "ENABLED", 10, 100, 170.0, 1, 0]]})
    s = ga.script("https://x/h")
    assert "function createCampaign" in s and "AdsApp.mutateAll" in s and "new_campaign" in s
    with TestClient(app) as c:
        _login(c)
        r = c.get("/marketing/google/export.csv?level=keyword")
        assert r.status_code == 200 and "косметолог київ" in r.text


def test_no_clinic_word_in_ad_texts():
    import pytest
    with SessionLocal() as db:
        with pytest.raises(ValueError):
            ac.propose(db, "rsa", "Тест Слова", "x", "тест", payload={"headlines": ["Клініка MOLODIST"], "descriptions": ["опис"]})
        ch = ac.propose(db, "rsa", "Тест Слова", "y", "тест", payload={"headlines": ["Медичний простір MOLODIST"], "descriptions": ["опис"]})
        db.commit()
        with pytest.raises(ValueError):
            ac.edit(db, ch, {"headlines": ["у медичній клініці"], "descriptions": ["x"]})
        assert ac.edit(db, ch, {"headlines": ["Медичний простір"], "descriptions": ["x"]})


def test_no_drug_names_in_ad_texts():
    import pytest
    with SessionLocal() as db:
        with pytest.raises(ValueError):
            ac.propose(db, "rsa", "Тест Препарати", "x", "тест", payload={"headlines": ["Botox, Xeomin, Dysport"], "descriptions": ["опис"]})
