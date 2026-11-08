"""Копія в Dropbox: PKCE-підключення, шифрування, ротація, докопіювання файлів, сторінка «Дані»."""
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import SessionLocal, SyncState
from app.services import dropbox_backup as dbx


class FakeDropbox:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.sessions: dict[str, bytes] = {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if u == dbx.TOKEN:
            form = parse_qs(req.content.decode())
            if form["grant_type"][0] == "authorization_code":
                assert form["code_verifier"][0] and form["client_id"][0] == "appkey123"
                return httpx.Response(200, json={"refresh_token": "R1", "account_id": "acc"})
            return httpx.Response(200, json={"access_token": "A1", "expires_in": 14400})
        assert req.headers.get("authorization") == "Bearer A1"
        ep = u.split("/2/", 1)[1]
        if ep == "users/get_current_account":
            return httpx.Response(200, json={"email": "owner@example.com"})
        if ep == "files/upload":
            arg = json.loads(req.headers["dropbox-api-arg"])
            self.files[arg["path"].lower()] = req.content
            return httpx.Response(200, json={"path_lower": arg["path"].lower()})
        if ep == "files/list_folder":
            path = json.loads(req.content)["path"].lower()
            ents = [{".tag": "file", "name": p.rsplit("/", 1)[1], "path_lower": p} for p in self.files if p.rsplit("/", 1)[0] == path]
            return httpx.Response(200, json={"entries": ents, "has_more": False})
        if ep == "files/delete_v2":
            self.files.pop(json.loads(req.content)["path"], None)
            return httpx.Response(200, json={})
        return httpx.Response(404, text=ep)


def test_dropbox_flow(tmp_path, monkeypatch):
    fake = FakeDropbox()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    files = tmp_path / "files"
    (files / "p1").mkdir(parents=True)
    (files / "p1" / "plan.pdf").write_bytes(b"%PDF-1 test")
    monkeypatch.setattr(settings, "files_dir", str(files))
    monkeypatch.setattr(dbx, "KEEP", 2)
    with SessionLocal() as db:
        db.execute(SyncState.__table__.delete().where(SyncState.key == dbx.KEY)); db.commit()
        url = dbx.start_auth(db, "appkey123")
        q = parse_qs(urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and q["token_access_type"] == ["offline"]
        try:
            dbx.finish_auth(db, "code", "wrong-state", http)
            assert False
        except dbx.DropboxError:
            pass
        d = dbx.finish_auth(db, "code", q["state"][0], http)
        assert dbx.connected(d) and d["account"] == "owner@example.com" and not dbx.ready(d)
        assert "R1" not in json.dumps(dbx.cfg(db))                    # токен зберігається зашифрованим
        src = tmp_path / "MOLODIST_CRM_backup_2026-11-08_030000.zip"
        src.write_bytes(b"zipdata")
        assert dbx.run(db, src, http)["status"] == "немає пароля"
        try:
            dbx.set_password(db, "short")
            assert False
        except dbx.DropboxError:
            pass
        dbx.set_password(db, "very-long-password-1")
        rep = dbx.run(db, src, http)
        assert rep["status"] == "ok" and rep["files"]["uploaded"] == 1
        raw = fake.files["/db/" + src.name.lower() + ".enc"]
        assert raw != b"zipdata" and dbx.decrypt_bytes(raw, "very-long-password-1") == b"zipdata"
        assert dbx.decrypt_bytes(fake.files["/files/p1/plan.pdf.enc"], "very-long-password-1") == b"%PDF-1 test"
        rep = dbx.run(db, src, http)                                   # повторно — файли вже є
        assert rep["files"]["uploaded"] == 0
        for n in ("2026-11-09", "2026-11-10"):
            s2 = tmp_path / f"MOLODIST_CRM_backup_{n}_030000.zip"
            s2.write_bytes(b"z")
            dbx.run(db, s2, http, with_files=False)
        assert len([p for p in fake.files if p.startswith("/db/")]) == 2   # ротація
        assert dbx.status(db)["last"]["ok"]
    settings.admin_password = "owner-pass-1"
    with TestClient(app) as c:
        c.post("/login", data={"login": settings.owner_login, "password": "owner-pass-1"})
        t = c.get("/data").text
        assert "Копія в Dropbox" in t and "підключено" in t and "Вивантажити зараз" in t
        r = c.post("/settings/dropbox/password", data={"password": "a" * 12, "password2": "b" * 12}, follow_redirects=False)
        assert "msg=" in r.headers["location"] or "err" in r.headers["location"]
        r = c.post("/settings/dropbox/disconnect", follow_redirects=False)
        assert r.status_code == 303
        t = c.get("/data").text
        assert "Підключити Dropbox" in t and "/settings/dropbox/callback" in t


def test_dropbox_retry_on_429(monkeypatch):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url) == dbx.TOKEN:
            return httpx.Response(200, json={"access_token": "A1", "expires_in": 14400})
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, json={"error": {"reason": {".tag": "too_many_write_operations"}, "retry_after": 1}})
        return httpx.Response(200, json={"path_lower": "/db/x"})

    slept = []
    monkeypatch.setattr(dbx, "SLEEP", lambda s: slept.append(s))
    c = dbx.Client({"app_key": "k", "secrets": {}}, httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(dbx, "secret", lambda d, n: "R")
    assert c.upload(b"data", "/db/x")["path_lower"] == "/db/x"
    assert calls["n"] == 3 and len(slept) == 2
