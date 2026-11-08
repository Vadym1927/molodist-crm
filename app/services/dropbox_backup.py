"""Копія поза сервером — у Dropbox власника, окрема папка застосунку («Програми / <назва застосунку>»).

Підключення (один раз, власник): створює застосунок у Dropbox (доступ «App folder»), вставляє App key у CRM,
натискає «Підключити Dropbox» → дозвіл у Dropbox → CRM отримує refresh-токен (PKCE, секрет застосунку не потрібен).
Токен і пароль шифрування зберігаються в CRM зашифрованими й ніде не показуються.

Щоночі після локальної копії:
  /db/MOLODIST_CRM_backup_….zip.enc — база, зашифрована паролем (відновлення: «Дані → Відновити з копії» + пароль);
  /files/… .enc — фото й PDF, докопіюються лише нові/змінені (кожен файл зашифровано тим самим паролем);
зберігаються KEEP останніх копій бази. Збій → сповіщення власнику."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import SyncState

log = logging.getLogger("dropbox_backup")
KEY = "dropbox_backup"
AUTH = "https://www.dropbox.com/oauth2/authorize"
TOKEN = "https://api.dropboxapi.com/oauth2/token"
API = "https://api.dropboxapi.com/2"
CONTENT = "https://content.dropboxapi.com/2"
KEEP = 30
CHUNK = 8 * 1024 * 1024
SIMPLE_LIMIT = 140 * 1024 * 1024
FILES_BUDGET_S = 20 * 60          # за ніч на фото/PDF — не довше 20 хв, решта — наступної ночі
MIN_PASSWORD = 10


class DropboxError(RuntimeError):
    pass


# ---------------- налаштування ----------------
def _cipher():
    from ..integrations.cleverbox import _token_cipher
    return _token_cipher()


def cfg(db: Session) -> dict:
    st = db.get(SyncState, KEY)
    try:
        d = json.loads(st.value) if st and st.value else {}
    except ValueError:
        d = {}
    d.setdefault("app_key", "")
    d.setdefault("secrets", {})
    d.setdefault("files", {})
    d.setdefault("last", {})
    return d


def save(db: Session, d: dict) -> None:
    st = db.get(SyncState, KEY) or SyncState(key=KEY)
    st.value, st.updated_at = json.dumps(d, ensure_ascii=False, default=str), datetime.utcnow()
    db.merge(st)
    db.commit()


def set_secret(d: dict, name: str, value: str) -> None:
    d["secrets"][name] = _cipher().encrypt(value.encode()).decode()


def secret(d: dict, name: str) -> str:
    v = d["secrets"].get(name)
    if not v:
        return ""
    try:
        return _cipher().decrypt(v.encode()).decode()
    except Exception:  # noqa: BLE001
        return ""


def connected(d: dict) -> bool:
    return bool(d.get("app_key") and d["secrets"].get("refresh"))


def ready(d: dict) -> bool:
    return connected(d) and bool(d["secrets"].get("password"))


def redirect_uri() -> str:
    return settings.public_url.rstrip("/") + "/settings/dropbox/callback"


def set_password(db: Session, pw: str) -> None:
    pw = (pw or "").strip()
    if len(pw) < MIN_PASSWORD:
        raise DropboxError(f"Пароль шифрування — щонайменше {MIN_PASSWORD} символів")
    d = cfg(db)
    if d["secrets"].get("password") and secret(d, "password") != pw:
        d["files"] = {}                    # новий пароль — файли перешифруються й докопіюються заново
    set_secret(d, "password", pw)
    save(db, d)


# ---------------- OAuth (PKCE) ----------------
def start_auth(db: Session, app_key: str) -> str:
    app_key = (app_key or "").strip()
    if not app_key or len(app_key) > 64:
        raise DropboxError("Вставте App key застосунку Dropbox")
    d = cfg(db)
    verifier = secrets.token_urlsafe(64)[:96]
    state_ = secrets.token_urlsafe(16)
    d["app_key"] = app_key
    set_secret(d, "verifier", verifier)
    d["state"] = state_
    save(db, d)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return AUTH + "?" + urlencode({"client_id": app_key, "response_type": "code", "token_access_type": "offline",
                                   "code_challenge": challenge, "code_challenge_method": "S256",
                                   "redirect_uri": redirect_uri(), "state": state_})


def finish_auth(db: Session, code: str, state_: str, http: httpx.Client | None = None) -> dict:
    d = cfg(db)
    if not code or not state_ or state_ != d.get("state"):
        raise DropboxError("Підключення не підтверджено — спробуйте ще раз «Підключити Dropbox»")
    r = (http or httpx.Client(timeout=30)).post(TOKEN, data={
        "code": code, "grant_type": "authorization_code", "client_id": d["app_key"],
        "code_verifier": secret(d, "verifier"), "redirect_uri": redirect_uri()})
    if r.status_code >= 400:
        raise DropboxError(f"Dropbox не видав доступ ({r.status_code}): перевірте App key і Redirect URI в застосунку")
    j = r.json()
    if not j.get("refresh_token"):
        raise DropboxError("Dropbox не повернув refresh-токен")
    set_secret(d, "refresh", j["refresh_token"])
    d["secrets"].pop("verifier", None)
    d.pop("state", None)
    d["account_id"] = j.get("account_id")
    d["connected_at"] = datetime.now().isoformat(timespec="seconds")
    save(db, d)
    try:
        acc = Client(d, http).call("users/get_current_account", None)
        d["account"] = (acc.get("email") or (acc.get("name") or {}).get("display_name") or "")[:120]
        save(db, d)
    except Exception:  # noqa: BLE001
        pass
    return d


def disconnect(db: Session) -> None:
    d = cfg(db)
    d["secrets"].pop("refresh", None)
    for k in ("account", "account_id", "connected_at", "state"):
        d.pop(k, None)
    save(db, d)


# ---------------- API ----------------
class Client:
    def __init__(self, d: dict, http: httpx.Client | None = None):
        self.d, self.http = d, http or httpx.Client(timeout=120)
        self._tok, self._exp = None, 0.0

    def token(self) -> str:
        if self._tok and time.time() < self._exp - 60:
            return self._tok
        r = self.http.post(TOKEN, data={"grant_type": "refresh_token", "refresh_token": secret(self.d, "refresh"),
                                        "client_id": self.d["app_key"]})
        if r.status_code >= 400:
            raise DropboxError(f"Dropbox: доступ відкликано або токен недійсний ({r.status_code}) — підключіть Dropbox знову")
        j = r.json()
        self._tok, self._exp = j["access_token"], time.time() + float(j.get("expires_in") or 3600)
        return self._tok

    def call(self, ep: str, body: dict | None):
        r = self.http.post(f"{API}/{ep}", headers={"Authorization": f"Bearer {self.token()}",
                                                    **({"Content-Type": "application/json"} if body is not None else {})},
                           content=json.dumps(body) if body is not None else None)
        if r.status_code == 409:
            return {"_conflict": r.text[:300]}
        if r.status_code >= 400:
            raise DropboxError(f"Dropbox {ep}: {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    def _content(self, ep: str, arg: dict, data: bytes):
        r = self.http.post(f"{CONTENT}/{ep}", headers={"Authorization": f"Bearer {self.token()}",
                                                        "Dropbox-API-Arg": json.dumps(arg, ensure_ascii=True),
                                                        "Content-Type": "application/octet-stream"}, content=data)
        if r.status_code >= 400:
            raise DropboxError(f"Dropbox {ep}: {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}

    def upload(self, data: bytes, path: str) -> dict:
        commit = {"path": path, "mode": "overwrite", "mute": True}
        if len(data) <= SIMPLE_LIMIT:
            return self._content("files/upload", commit, data)
        sid = self._content("files/upload_session/start", {"close": False}, data[:CHUNK])["session_id"]
        off = CHUNK
        while len(data) - off > CHUNK:
            self._content("files/upload_session/append_v2", {"cursor": {"session_id": sid, "offset": off}, "close": False},
                          data[off:off + CHUNK])
            off += CHUNK
        return self._content("files/upload_session/finish", {"cursor": {"session_id": sid, "offset": off}, "commit": commit},
                             data[off:])

    def list(self, path: str) -> list[dict]:
        j = self.call("files/list_folder", {"path": path, "recursive": False})
        if j.get("_conflict"):
            return []
        out = list(j.get("entries") or [])
        while j.get("has_more"):
            j = self.call("files/list_folder/continue", {"cursor": j["cursor"]})
            out += j.get("entries") or []
        return out

    def delete(self, path: str) -> None:
        self.call("files/delete_v2", {"path": path})


# ---------------- шифрування (той самий формат, що й копії в Telegram) ----------------
def _encrypt(data: bytes, password: str) -> bytes:
    from .backups import MAGIC, _fernet
    salt = os.urandom(16)
    return MAGIC + salt + _fernet(password, salt).encrypt(data)


def decrypt_bytes(raw: bytes, password: str) -> bytes:
    from .backups import MAGIC, _fernet
    return _fernet(password, raw[len(MAGIC):len(MAGIC) + 16]).decrypt(raw[len(MAGIC) + 16:])


# ---------------- копія ----------------
def run(db: Session, src: Path, http: httpx.Client | None = None, with_files: bool = True) -> dict:
    """Вивантажити копію бази (і докопіювати файли) у Dropbox. Повертає звіт; не підключено — нічого не робить."""
    from . import alerts
    d = cfg(db)
    if not connected(d):
        return {"status": "не підключено"}
    pw = secret(d, "password")
    if not pw:
        alerts.failure("backup_dropbox", "Копія не вивантажена в Dropbox: не задано пароль шифрування",
                       "Задайте пароль у «Дані → Резервні копії → Dropbox».")
        return {"status": "немає пароля"}
    rep: dict = {"status": "ok"}
    try:
        c = Client(d, http)
        name = src.name + ".enc"
        c.upload(_encrypt(src.read_bytes(), pw), f"/db/{name}")
        rep["file"] = name
        olds = sorted((e for e in c.list("/db") if e.get(".tag") == "file" and e.get("name", "").endswith(".enc")),
                      key=lambda e: e.get("name"), reverse=True)
        for e in olds[KEEP:]:
            c.delete(e["path_lower"])
        rep["kept"] = min(len(olds), KEEP)
        if with_files:
            rep["files"] = _mirror(c, d, pw)
        d["last"] = {"at": datetime.now().isoformat(timespec="seconds"), "file": name, "ok": True,
                     "files": rep.get("files"), "error": None}
        save(db, d)
        alerts.success("backup_dropbox", f"Dropbox: {name}")
        return rep
    except Exception as e:  # noqa: BLE001
        log.exception("dropbox backup failed")
        d = cfg(db)
        d["last"] = {**(d.get("last") or {}), "at": datetime.now().isoformat(timespec="seconds"), "ok": False, "error": str(e)[:300]}
        save(db, d)
        alerts.failure("backup_dropbox", f"Копія в Dropbox не вивантажена: {e}", "Перевірте підключення Dropbox у «Дані → Резервні копії».")
        return {"status": "помилка", "error": str(e)[:300]}


def _mirror(c: Client, d: dict, pw: str) -> dict:
    src = Path(settings.files_dir)
    rep = {"uploaded": 0, "mb": 0.0, "left": 0}
    if not src.exists():
        return rep
    done: dict = d["files"]
    t0 = time.time()
    todo = []
    for f in src.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src).as_posix()
            try:
                sz = f.stat().st_size
            except OSError:
                continue
            if done.get(rel) != sz:
                todo.append((f, rel, sz))
    for i, (f, rel, sz) in enumerate(todo):
        if time.time() - t0 > FILES_BUDGET_S:
            rep["left"] = len(todo) - i
            break
        c.upload(_encrypt(f.read_bytes(), pw), "/files/" + rel + ".enc")
        done[rel] = sz
        rep["uploaded"] += 1
        rep["mb"] += sz / 1024 ** 2
    rep["mb"] = round(rep["mb"], 1)
    rep["total"] = len(done)
    return rep


def status(db: Session) -> dict:
    d = cfg(db)
    return {"app_key": d.get("app_key") or "", "connected": connected(d), "ready": ready(d), "has_password": bool(d["secrets"].get("password")),
            "account": d.get("account"), "connected_at": d.get("connected_at"), "last": d.get("last") or {},
            "files": len(d.get("files") or {}), "redirect": redirect_uri()}
