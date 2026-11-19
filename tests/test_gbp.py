import json

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.models import SessionLocal, SyncState


def _http(log):
    def h(req: httpx.Request):
        log.append((req.method, str(req.url)))
        u = str(req.url)
        if "oauth2.googleapis.com/token" in u:
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT-secret-refresh-xyz", "expires_in": 3600})
        if u.startswith("https://mybusinessaccountmanagement"):
            return httpx.Response(200, json={"accounts": [{"name": "accounts/1"}]})
        if "/locations" in u and "mybusinessbusinessinformation" in u:
            return httpx.Response(200, json={"locations": [{"name": "locations/9", "title": "MOLODIST"}]})
        if req.method == "GET" and u.startswith("https://mybusiness.googleapis.com/v4/accounts/1/locations/9/reviews"):
            return httpx.Response(200, json={"averageRating": 4.8, "totalReviewCount": 2, "reviews": [
                {"reviewId": "r1", "reviewer": {"displayName": "Олена"}, "starRating": "FIVE", "comment": "Дякую!", "createTime": "2026-11-01T10:00:00Z"},
                {"reviewId": "r2", "reviewer": {"displayName": "Іра"}, "starRating": "THREE", "createTime": "2026-11-02T10:00:00Z",
                 "reviewReply": {"comment": "Дякуємо", "updateTime": "2026-11-03T10:00:00Z"}}]})
        if req.method == "PUT" and u.endswith("/reviews/r1/reply"):
            assert json.loads(req.content)["comment"] == "Дякуємо за довіру!"
            return httpx.Response(200, json={"comment": "Дякуємо за довіру!"})
        return httpx.Response(404, json={"error": {"message": "nf"}})
    return httpx.Client(transport=httpx.MockTransport(h))


def test_gbp_flow():
    from app.integrations import gbp
    log = []
    with TestClient(app):
        with SessionLocal() as db:
            gbp.set_client(db, "cid.apps.googleusercontent.com", "GOCSPX-secret")
            raw = db.get(SyncState, gbp.KEY).value
            assert "GOCSPX-secret" not in raw and gbp.status(gbp.cfg(db))["client"]
            url = gbp.auth_url(db, "https://crm/cb")
            assert "business.manage" in url and "access_type=offline" in url
            st = gbp.cfg(db)["state"]
            try:
                gbp.callback(db, "code", "bad", "https://crm/cb", _http(log))
                assert False
            except gbp.GbpError:
                pass
            s = gbp.callback(db, "code", st, "https://crm/cb", _http(log))
            assert s["connected"] and s["title"] == "MOLODIST" and "RT-secret-refresh-xyz" not in db.get(SyncState, gbp.KEY).value
            rows = gbp.reviews(db, _http(log))
            assert [r["stars"] for r in rows] == [5, 3] and rows[1]["reply"] == "Дякуємо" and gbp.cfg(db)["avg"] == 4.8
            gbp.reply(db, "r1", "Дякуємо за довіру!", _http(log))
            assert ("PUT", "https://mybusiness.googleapis.com/v4/accounts/1/locations/9/reviews/r1/reply") in log
            gbp.disconnect(db)
            assert not gbp.status(gbp.cfg(db))["connected"]


def test_gbp_page():
    from app.config import settings
    from app.integrations import gbp
    settings.admin_password = "owner-pass-g"
    with TestClient(app) as c:
        with SessionLocal() as db:
            gbp.disconnect(db)
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-g"})
        h = c.get("/settings/google-profile")
        assert h.status_code == 200 and "/settings/google-profile/callback" in h.text
        r = c.post("/settings/google-profile", data={"client_id": "x.apps.googleusercontent.com", "client_secret": "GOCSPX-zz"}, follow_redirects=False)
        assert r.status_code == 303
        h = c.get("/settings/google-profile")
        assert "GOCSPX-zz" not in h.text and "Підключити Google" in h.text
        r = c.get("/settings/google-profile/connect", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
        assert c.get("/settings/site").status_code == 200
